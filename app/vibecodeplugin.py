#!/usr/bin/env python3
"""
JellyJams - Jellyfin Playlist Generator
Generates music playlists using Jellyfin API and saves them as XML files
"""

import os
import random
import unicodedata
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
import requests
import schedule
import signal
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from io import BytesIO

# Pillow is a hard requirement: the genre cover fallback draws with it.
from PIL import Image

from genre_cover import find_cover, render_artist_cover, render_genre_cover

# Configuration
SETTINGS_FILE = '/data/config/settings.json'
COVER_SOURCE_DIR = '/data/cover'

# --- Name normalization helper ---
def normalize_name(s: str) -> str:
    """Normalize playlist/artist names to avoid Unicode punctuation mismatches.
    Converts curly quotes/apostrophes and dash variants to ASCII, applies NFKC, trims.
    """
    if not s:
        return ''
    # Normalize compatibility forms (e.g., fullwidth quotes) and compose
    s = unicodedata.normalize('NFKC', s)
    # Replace common Unicode quotes/apostrophes with ASCII
    quote_map = {
        '\u2018': "'",  # left single quotation mark
        '\u2019': "'",  # right single quotation mark (Guns N’ Roses)
        '\u201B': "'",  # single high-reversed-9 quotation mark
        '\u2032': "'",  # prime
        '\u02BC': "'",  # modifier letter apostrophe
        '\uFF07': "'",  # fullwidth apostrophe
        '\u201C': '"',   # left double quotation mark
        '\u201D': '"',   # right double quotation mark
        '\uFF02': '"',   # fullwidth quotation mark
    }
    for k, v in quote_map.items():
        s = s.replace(k, v)
    # Replace dash variants with ASCII hyphen
    for dash in ['\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015']:
        s = s.replace(dash, '-')
    return s.strip()
class Config:
    def __init__(self):
        # Set constants
        self.playlist_folder = '/playlists'

        # Load environment variables first (as defaults)
        self.jellyfin_url = os.getenv('JELLYFIN_URL', 'http://jellyfin:8096')
        self.api_key = os.getenv('JELLYFIN_API_KEY', '')
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        # Media library scan after playlist creation
        self.trigger_library_scan = os.getenv('TRIGGER_LIBRARY_SCAN', 'true').lower() == 'true'
        # Playlists with a generated-looking name that JellyJams has no record of are left
        # alone. Set ADOPT_EXISTING_PLAYLISTS=true for one run to take them over.
        self.adopt_existing_playlists = os.getenv('ADOPT_EXISTING_PLAYLISTS', 'false').lower() == 'true'
        # Set by `python vibecodeplugin.py --dry-run`: read everything, write nothing.
        self.dry_run = False
        # Remove playlists JellyJams made earlier that this run no longer generates (for
        # example the old per-genre playlists after the caps came in). Off by default;
        # `python vibecodeplugin.py --retire-stale` turns it on for one pass.
        self.retire_stale_playlists = os.getenv('RETIRE_STALE_PLAYLISTS', 'false').lower() == 'true'
        
        # Set default values for web UI configurable variables
        self.generation_interval = 24
        self.max_tracks_per_playlist = 100
        self.min_tracks_per_playlist = 5
        self.excluded_genres = []
        self.excluded_artists = []
        self.shuffle_tracks = True
        # Pick a random sample when a genre/decade pool is bigger than the cap, instead
        # of always taking the first N tracks in library order.
        self.random_track_selection = True
        self.playlist_types = ['Genre','Year','Artist','Personal']
        
        # Playback Reporting: how far back to look for play counts when building
        # personal playlists. A window that is too short leaves "Top Tracks" empty for
        # anyone who does not listen every week, so the code widens once before falling
        # back to favourites.
        self.listening_window_days = 90
        
        # Spotify API configuration (optional)
        self.spotify_client_id = ''
        self.spotify_client_secret = ''
        self.spotify_cover_art_enabled = False

        # Deezer cover art (public API, no key or account needed)
        self.deezer_cover_art_enabled = False
        # Draw "THIS IS / <artist>" on artist covers. Off leaves the plain photo.
        self.artist_cover_name_overlay = os.getenv('ARTIST_COVER_NAME_OVERLAY', 'true').lower() == 'true'
        
        # User configuration for personalized playlists
        self.personal_playlist_users = 'all'
        self.personal_playlist_new_users_default = True
        self.personal_playlist_min_user_tracks = 10
        
        # Playlist diversity settings
        self.min_artist_diversity = 5

        # Discovery playlist diversity settings
        self.discovery_max_songs_per_album = 1
        self.discovery_max_songs_per_artist = 2
        
        # Artist playlist requirements
        self.min_albums_per_artist = 2
        
        # Decade playlist requirements
        self.min_albums_per_decade = 3
        
        # Scheduling configuration
        self.auto_generate_on_startup = False
        self.schedule_mode = 'weekly'      # manual, daily, weekly, interval
        self.schedule_weekday = 'sunday'   # weekday name, used by weekly mode
        self.schedule_time = '04:30'       # HH:MM, container local time
        
        # How many playlists a run may make. The point is quality over quantity: a 10,000
        # track library otherwise produces well over a hundred genre playlists.
        self.max_genre_playlists = 14      # the biggest genres, always made
        self.genre_spotlight_slots = 2     # extra genres from the long tail, rotating weekly
        self.genre_split_share = 0.25      # a group holding more than this share of the library is split
        self.max_artist_playlists = 20     # "This is <artist>!", biggest artists first
        self.max_total_playlists = 45      # genre + decade + artist playlists together
        
        # Genre grouping/mapping system
        self.genre_grouping_enabled = True
        self.genre_mappings = self._load_genre_mappings()
        
        # Load web UI settings if they exist (these override environment variables)
        self.load_web_ui_settings()
    
    def load_web_ui_settings(self):
        """Load settings from web UI JSON file - these take precedence over environment variables"""
        config_file = SETTINGS_FILE
        try:
            if Path(config_file).exists():
                with open(config_file, 'r') as f:
                    web_settings = json.load(f)
                    
                # Apply web UI settings (override environment variables)
                if 'jellyfin_url' in web_settings:
                    self.jellyfin_url = web_settings['jellyfin_url']
                if 'max_tracks_per_playlist' in web_settings:
                    self.max_tracks_per_playlist = int(web_settings['max_tracks_per_playlist'])
                if 'min_tracks_per_playlist' in web_settings:
                    self.min_tracks_per_playlist = int(web_settings['min_tracks_per_playlist'])
                if 'excluded_genres' in web_settings:
                    self.excluded_genres = web_settings['excluded_genres'] if isinstance(web_settings['excluded_genres'], list) else web_settings['excluded_genres'].split(',')
                if 'excluded_artists' in web_settings:
                    self.excluded_artists = web_settings['excluded_artists'] if isinstance(web_settings['excluded_artists'], list) else web_settings['excluded_artists'].split(',')
                if 'shuffle_tracks' in web_settings:
                    self.shuffle_tracks = bool(web_settings['shuffle_tracks'])
                if 'random_track_selection' in web_settings:
                    self.random_track_selection = bool(web_settings['random_track_selection'])
                if 'playlist_types' in web_settings:
                    self.playlist_types = web_settings['playlist_types'] if isinstance(web_settings['playlist_types'], list) else web_settings['playlist_types'].split(',')
                if 'generation_interval' in web_settings:
                    self.generation_interval = int(web_settings['generation_interval'])
                if 'log_level' in web_settings:
                    self.log_level = web_settings['log_level']
                if 'min_artist_diversity' in web_settings:
                    self.min_artist_diversity = int(web_settings['min_artist_diversity'])
                if 'spotify_client_id' in web_settings:
                    self.spotify_client_id = web_settings['spotify_client_id']
                if 'spotify_client_secret' in web_settings:
                    self.spotify_client_secret = web_settings['spotify_client_secret']
                if 'spotify_cover_art_enabled' in web_settings:
                    self.spotify_cover_art_enabled = bool(web_settings['spotify_cover_art_enabled'])
                if 'deezer_cover_art_enabled' in web_settings:
                    self.deezer_cover_art_enabled = bool(web_settings['deezer_cover_art_enabled'])
                if 'artist_cover_name_overlay' in web_settings:
                    self.artist_cover_name_overlay = bool(web_settings['artist_cover_name_overlay'])
                
                # User configuration for personalized playlists
                if 'personal_playlist_users' in web_settings:
                    self.personal_playlist_users = web_settings['personal_playlist_users']
                if 'personal_playlist_new_users_only' in web_settings:
                    self.personal_playlist_new_users_only = bool(web_settings['personal_playlist_new_users_only'])
                
                # Scheduling configuration
                if 'auto_generate_on_startup' in web_settings:
                    self.auto_generate_on_startup = bool(web_settings['auto_generate_on_startup'])
                if 'schedule_mode' in web_settings:
                    self.schedule_mode = web_settings['schedule_mode']
                if 'schedule_time' in web_settings:
                    self.schedule_time = web_settings['schedule_time']
                if 'schedule_weekday' in web_settings:
                    self.schedule_weekday = web_settings['schedule_weekday']
                if 'personal_playlist_min_user_tracks' in web_settings:
                    self.personal_playlist_min_user_tracks = int(web_settings['personal_playlist_min_user_tracks'])
                if 'discovery_max_songs_per_album' in web_settings:
                    self.discovery_max_songs_per_album = int(web_settings['discovery_max_songs_per_album'])
                if 'discovery_max_songs_per_artist' in web_settings:
                    self.discovery_max_songs_per_artist = int(web_settings['discovery_max_songs_per_artist'])
                if 'listening_window_days' in web_settings:
                    self.listening_window_days = int(web_settings['listening_window_days'])
                if 'min_albums_per_artist' in web_settings:
                    self.min_albums_per_artist = int(web_settings['min_albums_per_artist'])
                if 'min_albums_per_decade' in web_settings:
                    self.min_albums_per_decade = int(web_settings['min_albums_per_decade'])
                if 'trigger_library_scan' in web_settings:
                    self.trigger_library_scan = bool(web_settings['trigger_library_scan'])
                if 'genre_grouping_enabled' in web_settings:
                    self.genre_grouping_enabled = bool(web_settings['genre_grouping_enabled'])
                for key in ('max_genre_playlists', 'genre_spotlight_slots', 'max_artist_playlists',
                            'max_total_playlists'):
                    if key in web_settings:
                        setattr(self, key, max(0, int(web_settings[key])))
                if 'genre_split_share' in web_settings:
                    self.genre_split_share = min(1.0, max(0.0, float(web_settings['genre_split_share'])))
                    
                print(f"🎵  JellyJams web UI settings loaded - overriding environment variables")
                print(f"   Max tracks: {self.max_tracks_per_playlist}, Min tracks: {self.min_tracks_per_playlist}")
                print(f"   Playlist types: {', '.join(self.playlist_types)}")
                print(f"   Excluded genres: {', '.join(self.excluded_genres) if self.excluded_genres else 'None'}")
            print(f"   Excluded artists: {', '.join(self.excluded_artists) if self.excluded_artists else 'None'}")
                
        except Exception as e:
            print(f"⚠️  Could not load web UI settings: {e}")
            print(f"   Using environment variables instead")
    
    def _load_genre_mappings(self):
        """Load the genre groups, with each genre claimed by exactly one group.

        Some genres are genuinely named in more than one list below. map_genre_to_group
        returns the first group it finds, so a later listing is dead weight and the
        grouping silently depends on dict order. Drop the duplicates here so the map
        says what it does.
        """
        mappings = {
            # Rock and its many subgenres
            'Rock': [
                'Rock', 'Classic Rock', 'Hard Rock', 'Soft Rock', 'Arena Rock', 'Art Rock',
                'Alternative Rock', 'Indie Rock', 'Progressive Rock', 'Psychedelic Rock',
                'Blues Rock', 'Country Rock', 'Folk Rock', 'Garage Rock', 'Glam Rock',
                'Gothic Rock', 'Grunge', 'Heartland Rock', 'Mainstream Rock', 'Math Rock',
                'Noise Rock', 'Post-Rock', 'Punk Rock', 'Southern Rock', 'Stoner Rock',
                'Symphonic Rock', 'Experimental Rock', 'Electronic Rock', 'Funk Rock',
                'Piano Rock', 'Garage Rock Revival', 'Desert Rock', 'Boogie Rock',
                'Swamp Rock', 'Roots Rock', 'Dance-Rock', 'Rap Rock', 'Nu Metal',
                'Acoustic Rock', 'AlternRock', 'Britpop', 'Crossover Prog', 'Indie Rock/Rock Pop',
                'Post-Grunge', 'Post-Britpop', 'Slacker Rock', 'Surf Punk', 'Beat Music'
            ],
            
            # Pop and its variants
            'Pop': [
                'Pop', 'Pop Rock', 'Dance-Pop', 'Electropop', 'Synth-Pop', 'Art Pop',
                'Alternative Pop', 'Indie Pop', 'Dream Pop', 'Power Pop', 'Baroque Pop',
                'Chamber Pop', 'Sunshine Pop', 'Traditional Pop', 'International Pop',
                'Ambient Pop', 'Bedroom Pop', 'Hypnagogic Pop', 'Jangle Pop', 'Noise Pop',
                'Twee Pop', 'Progressive Pop', 'Psychedelic Pop', 'Sophisti-Pop',
                'Indie Pop/Folk', 'Pop Soul', 'Pop Metal', 'Country Pop', 'Latin Pop',
                'J-Pop', 'Pop Punk', 'Pop Rap', 'Reggae-Pop'
            ],
            
            # Electronic and dance music
            'Electronic': [
                'Electronic', 'Electronica', 'Electro', 'EDM', 'Techno', 'House',
                'Trance', 'Dubstep', 'Drum And Bass', 'Ambient', 'Downtempo',
                'Breakbeat', 'Breaks', 'Big Beat', 'Dance', 'Electro House',
                'Deep House', 'Tech House', 'Progressive House', 'Hard Techno',
                'Hardstyle', 'Dark Electro', 'Electro-Industrial', 'Trip Hop',
                'Chillwave', 'Synthwave', 'Minimal Synth', 'Indietronica',
                'Folktronica', 'New Rave', 'Jersey Club', 'Leftfield'
            ],
            
            # Hip Hop and Rap
            'Hip Hop': [
                'Hip Hop', 'Rap/Hip Hop', 'Alternative Hip Hop', 'East Coast Hip Hop',
                'Southern Hip Hop', 'Conscious Hip Hop', 'Political Hip Hop',
                'Experimental Hip Hop', 'Cloud Rap', 'Emo Rap', 'Trap', 'Grime',
                'Hip House', 'Rap Metal', 'Rapcore', 'Country Rap', 'Trap Latino',
                'Sexy Drill'
            ],
            
            # Alternative and Indie
            'Alternative': [
                'Alternative', 'Alternative Country', 'Alternative Dance', 'Alternative Folk',
                'Alternative Hip Hop', 'Alternative Metal', 'Alternative Pop', 'Alternative Punk',
                'Alternative R&B', 'Indie Folk', 'Indie Pop', 'Indie Rock', 'Indie Surf',
                'Indie, Blues Rock', 'Neo-Acoustic', 'Neo-Psychedelia'
            ],
            
            # Metal
            'Metal': [
                'Metal', 'Heavy Metal', 'Alternative Metal', 'Doom Metal', 'Glam Metal',
                'Gothic Metal', 'Industrial Metal', 'Nu Metal', 'Pop Metal',
                'Progressive Metal', 'Rap Metal', 'Stoner Metal', 'Traditional Doom Metal',
                'Neue Deutsche HäRte'
            ],
            
            # Punk
            'Punk': [
                'Punk', 'Punk Rock', 'Alternative Punk', 'Dance-Punk', 'Garage Punk',
                'Pop Punk', 'Post-Punk', 'Post-Punk Revival', 'Punk Blues', 'Surf Punk',
                'Emo', 'Hardcore', 'Melodic Hardcore', 'Post-Hardcore', 'Midwest Emo'
            ],
            
            # Blues
            'Blues': [
                'Blues', 'Blues Rock', 'British Blues', 'Country Blues', 'Electric Blues',
                'Hill Country Blues', 'Piano Blues', 'Punk Blues', 'Blue-Eyed Soul'
            ],
            
            # Jazz
            'Jazz': [
                'Jazz', 'Jazz Fusion', 'Vocal Jazz', 'Dixieland'
            ],
            
            # Country
            'Country': [
                'Country', 'Alternative Country', 'Country Blues', 'Country Pop',
                'Country Rap', 'Country Rock', 'Country Soul', 'Progressive Country',
                'Traditional Country', 'Americana'
            ],
            
            # R&B and Soul
            'R&B': [
                'R&B', 'Contemporary R&B', 'Alternative R&B', 'Soul', 'Neo Soul',
                'Pop Soul', 'Psychedelic Soul', 'Southern Soul', 'Smooth Soul',
                'Country Soul', 'Blue-Eyed Soul'
            ],
            
            # Funk
            'Funk': [
                'Funk', 'Funk Rock', 'Synth Funk'
            ],
            
            # Reggae
            'Reggae': [
                'Reggae', 'Reggae-Pop', 'Reggaeton', 'Dancehall', 'Dub', 'Ambient Dub'
            ],
            
            # Folk
            'Folk': [
                'Folk', 'Alternative Folk', 'Contemporary Folk', 'Folk Pop', 'Folk Rock',
                'Indie Folk', 'Stomp And Holler'
            ],
            
            # Classical and Orchestral
            'Classical': [
                'Classical', 'Modern Classical', 'Cinematic Classical', 'Opera',
                'Orchestral', 'Symphonic Prog', 'Symphonic Rock'
            ],
            
            # World Music
            'World': [
                'Asian Music', 'Brazilian Music', 'Latin Music', 'Afrobeat', 'Soukous',
                'Salsa', 'Plena', 'Schlager'
            ],
            
            # Gospel and Religious
            'Gospel': [
                'Gospel', 'Contemporary Gospel', 'Southern Gospel'
            ],
            
            # Ambient and Experimental
            'Ambient': [
                'Ambient', 'Ambient Dub', 'Ambient Pop', 'Space Ambient', 'Experimental',
                'Avant-Garde', 'Noise Pop', 'Noise Rock', 'Slowcore'
            ],
            
            # Singer-Songwriter
            'Singer-Songwriter': [
                'Singer-Songwriter', 'Singer & Songwriter'
            ],
            
            # New Wave and Synth
            'New Wave': [
                'New Wave', 'New Romantic', 'Synth-Pop', 'Electropop', 'Synthwave',
                'Minimal Synth'
            ],
            
            # Disco
            'Disco': [
                'Disco'
            ],
            
            # Rockabilly
            'Rockabilly': [
                'Rockabilly', 'Rock And Roll', 'Rock & Roll/Rockabilly'
            ],
            
            # Industrial
            'Industrial': [
                'Industrial', 'Industrial Metal', 'Industrial Rock', 'Electro-Industrial'
            ],
            
            # Shoegaze
            'Shoegaze': [
                'Shoegaze', 'Dream Pop'
            ],
            
            # Lounge
            'Lounge': [
                'Lounge'
            ]
        }

        claimed = {}
        normalised = {}
        for group, genres in mappings.items():
            kept = []
            for genre in genres:
                if genre in claimed:
                    logging.getLogger('jellyjams').debug(
                        f"Dropping duplicate genre mapping '{genre}' from '{group}' "
                        f"(already mapped to '{claimed[genre]}')"
                    )
                    continue
                claimed[genre] = group
                kept.append(genre)
            normalised[group] = kept
        return normalised

    def map_genre_to_group(self, genre):
        """Map a specific genre to its broader group category, ignoring case"""
        if not self.genre_grouping_enabled:
            return genre
        
        genre = genre.strip()
        
        lookup = getattr(self, '_genre_lookup', None)
        if lookup is None or getattr(self, '_genre_lookup_source', None) is not self.genre_mappings:
            lookup = {}
            for group_name, genre_list in self.genre_mappings.items():
                for member in genre_list:
                    lookup.setdefault(member.strip().lower(), group_name)
            self._genre_lookup = lookup
            self._genre_lookup_source = self.genre_mappings
        
        # If no mapping is found the genre stands for itself
        return lookup.get(genre.lower(), genre)

class SpotifyClient:
    """Spotify API client for downloading cover art"""
    
    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger
        self.spotify = None
        # Statistics tracking
        self.stats = {
            'total_attempts': 0,
            'successful_downloads': 0,
            'failed_downloads': 0,
            'successful_searches': 0,
            'api_errors': 0,
            'response_times': [],
            'last_test_time': None,
            'last_test_result': False,
            'initialization_success': False,
            'initialization_attempts': 0
        }
        self._initialize_client()
    
    def _initialize_client(self):
        self.stats['initialization_attempts'] += 1
        
        if not self.config.spotify_cover_art_enabled:
            self.logger.info("Spotify cover art is disabled in configuration")
            return
            
        if not self.config.spotify_client_id or not self.config.spotify_client_secret:
            self.logger.warning("Spotify credentials not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.")
            return
            
        try:
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyClientCredentials
            except ImportError:
                self.logger.warning("Spotify support needs the spotipy package, which this image does not include. "
                                    "Set SPOTIPY_ENABLED=false or add spotipy to the image.")
                return
            
            self.logger.info("Initializing Spotify API client...")
            client_credentials_manager = SpotifyClientCredentials(
                client_id=self.config.spotify_client_id,
                client_secret=self.config.spotify_client_secret
            )
            self.spotify = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
            
            # Test the connection immediately
            try:
                # Simple test query to verify credentials work
                test_result = self.spotify.search(q='test', type='track', limit=1)
                self.logger.info("✅ Spotify API client initialized and tested successfully")
                self.stats['initialization_success'] = True
            except Exception as test_e:
                self.logger.error(f"❌ Spotify API credentials test failed: {test_e}")
                self.spotify = None
                self.stats['initialization_success'] = False
                self.stats['api_errors'] += 1
                
        except ImportError:
            self.logger.error("❌ Spotipy library not found. Install with: pip install spotipy")
            self.stats['initialization_success'] = False
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Spotify API client: {e}")
            self.spotify = None
            self.stats['initialization_success'] = False
            self.stats['api_errors'] += 1
    
    def is_enabled(self) -> bool:
        """Check if Spotify integration is enabled and configured"""
        return self.spotify is not None
    
    def search_artist_playlist(self, artist_name: str) -> dict:
        """Search for 'This is {artist}' playlist on Spotify"""
        # Double-check that Spotify client is available
        if not self.is_enabled() or self.spotify is None:
            self.logger.debug(f"Spotify client not available for searching artist: {artist_name}")
            return None
            
        try:
            # Search for "This is {artist}" playlist
            query = f"This is {artist_name}"
            self.logger.debug(f"Searching Spotify for: {query}")
            
            results = self.spotify.search(q=query, type='playlist', limit=10)
            
            # Validate results structure
            if not results or 'playlists' not in results or not results['playlists']:
                self.logger.debug(f"No playlists section in Spotify results for: {artist_name}")
                return None
                
            if 'items' not in results['playlists'] or not results['playlists']['items']:
                self.logger.debug(f"No playlist items found for: {artist_name}")
                return None
            
            # Look for exact or close matches
            for playlist in results['playlists']['items']:
                if not playlist or 'name' not in playlist:
                    continue
                    
                playlist_name = playlist['name'].lower()
                target_name = f"this is {artist_name.lower()}"
                
                # Check for exact match or close variations
                if (playlist_name == target_name or 
                    playlist_name == f"this is {artist_name.lower()}!" or
                    playlist_name.startswith(target_name)):
                    
                    self.logger.info(f"✅ Found Spotify playlist: {playlist['name']} for artist: {artist_name}")
                    self.stats['successful_searches'] += 1
                    return playlist
            
            self.logger.debug(f"No 'This is {artist_name}' playlist found on Spotify")
            return None
            
        except Exception as e:
            self.logger.error(f"❌ Error searching Spotify for artist {artist_name}: {e}")
            self.stats['api_errors'] += 1
            return None
    
    def download_cover_art(self, playlist_info: dict, save_path: str) -> bool:
        """Download cover art from Spotify playlist"""
        if not playlist_info or not playlist_info.get('images'):
            return False
            
        try:
            # Get the highest quality image (first in the list)
            image_url = playlist_info['images'][0]['url']
            
            # Download the image
            import requests
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            
            # Save to file
            Image.open(BytesIO(response.content)).save(save_path)
            
            self.logger.info(f"Downloaded Spotify cover art: {save_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error downloading cover art: {e}")
            return False
    
    def get_artist_cover_art(self, artist_name: str, playlist_dir: Path) -> bool:
        """Get and save cover art for an artist playlist"""
        # Double-check that Spotify client is available
        if not self.is_enabled() or self.spotify is None:
            self.logger.debug(f"Spotify client not available for getting cover art for: {artist_name}")
            return False
            
        import time
        start_time = time.time()
        self.stats['total_attempts'] += 1
        exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif']
        
        try:
            # Check if cover art already exists
            for ext in exts:
                cover_path = playlist_dir / f"cover{ext}"
                if cover_path.exists():
                    self.logger.debug(f"Cover art already exists for {artist_name}")
                    self.stats['successful_downloads'] += 1
                    return True
            
            # Search for Spotify playlist
            playlist_info = self.search_artist_playlist(artist_name)
            if not playlist_info:
                self.stats['failed_downloads'] += 1
                return False
            
            # Download cover art
            success = self.download_cover_art(playlist_info, str(playlist_dir / "cover.webp"))
            
            # Track statistics
            response_time = time.time() - start_time
            self.stats['response_times'].append(response_time)
            if len(self.stats['response_times']) > 100:  # Keep last 100 response times
                self.stats['response_times'] = self.stats['response_times'][-100:]
            
            if success:
                self.stats['successful_downloads'] += 1
            else:
                self.stats['failed_downloads'] += 1
                
            return success
            
        except Exception as e:
            self.logger.error(f"Error getting cover art for {artist_name}: {e}")
            self.stats['failed_downloads'] += 1
            self.stats['api_errors'] += 1
            return False
    
    def test_connection(self) -> dict:
        """Test Spotify API connection and return results"""
        import time
        from datetime import datetime
        
        test_result = {
            'success': False,
            'message': '',
            'response_time': 0,
            'timestamp': datetime.now().isoformat()
        }
        
        start_time = time.time()
        
        try:
            if not self.config.spotify_client_id or not self.config.spotify_client_secret:
                test_result['message'] = 'Spotify credentials not configured'
                return test_result
            
            if not self.is_enabled():
                test_result['message'] = 'Spotify client not initialized'
                return test_result
            
            # Test API call - search for a popular playlist
            results = self.spotify.search(q='This is Drake', type='playlist', limit=1)
            
            test_result['response_time'] = time.time() - start_time
            
            if results and results.get('playlists') and results['playlists'].get('items'):
                test_result['success'] = True
                test_result['message'] = 'Spotify API connection successful'
            else:
                test_result['message'] = 'Spotify API returned empty results'
                
        except Exception as e:
            test_result['response_time'] = time.time() - start_time
            test_result['message'] = f'Spotify API error: {str(e)}'
            self.stats['api_errors'] += 1
        
        # Update test statistics
        self.stats['last_test_time'] = test_result['timestamp']
        self.stats['last_test_result'] = test_result['success']
        
        return test_result
    
    def get_statistics(self) -> dict:
        """Get current Spotify integration statistics"""
        stats = self.stats.copy()
        
        # Calculate additional metrics
        if stats['total_attempts'] > 0:
            stats['success_rate'] = (stats['successful_downloads'] / stats['total_attempts']) * 100
        else:
            stats['success_rate'] = 0
            
        if stats['response_times']:
            stats['avg_response_time'] = sum(stats['response_times']) / len(stats['response_times'])
            stats['min_response_time'] = min(stats['response_times'])
            stats['max_response_time'] = max(stats['response_times'])
        else:
            stats['avg_response_time'] = 0
            stats['min_response_time'] = 0
            stats['max_response_time'] = 0
            
        return stats

# Setup logging
def setup_logging(config: Config):
    """Setup logging configuration with timestamps - ensure all logs visible in Docker"""
    # Ensure log directory exists
    log_dir = Path('/data/logs')
    log_level_num = logging._nameToLevel[config.log_level]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        print(f"📁 Log directory created/verified: {log_dir}")
    except Exception as e:
        print(f"⚠️ Could not create log directory {log_dir}: {e}")
    
    # Set log level according to settings/default
    print(f"🔧 Log level is set to {config.log_level}")
    
    # Create formatters with timestamps
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # Configure root logger to catch ALL logging
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level_num)
    root_logger.handlers.clear()
    
    # Console handler for Docker logs
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level_num)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Also setup specific jellyjams logger
    logger = logging.getLogger('jellyjams')
    logger.setLevel(log_level_num)
    logger.propagate = True  # Ensure it propagates to root logger
    
    # File handler (with error handling)
    try:
        # Rotating, so a long-running container cannot fill the disk with logs. The
        # generator process and the web workers share this file, so a rollover can
        # occasionally lose one backup across processes; losing a log line is
        # preferable to an unbounded file.
        file_handler = RotatingFileHandler(
            log_dir / 'jellyjams.log',
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8',
        )
        file_handler.setLevel(log_level_num)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        print(f"📝 File logging enabled (rotating, 5 MB x 3): {log_dir / 'jellyjams.log'}")
    except Exception as e:
        print(f"⚠️ Could not create file handler: {e}")
        print("📺 Continuing with console logging only")
    
    # Disable other loggers that might interfere
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    
    print(f"🔧 Logging initialized at {config.log_level} level with timestamps")
    print(f"📊 Root logger handlers: {len(root_logger.handlers)} ({[type(h).__name__ for h in root_logger.handlers]})")
    print(f"📊 JellyJams logger propagate: {logger.propagate}")
    
    # Test logging immediately
    logger.info("🎵 JellyJams logging system initialized and ready")
    logger.debug("🔍 Debug logging is active and visible")
    
    return logger

class DryRunWriteError(RuntimeError):
    """A write to Jellyfin was attempted during a dry run. Always a bug in the caller."""


RUN_STATUS_FILE = '/data/run_status.json'
MANAGED_PLAYLISTS_FILE = '/data/managed_playlists.json'


PERSONAL_PLAYLIST_PREFIXES = ('Top Tracks - ', 'Discovery Mix - ', 'Recent Favorites - ', 'Genre Mix - ')


def categorize_playlist(name: str) -> str:
    """File a playlist name under the kind JellyJams generates, or 'manual'.

    Returns 'genre', 'year', 'artist' or 'personal' for the four name patterns
    save_playlist is called with: "<genre> Radio", "Back to the <decade>s",
    "This is <artist>!" and the four personal mixes. Anything else is 'manual', a
    playlist somebody made by hand. The dashboard counts with this, and
    is_generated_playlist_name is built on it, so both always agree.
    """
    if not name:
        return 'manual'
    if name.startswith(PERSONAL_PLAYLIST_PREFIXES):
        return 'personal'
    if name.startswith('This is ') and name.endswith('!'):
        return 'artist'
    if re.match(r'^Back to the \d+s$', name):
        return 'year'
    if name.endswith(' Radio'):
        return 'genre'
    return 'manual'


def is_generated_playlist_name(name: str) -> bool:
    """True when a name is one JellyJams itself creates.

    This is the first guard before anything is deleted, so a playlist made by hand can
    never be removed even if a generated one wants the same name.
    """
    return categorize_playlist(name) != 'manual'


def load_managed_playlists() -> Dict[str, str]:
    """Names JellyJams created, mapped to the playlist id it last wrote."""
    try:
        path = Path(MANAGED_PLAYLISTS_FILE)
        if path.exists():
            return dict((json.loads(path.read_text()) or {}).get('playlists') or {})
    except Exception as e:
        logging.getLogger('jellyjams').warning(f"Could not read the managed playlist list: {e}")
    return {}


def save_managed_playlists(mapping: Dict[str, str]):
    try:
        path = Path(MANAGED_PLAYLISTS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'playlists': mapping}, indent=2, sort_keys=True))
        tmp.replace(path)
    except Exception as e:
        logging.getLogger('jellyjams').warning(f"Could not write the managed playlist list: {e}")


def write_run_status(success: bool, summary: str = '', counts: Optional[Dict] = None):
    """Record how the last generation went, so a failed run is visible outside the log.

    Written into the app data volume and read by /api/health and the container
    healthcheck, which means any monitoring tool can see it. Never raises: recording
    the status must not be what breaks a run.
    """
    try:
        payload = {
            'last_run_finished': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'success': bool(success),
            'summary': summary,
            'counts': counts or {},
        }
        path = Path(RUN_STATUS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(payload, f, indent=2)
        tmp.replace(path)
    except Exception as e:
        logging.getLogger('jellyjams').warning(f"Could not write run status: {e}")


class DeezerClient:
    """Artist pictures from the public Deezer API, used for playlist cover art.

    api.deezer.com needs no API key and no account, unlike the Spotify search
    endpoint that returns 403 unless the app owner has a paid subscription.
    picture_xl is a square 1000x1000 image.
    """

    API = "https://api.deezer.com"

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.stats = {'searches': 0, 'downloads': 0, 'errors': 0}
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'JellyJams/1.0 (+https://github.com/didiermortier/JellyJams)',
            'Accept': 'application/json',
        })
        if not self.is_enabled():
            self.logger.info("Deezer cover art is disabled in configuration")

    def is_enabled(self) -> bool:
        return bool(getattr(self.config, 'deezer_cover_art_enabled', False))

    def search_artist(self, artist_name: str) -> Optional[Dict]:
        """Return the best matching Deezer artist for a name, or None."""
        if not self.is_enabled() or not artist_name:
            return None
        try:
            resp = self.session.get(
                f"{self.API}/search/artist",
                params={'q': artist_name, 'limit': 5},
                timeout=15,
            )
            resp.raise_for_status()
            candidates = (resp.json() or {}).get('data') or []
            if not candidates:
                self.logger.info(f"🔎 Deezer: no artist match for {artist_name!r}")
                return None

            target = normalize_name(artist_name).lower()
            best = None
            for cand in candidates:
                name = normalize_name(cand.get('name') or '').lower()
                if name == target:
                    best = cand
                    break
                if best is None and name and (name in target or target in name):
                    best = cand
            best = best or candidates[0]
            self.stats['searches'] += 1
            return best
        except Exception as e:
            self.stats['errors'] += 1
            self.logger.warning(f"🌐 Deezer artist search failed for {artist_name}: {e}")
            return None

    def get_artist_cover_art(self, artist_name: str, playlist_dir: Path) -> bool:
        """Save the artist's Deezer picture as cover.jpg in playlist_dir."""
        if not self.is_enabled():
            return False

        artist = self.search_artist(artist_name)
        if not artist:
            return False

        url = (artist.get('picture_xl') or artist.get('picture_big')
               or artist.get('picture_medium'))
        if not url:
            self.logger.info(f"🔎 Deezer: no picture for {artist_name!r}")
            return False

        try:
            from PIL import Image as PILImage
        except ImportError:
            self.logger.warning("Pillow not available, skipping Deezer cover art")
            return False

        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            image = PILImage.open(BytesIO(resp.content))
            if image.width < 300 or image.height < 300:
                self.logger.info(
                    f"🔎 Deezer: picture too small for {artist_name!r} "
                    f"({image.width}x{image.height})"
                )
                return False

            # Jellyfin playlist covers are square, so centre-crop then cap the size.
            side = min(image.width, image.height)
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            image = image.convert('RGB').crop((left, top, left + side, top + side))
            if side != 1000:
                image = image.resize((1000, 1000), PILImage.Resampling.LANCZOS)

            target = playlist_dir / 'cover.jpg'
            drawn = False
            if getattr(self.config, 'artist_cover_name_overlay', True):
                try:
                    render_artist_cover(image, artist_name, target)
                    drawn = True
                except Exception as overlay_err:
                    self.logger.warning(f"Could not draw the name on {artist_name!r}'s picture, "
                                        f"using the plain photo: {overlay_err}")
            if not drawn:
                image.save(target, 'JPEG', quality=90)
            try:
                os.chmod(target, 0o664)
            except Exception as chmod_err:
                self.logger.debug(f"chmod failed for {target}: {chmod_err}")

            # Drop the older JellyJams covers so Jellyfin cannot keep serving one of
            # them instead of the picture we just fetched.
            for stale in ('cover.webp', 'folder.webp', 'folder.jpg', 'cover.png', 'folder.png'):
                stale_path = playlist_dir / stale
                if stale_path.exists():
                    try:
                        stale_path.unlink()
                        self.logger.debug(f"Removed stale cover: {stale_path}")
                    except Exception as stale_err:
                        self.logger.debug(f"Could not remove {stale_path}: {stale_err}")

            self.stats['downloads'] += 1
            self.logger.info(
                f"✅ Deezer cover art for {artist_name}: "
                f"{artist.get('name')} -> {target}"
            )
            return True
        except Exception as e:
            self.stats['errors'] += 1
            self.logger.warning(f"🌐 Deezer picture download failed for {artist_name}: {e}")
            return False

    def test_connection(self) -> dict:
        """Verify Deezer answers, for the settings page Test button."""
        if not self.is_enabled():
            return {'success': False, 'message': 'Deezer cover art is disabled'}
        artist = self.search_artist('Faithless')
        if not artist:
            return {'success': False, 'message': 'Deezer returned no artist match'}
        return {
            'success': True,
            'message': f"Deezer OK - test match {artist.get('name')} "
                       f"({artist.get('nb_fan', 0)} fans)",
        }

    def get_statistics(self) -> dict:
        return dict(self.stats)


class JellyfinAPI:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({
            # Jellyfin 10.11 removed the legacy X-Emby-Token scheme and Jellyfin 12
            # rejects it outright with 401. The supported form is the standard
            # Authorization header; verified against Jellyfin 12.1.
            'Authorization': f'MediaBrowser Token={config.api_key}',
            'Content-Type': 'application/json'
        })

    def _refuse_write(self, action: str):
        """Safety net: callers check dry_run first, so reaching this is a bug."""
        if getattr(self.config, 'dry_run', False):
            raise DryRunWriteError(f"Refusing to {action} during a dry run")

    def get_audio_items(self) -> List[Dict]:
        """Get all audio items from Jellyfin"""
        try:
            url = f"{self.config.jellyfin_url}/Items"
            params = {
                'IncludeItemTypes': 'Audio',
                'Recursive': 'true',
                'Fields': 'Path,Genres,ProductionYear,Artists,RunTimeTicks,DateCreated',
                'SortBy': 'SortName',
                'SortOrder': 'Ascending'
            }
            
            response = self.session.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            items = data.get('Items', [])
            
            self.logger.info(f"Retrieved {len(items)} audio items from Jellyfin")
            return items
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching audio items: {e}")
            return []

    def test_connection(self) -> bool:
        """Test connection to Jellyfin API"""
        try:
            url = f"{self.config.jellyfin_url}/System/Info"
            response = self.session.get(url)
            response.raise_for_status()
            
            info = response.json()
            self.logger.info(f"Connected to Jellyfin {info.get('Version', 'Unknown')} at {self.config.jellyfin_url}")
            return True
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to connect to Jellyfin: {e}")
            return False

    def get_artist_image_by_name(self, artist_name: str, timeout: float = 10.0) -> Optional[bytes]:
        if not artist_name:
            raise ValueError("artist_name is required")

        url = f"{self.config.jellyfin_url}/Artists/{artist_name}/Images/Primary/0"
        params = {"format": "webp", "maxWidth": 600, "maxHeight": 600}

        # Merge in an Accept header that prefers webp.
        headers = {"Accept": "image/webp,image/*;q=0.8,*/*;q=0.5"}

        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                self.logger.info("Artist not found or no primary image: %r", artist_name)
                return None
            resp.raise_for_status()

            ctype = resp.headers.get("Content-Type", "").lower()
            if "image" not in ctype:
                # Jellyfin should return an image; if not, log a short body preview
                preview = ""
                try:
                    preview = (resp.text or "")[:300]
                except Exception:
                    pass
                self.logger.error("Unexpected response (Content-Type=%s). Body: %s", ctype, preview)
                raise requests.exceptions.RequestException(
                    f"Unexpected response (Content-Type={ctype}). Body: {preview}"
                )
            return resp.content

        except requests.exceptions.HTTPError as e:
            # Keep an informative log (with short body preview, if any)
            body_preview = ""
            try:
                body_preview = (getattr(e.response, "text", "") or "")[:300]
            except Exception:
                pass
            self.logger.error("Jellyfin HTTP error for %s: %s | %s", url, e, body_preview)
            raise
        except requests.exceptions.RequestException as e:
            self.logger.error("Failed fetching artist image: %s", e)
            raise
    
    def get_users(self) -> List[Dict]:
        """Get all users from Jellyfin"""
        try:
            url = f"{self.config.jellyfin_url}/Users"
            response = self.session.get(url)
            response.raise_for_status()
            
            users = response.json()
            self.logger.info(f"Retrieved {len(users)} users from Jellyfin")
            return users
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching users: {e}")
            return []

    def get_playback_active_dates(self, user_id: str, days: int) -> List[str]:
        """Dates in the last `days` on which this user played anything, newest first.

        Raises RuntimeError when the Playback Reporting plugin is absent, so the caller
        can tell "no plugin" from "no listening", and returns [] for the latter.
        """
        url = f"{self.config.jellyfin_url}/user_usage_stats/PlayActivity"
        response = self.session.get(url, params={'days': days}, timeout=30)
        if response.status_code == 404:
            raise RuntimeError("Playback Reporting plugin not installed")
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, list):
            return []
        # One entry per user that has any activity; user_usage maps date -> play count.
        for entry in data:
            if entry.get('user_id') == user_id:
                usage = entry.get('user_usage') or {}
                return sorted((d for d, n in usage.items() if isinstance(n, (int, float)) and n > 0),
                              reverse=True)
        return []

    def get_user_play_counts(self, user_id: str, days: int, max_dates: int = 200) -> Dict[str, int]:
        """Play counts per item for one user over a window, from Playback Reporting.

        The plugin reports activity per user per day and the items played on one day, but
        has no endpoint that returns "most played tracks" in one call, so this walks only
        the days that actually have activity. Returns {} when the plugin is missing, when
        the user has no activity in the window, or on any error, which is what keeps the
        caller's favourites and recently-played fallbacks working unchanged.
        """
        counts: Dict[str, int] = {}
        try:
            active_dates = self.get_playback_active_dates(user_id, days)
        except RuntimeError as e:
            self.logger.info(f"📊 {e}, using favourites instead of play counts")
            return {}
        except Exception as e:
            self.logger.warning(f"📊 Playback Reporting unavailable: {e}")
            return {}

        if not active_dates:
            self.logger.info(f"📊 No playback activity in the last {days} days")
            return {}

        dates = active_dates[:max_dates]
        if len(active_dates) > max_dates:
            self.logger.info(f"📊 {len(active_dates)} active days in {days} days, reading the newest {max_dates}")

        base = f"{self.config.jellyfin_url}/user_usage_stats"
        for date in dates:
            try:
                response = self.session.get(f"{base}/{user_id}/{date}/GetItems", timeout=30)
                if response.status_code != 200:
                    continue
                # Each row is one play event: Id is the item, Type separates audio from video.
                for row in response.json() or []:
                    if (row.get('Type') or '') != 'Audio':
                        continue
                    item_id = row.get('Id')
                    if item_id:
                        counts[item_id] = counts.get(item_id, 0) + 1
            except Exception as e:
                self.logger.debug(f"📊 Could not read playback for {date}: {e}")

        self.logger.info(
            f"📊 Playback Reporting: {len(counts)} distinct tracks over "
            f"{len(dates)} active day(s) in the last {days} days"
        )
        return counts

    def get_user_favorite_items(self, user_id: str) -> List[Dict]:
        """Get user's favorite/liked items"""
        try:
            url = f"{self.config.jellyfin_url}/Users/{user_id}/Items"
            params = {
                'IsFavorite': 'true',
                'IncludeItemTypes': 'Audio',
                'Recursive': 'true',
                'Fields': 'Path,Genres,ProductionYear,Artists,RunTimeTicks,DateCreated,UserData'
            }
            
            response = self.session.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            items = data.get('Items', [])
            self.logger.info(f"Retrieved {len(items)} favorite tracks for user {user_id}")
            return items
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching user favorites: {e}")
            return []

    def get_recently_played(self, user_id: str, limit: int = 50) -> List[Dict]:
        """Get user's recently played tracks"""
        try:
            url = f"{self.config.jellyfin_url}/Users/{user_id}/Items"
            params = {
                'IncludeItemTypes': 'Audio',
                'Recursive': 'true',
                'SortBy': 'DatePlayed',
                'SortOrder': 'Descending',
                'Limit': limit,
                'Fields': 'Path,Genres,ProductionYear,Artists,RunTimeTicks,DateCreated,UserData'
            }
            
            response = self.session.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            items = data.get('Items', [])
            # Filter only items that have been played
            played_items = [item for item in items if item.get('UserData', {}).get('LastPlayedDate')]
            self.logger.info(f"Retrieved {len(played_items)} recently played tracks for user {user_id}")
            return played_items
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching recently played: {e}")
            return []

    def get_similar_tracks_by_genre(self, reference_tracks: List[Dict], all_tracks: List[Dict], limit: int = 50) -> List[Dict]:
        """Find similar tracks based on genre matching"""
        if not reference_tracks:
            return []
        
        # Extract genres from reference tracks
        reference_genres = set()
        for track in reference_tracks:
            if track.get('Genres'):
                if isinstance(track['Genres'], list):
                    reference_genres.update(track['Genres'])
                elif isinstance(track['Genres'], str):
                    reference_genres.add(track['Genres'])
        
        # Find tracks with matching genres
        similar_tracks = []
        reference_ids = {track.get('Id') for track in reference_tracks}
        
        for track in all_tracks:
            # Skip if it's already in reference tracks
            if track.get('Id') in reference_ids:
                continue
                
            track_genres = set()
            if track.get('Genres'):
                if isinstance(track['Genres'], list):
                    track_genres.update(track['Genres'])
                elif isinstance(track['Genres'], str):
                    track_genres.add(track['Genres'])
            
            # Calculate genre overlap
            genre_overlap = len(reference_genres.intersection(track_genres))
            if genre_overlap > 0:
                track['similarity_score'] = genre_overlap / len(reference_genres)
                similar_tracks.append(track)
        
        # Sort by similarity score and return top matches
        similar_tracks.sort(key=lambda x: x.get('similarity_score', 0), reverse=True)
        return similar_tracks[:limit]

    def create_playlist(self, name: str, track_ids: List[str], user_id: str = None, is_public: bool = True) -> Dict:
        """Create a playlist using Jellyfin's REST API"""
        self._refuse_write('create a playlist')
        try:
            # If no user_id provided, get the first available user
            if not user_id:
                users = self.get_users()
                if not users:
                    raise Exception("No users found in Jellyfin")
                user_id = users[0]['Id']
                self.logger.info(f"Using user {users[0]['Name']} ({user_id}) for playlist creation")
            
            # Create the playlist
            url = f"{self.config.jellyfin_url}/Playlists"
            payload = {
                "Name": name,
                "IsPublic": is_public,
                "Ids": track_ids,
                "UserId": user_id
            }
            
            privacy_text = "public" if is_public else "private"
            self.logger.info(f"Creating {privacy_text} playlist '{name}' with {len(track_ids)} tracks for user {user_id}")
            
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            
            playlist_data = response.json()
            playlist_id = playlist_data.get('Id')
            
            self.logger.info(f"Successfully created {privacy_text} playlist '{name}' with ID: {playlist_id}")
            
            return {
                'success': True,
                'playlist_id': playlist_id,
                'name': name,
                'track_count': len(track_ids),
                'user_id': user_id,
                'is_public': is_public
            }
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error creating playlist '{name}': {e}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response status: {e.response.status_code}")
                self.logger.error(f"Response text: {e.response.text}")
            return {
                'success': False,
                'error': str(e),
                'name': name
            }
        except Exception as e:
            self.logger.error(f"Unexpected error creating playlist '{name}': {e}")
            return {
                'success': False,
                'error': str(e),
                'name': name
            }

    def list_playlists(self, user_id: str = None) -> List[Dict]:
        """Every playlist in the library, with the path of its folder on disk."""
        try:
            if not user_id:
                users = self.get_users()
                if not users:
                    return []
                user_id = users[0]['Id']

            url = f"{self.config.jellyfin_url}/Users/{user_id}/Items"
            params = {
                'IncludeItemTypes': 'Playlist',
                'Recursive': 'true',
                'Fields': 'Path',
            }
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json().get('Items', [])
        except Exception as e:
            self.logger.warning(f"Could not list playlists: {e}")
            return []

    def find_playlists_by_name(self, name: str, user_id: str = None) -> List[Dict]:
        """Return every playlist whose name matches, so duplicates can be cleaned up.

        Listing with a SearchTerm can miss items and returns only the first hits, which
        is why a half-finished run can leave several playlists sharing one name. Listing
        all playlists and comparing exact normalized names is reliable, and playlists
        are few even on a large server.
        """
        try:
            if not user_id:
                users = self.get_users()
                if not users:
                    return []
                user_id = users[0]['Id']

            url = f"{self.config.jellyfin_url}/Users/{user_id}/Items"
            params = {
                'IncludeItemTypes': 'Playlist',
                'Recursive': 'true',
                # Path is needed to clean up the playlist's folder on disk, which
                # Jellyfin leaves behind for the scanner to re-add as an empty duplicate.
                'Fields': 'Path',
            }

            response = self.session.get(url, params=params)
            response.raise_for_status()

            # Normalize Unicode quotes/apostrophes before comparing
            target = normalize_name(name).lower()
            return [
                playlist for playlist in response.json().get('Items', [])
                if normalize_name(playlist.get('Name', '')).lower() == target
            ]

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error checking for existing playlist '{name}': {e}")
            return []

    def get_playlist_by_name(self, name: str, user_id: str = None) -> Dict:
        """Check if a playlist with the given name already exists"""
        matches = self.find_playlists_by_name(name, user_id)
        return matches[0] if matches else None

    def delete_playlist(self, playlist_id: str) -> bool:
        """Delete a playlist by ID"""
        self._refuse_write('delete a playlist')
        try:
            url = f"{self.config.jellyfin_url}/Items/{playlist_id}"
            response = self.session.delete(url)
            response.raise_for_status()
            
            self.logger.info(f"Successfully deleted playlist with ID: {playlist_id}")
            return True
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error deleting playlist {playlist_id}: {e}")
            return False

    def trigger_library_scan(self) -> bool:
        """Trigger a media library scan in Jellyfin to refresh playlists"""
        self._refuse_write('trigger a library scan')
        try:
            url = f"{self.config.jellyfin_url}/Library/Refresh"
            response = self.session.post(url)
            response.raise_for_status()
            
            self.logger.info("Successfully triggered Jellyfin media library scan")
            return True
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error triggering library scan: {e}")
            return False

# Spellings that mean the same genre. Keys and values are lower case.
GENRE_ALIASES = {
    'electronica': 'electronic',
    'drum and bass': 'drum & bass',
    'dnb': 'drum & bass',
    'hiphop': 'hip hop',
    'hip-hop': 'hip hop',
    'rnb': 'r&b',
    'r and b': 'r&b',
}

# Genres whose plain title case would look wrong.
GENRE_DISPLAY = {
    'r&b': 'R&B', 'edm': 'EDM', 'idm': 'IDM', 'dnb': 'DnB', 'uk garage': 'UK Garage',
    'drum & bass': 'Drum & Bass', 'hip hop': 'Hip Hop', 'k-pop': 'K-Pop', 'j-pop': 'J-Pop',
    'lo-fi': 'Lo-Fi', 'ebm': 'EBM',
}

# Credits that name no real artist, so they never get a "This is ...!" playlist.
NOT_A_REAL_ARTIST = {'various artists', 'various', 'va', 'unknown artist', 'unknown', '[unknown]', 'soundtrack'}


def display_genre(tag: str) -> str:
    """Playlist-friendly spelling of a genre tag: 'hip hop' -> 'Hip Hop', 'r&b' -> 'R&B'."""
    tag = (tag or '').strip()
    key = tag.lower()
    return GENRE_DISPLAY.get(key) or ' '.join(
        '-'.join(part[:1].upper() + part[1:] for part in word.split('-')) for word in key.split())


def parse_track_genres(item: Dict) -> List[str]:
    """Genres of one track, split on ';' where a tag holds several, without duplicates."""
    raw = item.get('Genres') or []
    if isinstance(raw, str):
        raw = [raw]
    seen, genres = set(), []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        for part in entry.split(';'):
            part = part.strip()
            if part and part.lower() not in seen:
                seen.add(part.lower())
                genres.append(part)
    return genres


def select_genre_playlists(config, audio_items: List[Dict], week: Optional[int] = None) -> List[Dict]:
    """Decide which genre playlists to make. Pure: reads the config, touches no server.

    Tags are compared without regard to case and aliases are merged, so 'Hip Hop' and
    'hip hop' are one genre. With grouping on, related tags fold into their group
    (Rock, Pop, Hip Hop ...). A group that holds more than genre_split_share of the whole
    library is too broad to be one playlist, so its members stand on their own instead:
    that is what turns a house-heavy library into House, Progressive House, Deep House
    and so on rather than one giant Electronic.

    The biggest max_genre_playlists genres are always made. genre_spotlight_slots more
    are taken from the rest and rotate with the ISO week number, so the long tail still
    gets its turn. Returns [{'genre', 'tracks', 'artists', 'kind'}], best first.
    """
    excluded = {g.strip().lower() for g in (config.excluded_genres or []) if g and g.strip()}
    excluded_artists = set(config.excluded_artists or [])
    grouping = bool(config.genre_grouping_enabled)

    tag_tracks: Dict[str, Dict[str, Dict]] = {}
    tag_names: Dict[str, str] = {}
    all_tracks = set()
    for item in audio_items:
        if excluded_artists and any(a in excluded_artists for a in (item.get('Artists') or [])):
            continue
        track_id = item.get('Id')
        if not track_id:
            continue
        for tag in parse_track_genres(item):
            key = GENRE_ALIASES.get(tag.lower(), tag.lower())
            if key in excluded or tag.lower() in excluded:
                continue
            tag_tracks.setdefault(key, {})[track_id] = item
            tag_names.setdefault(key, tag)
            all_tracks.add(track_id)

    def group_key(key: str) -> str:
        return config.map_genre_to_group(key).lower() if grouping else key

    groups: Dict[str, List[str]] = {}
    for key in tag_tracks:
        groups.setdefault(group_key(key), []).append(key)

    group_labels = {name.lower(): name for name in (config.genre_mappings or {})}
    candidates: Dict[str, Dict] = {}

    def add(label_key: str, label: str, pool: Dict[str, Dict]):
        entry = candidates.setdefault(label_key, {'genre': label, 'pool': {}})
        entry['pool'].update(pool)

    for gkey, members in groups.items():
        pool: Dict[str, Dict] = {}
        for key in members:
            pool.update(tag_tracks[key])
        too_broad = (grouping and len(members) > 1 and all_tracks
                     and len(pool) > config.genre_split_share * len(all_tracks))
        if too_broad:
            for key in members:
                add(key, display_genre(key), tag_tracks[key])
        else:
            add(gkey, group_labels.get(gkey) or display_genre(gkey), pool)

    qualifying = []
    for entry in candidates.values():
        tracks = list(entry['pool'].values())
        artists = {a for t in tracks for a in (t.get('Artists') or [])}
        if len(tracks) >= config.min_tracks_per_playlist and len(artists) >= config.min_artist_diversity:
            qualifying.append({'genre': entry['genre'], 'tracks': tracks, 'artists': len(artists)})

    qualifying.sort(key=lambda c: (-len(c['tracks']), -c['artists'], c['genre'].lower()))
    top = qualifying[:config.max_genre_playlists]
    for entry in top:
        entry['kind'] = 'top'

    # A spotlight is a treat, not filler: the long tail only qualifies when it holds enough
    # tracks to make a playlist worth opening.
    spotlight_min = config.min_tracks_per_playlist * 5
    rest = sorted((c for c in qualifying[config.max_genre_playlists:] if len(c['tracks']) >= spotlight_min),
                  key=lambda c: c['genre'].lower())
    slots = min(config.genre_spotlight_slots, len(rest))
    spotlight = []
    if slots > 0:
        if week is None:
            week = datetime.now().isocalendar()[1]
        start = (week * slots) % len(rest)
        spotlight = [rest[(start + i) % len(rest)] for i in range(slots)]
        for entry in spotlight:
            entry['kind'] = 'spotlight'
    return top + spotlight


def select_artist_playlists(config, artist_data: Dict[str, Dict], limit: int) -> List[str]:
    """The artists that get a "This is <artist>!" playlist: biggest first, at most `limit`.

    artist_data maps an artist to {'tracks': [...], 'albums': set(...)}.
    """
    excluded = set(config.excluded_artists or [])
    eligible = []
    for artist, data in artist_data.items():
        if artist in excluded or artist.strip().lower() in NOT_A_REAL_ARTIST:
            continue
        if len(data['tracks']) < config.min_tracks_per_playlist:
            continue
        if len(data['albums']) < config.min_albums_per_artist:
            continue
        eligible.append(artist)
    eligible.sort(key=lambda a: (-len(artist_data[a]['tracks']), -len(artist_data[a]['albums']), a.lower()))
    return eligible[:max(0, limit)]


class PlaylistGenerator:
    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger
        # What a dry run found: one entry per playlist this run would create, replace
        # or skip. Stays empty on a normal run.
        self.dry_run_plan: List[Dict] = []
        # Genre, decade and artist playlists chosen so far this run, for max_total_playlists
        self.shared_planned = 0
        # Every playlist name this run set out to make, whether or not the create worked
        self.planned_names = set()
        self.jellyfin = JellyfinAPI(config, logger)
        self.spotify = SpotifyClient(config, logger)
        self.deezer = DeezerClient(config, logger)
        # Number of playlists written by the current generation pass
        self.created_playlists = 0
        # Add caching for API queries to prevent repeated expensive calls
        self._artist_path_cache = {}
        self._audio_items_cache = None
        self._cache_timestamp = None

    def _get_cached_audio_items(self) -> List[Dict]:
        """Get audio items with caching to prevent repeated expensive API calls"""
        import time
        current_time = time.time()
        
        # Cache for 30 minutes during cover art updates to prevent repeated API calls
        cache_duration = 1800  # 30 minutes for cover art operations
        
        if (self._audio_items_cache is None or 
            self._cache_timestamp is None or 
            current_time - self._cache_timestamp > cache_duration):
            
            self.logger.debug("📡 Fetching fresh audio items from Jellyfin API")
            self._audio_items_cache = self.jellyfin.get_audio_items()
            self._cache_timestamp = current_time
            self.logger.info(f"📋 Cached {len(self._audio_items_cache)} audio items for {cache_duration//60} minutes")
        else:
            self.logger.debug(f"📋 Using cached audio items ({len(self._audio_items_cache)} items, cached {int((current_time - self._cache_timestamp)/60)} minutes ago)")
        
        return self._audio_items_cache

    def _playlist_track_ids(self, playlist_id: str, user_id: str) -> List[str]:
        """Track ids currently in a playlist, used to restore it if a replace fails.

        Shared playlists are made without a user id, and Jellyfin refuses to list a
        playlist without one ("Guid can't be empty"), which silently disabled the
        restore. Any user can read a shared playlist, so the first user stands in.
        """
        try:
            if not user_id:
                if getattr(self, '_reader_user_id', None) is None:
                    users = self.jellyfin.get_users()
                    self._reader_user_id = users[0]['Id'] if users else ''
                user_id = self._reader_user_id
            if not user_id:
                self.logger.warning(f"Cannot read playlist {playlist_id} before replacing it: no Jellyfin user found")
                return []
            url = f"{self.jellyfin.config.jellyfin_url}/Playlists/{playlist_id}/Items"
            response = self.jellyfin.session.get(url, params={'UserId': user_id})
            response.raise_for_status()
            return [item['Id'] for item in response.json().get('Items', []) if item.get('Id')]
        except Exception as e:
            self.logger.warning(f"Could not read tracks of playlist {playlist_id} before replacing it: {e}")
            return []

    def _remove_playlist_folder(self, jellyfin_path: str):
        """Delete a playlist's folder, mapping Jellyfin's path onto ours.

        Jellyfin reports its own container path (/config/data/data/playlists/<name>)
        while this container mounts the same folder at /playlists, so only the final
        path segment travels. A path that does not resolve to a direct child of the
        playlist folder is left alone.
        """
        try:
            name = os.path.basename(str(jellyfin_path or '').rstrip('/'))
            if not name:
                return
            root = Path(self.config.playlist_folder).resolve()
            target = (root / name).resolve()
            if target.parent != root:
                self.logger.warning(f"Refusing to remove unexpected path: {target}")
                return
            if target.is_dir():
                shutil.rmtree(target)
                self.logger.debug(f"Removed playlist folder: {target}")
        except Exception as e:
            self.logger.debug(f"Could not remove playlist folder {jellyfin_path}: {e}")

    def adopt_existing_playlists(self) -> int:
        """Take over playlists that carry a generated name but are not in the record.

        Off unless ADOPT_EXISTING_PLAYLISTS=true. A name is no proof that JellyJams made
        a playlist, so nothing is claimed by default: a playlist that is not in
        managed_playlists.json is left alone. Turn it on for one run when upgrading from
        a version that kept no record. Only generated names are adopted, so a hand-made
        playlist is never claimed, and every adoption is logged.
        """
        if not self.config.adopt_existing_playlists or self.config.dry_run:
            return 0

        managed = load_managed_playlists()
        adopted = 0
        for item in self.jellyfin.list_playlists():
            name = item.get('Name')
            if name and is_generated_playlist_name(name) and managed.get(name) != item.get('Id'):
                managed[name] = item.get('Id')
                adopted += 1
                self.logger.info(f"📋 Adopted existing playlist '{name}' (id {item.get('Id')})")

        if adopted:
            save_managed_playlists(managed)
            self.logger.info(f"📋 Adopted {adopted} playlist(s). Turn ADOPT_EXISTING_PLAYLISTS off again.")
        return adopted

    def _record(self) -> Dict[str, str]:
        """The managed-playlist record as this run sees it.

        In a dry run with ADOPT_EXISTING_PLAYLISTS on, the adoption that a real run would
        do is shown as already done, so the preview matches what the real run does.
        """
        managed = load_managed_playlists()
        if self.config.dry_run and self.config.adopt_existing_playlists:
            for item in self.jellyfin.list_playlists():
                name = item.get('Name')
                if name and is_generated_playlist_name(name) and name not in managed:
                    managed[name] = item.get('Id')
        return managed

    def _classify_existing(self, name: str, user_id: str):
        """Split the playlists called `name` into (ours, foreign). Reads only.

        Ours means the playlist id is the one JellyJams recorded when it created it.
        Everything else with that name, including a hand-made playlist, is foreign.
        """
        managed = self._record()
        ours, foreign = [], []
        for item in self.jellyfin.find_playlists_by_name(name, user_id):
            (ours if managed.get(name) == item.get('Id') else foreign).append(item)
        return ours, foreign

    def dry_run_report(self) -> Dict:
        """What a dry run would do, plus generated-looking playlists it will not touch."""
        managed_ids = set(self._record().values())
        unrecorded = sorted(
            item.get('Name') for item in self.jellyfin.list_playlists()
            if is_generated_playlist_name(item.get('Name', '')) and item.get('Id') not in managed_ids
        )
        return {'plan': list(self.dry_run_plan), 'unrecorded_generated': unrecorded,
                'retire': self.plan_retirement()}

    # The playlist kinds a run can retire, and the playlist type setting that makes them.
    RETIRABLE_TYPES = {'genre': 'Genre', 'year': 'Year', 'artist': 'Artist'}

    def plan_retirement(self) -> List[Dict]:
        """Playlists JellyJams made earlier that this run no longer generates. Reads only.

        A playlist is on the list only when all of these hold: its id is in the record,
        its name is a generated one, it is a genre, decade or artist playlist whose type
        is switched on for this run (a type you turned off is not touched), this run did
        not plan to make it, and Jellyfin still shows that id under that very name (a
        playlist you renamed since is yours now). Personal mixes are never retired.
        """
        enabled = set(self.config.playlist_types or [])
        # A kind this run made nothing of (a library that lost its tags, a cap of zero) is
        # never wiped: that looks like a broken run, not a wish to delete everything.
        made = {categorize_playlist(n) for n in self.planned_names}
        record = self._record()
        by_id = {item.get('Id'): item for item in self.jellyfin.list_playlists()}
        stale = []
        for name, playlist_id in sorted(record.items()):
            kind = categorize_playlist(name)
            if (self.RETIRABLE_TYPES.get(kind) not in enabled or kind not in made
                    or normalize_name(name) in self.planned_names):
                continue
            item = by_id.get(playlist_id)
            if not item or normalize_name(item.get('Name', '')) != normalize_name(name):
                continue
            stale.append({'name': name, 'id': playlist_id, 'kind': kind, 'path': item.get('Path')})
        return stale

    def retire_stale_playlists(self) -> int:
        """Delete the playlists plan_retirement lists, and forget them in the record."""
        if self.config.dry_run:
            return 0
        retired = 0
        managed = load_managed_playlists()
        for entry in self.plan_retirement():
            if not is_generated_playlist_name(entry['name']):      # belt and braces
                continue
            if self.jellyfin.delete_playlist(entry['id']):
                self._remove_playlist_folder(entry.get('path'))
                managed.pop(entry['name'], None)
                retired += 1
                self.logger.info(f"🧹 Retired '{entry['name']}' (id {entry['id']}): no longer generated")
            else:
                self.logger.warning(f"⚠️ Could not retire '{entry['name']}' (id {entry['id']})")
        if retired:
            save_managed_playlists(managed)
        return retired

    def _log_dry_run_report(self):
        report = self.dry_run_report()
        by_action: Dict[str, List[Dict]] = {'create': [], 'replace': [], 'skip': []}
        for entry in report['plan']:
            by_action[entry['action']].append(entry)

        lines = ["", "================ JellyJams DRY RUN: nothing was changed ================"]
        for action, label in (('create', 'Would CREATE'), ('replace', 'Would REPLACE (created by JellyJams)'),
                              ('skip', 'Would SKIP (a playlist of that name exists that JellyJams did not create)')):
            entries = by_action[action]
            lines.append(f"{label}: {len(entries)}")
            lines.extend(f"    {e['name']}  [{e['type']}, {e['tracks']} tracks]" for e in entries)
        retire = report['retire']
        lines.append(f"Would RETIRE (made by JellyJams, no longer generated): {len(retire)}")
        lines.extend(f"    {e['name']}" for e in retire)
        if retire:
            lines.append("    (removed only by a real run with --retire-stale or RETIRE_STALE_PLAYLISTS=true)")
        unrecorded = report['unrecorded_generated']
        lines.append(f"Generated-looking playlists JellyJams has no record of, left alone: {len(unrecorded)}")
        lines.extend(f"    {name}" for name in unrecorded)
        if unrecorded:
            lines.append("    (set ADOPT_EXISTING_PLAYLISTS=true for one run to take these over)")
        lines.append("========================================================================")
        for line in lines:
            self.logger.info(line)

    def _remove_existing_playlists(self, name: str, user_id: str):
        """Remove our own playlist with this name and its folder.

        Returns (rollback_track_ids, skip). Only a playlist whose id JellyJams recorded
        is removed. One made by hand is left alone even when it shares a name with a
        generated one, and the replacement is skipped rather than left as a duplicate.

        Jellyfin derives the playlist folder from the name and appends a number when that
        name is taken, so the name has to be freed before the create or it lands in
        "Name1". The folder also outlives the item and is re-scanned into an empty
        duplicate, so it is removed too. The returned track ids let a failed create be
        rolled back, so the playlist is never simply lost.
        """
        if not is_generated_playlist_name(name):
            self.logger.warning(f"Refusing to manage '{name}': JellyJams does not generate that name")
            return [], True

        ours, foreign = self._classify_existing(name, user_id)

        if foreign:
            self.logger.warning(
                f"⚠️ A playlist named '{name}' exists but was not created by JellyJams "
                f"(it is not in the record). Leaving it untouched and skipping that name. "
                f"Rename or remove it if JellyJams should manage the name again, or set "
                f"ADOPT_EXISTING_PLAYLISTS=true for one run."
            )
            return [], True

        if not ours:
            self.logger.info(f"ℹ️ No existing playlist found with name: {name}")
            return [], False

        self.logger.info(f"Found {len(ours)} existing playlist(s) named '{name}'")

        rollback_tracks: List[str] = []
        for item in ours:
            ids = self._playlist_track_ids(item.get('Id'), user_id)
            if len(ids) > len(rollback_tracks):
                rollback_tracks = ids

        for item in ours:
            playlist_id = item.get('Id')
            if self.jellyfin.delete_playlist(playlist_id):
                self.logger.info(f"🧹 Removed previous playlist '{name}' (id {playlist_id})")
            else:
                self.logger.warning(f"⚠️ Could not remove previous playlist '{name}' (id {playlist_id})")
            self._remove_playlist_folder(item.get('Path'))

        return rollback_tracks, False

    def _select_tracks(self, tracks: List[Dict]) -> List[Dict]:
        """Choose which tracks go into one playlist.

        Upstream took the first N tracks in library order and then shuffled them, so a
        genre playlist held exactly the same songs on every run and the rest of the
        catalogue never appeared. With random_track_selection on (the default) a random
        sample is taken instead, so a daily run rotates what gets played. Turn it off to
        keep the old deterministic behaviour.
        """
        limit = self.config.max_tracks_per_playlist
        if len(tracks) <= limit:
            selected = list(tracks)
        elif getattr(self.config, 'random_track_selection', True):
            selected = random.sample(tracks, limit)
        else:
            selected = list(tracks[:limit])
        if self.config.shuffle_tracks:
            random.shuffle(selected)
        return selected

    def copy_custom_cover_art(self, playlist_name: str, playlist_dir: Path) -> bool:
        """Copy custom cover art from /app/cover/ directory with fallback system and extension preservation"""
        try:
            cover_source_dir = Path(COVER_SOURCE_DIR)
            self.logger.info(f"Looking for custom cover art for playlist: {playlist_name}")
            self.logger.info(f"Checking cover source directory: {cover_source_dir}")
            
            if not cover_source_dir.exists():
                self.logger.warning(f"Cover source directory does not exist: {cover_source_dir}")
                return False
            
            # List all files in cover directory for debugging
            try:
                cover_files = list(cover_source_dir.glob("*"))
                self.logger.info(f"Found {len(cover_files)} files in cover directory: {[f.name for f in cover_files[:10]]}")  # Show first 10
            except Exception as e:
                self.logger.warning(f"Could not list cover directory contents: {e}")
            
            # Look for cover image with playlist name (try common extensions)
            extensions = ['.jpg', '.jpeg', '.png', '.webp', '.avif', '.bmp']
            source_image = None
            found_extension = None
            
            # First, try exact playlist name match
            self.logger.info(f"Trying exact match for: {playlist_name}")
            for ext in extensions:
                potential_file = cover_source_dir / f"{playlist_name}{ext}"
                self.logger.debug(f"Checking: {potential_file}")
                if potential_file.exists():
                    source_image = potential_file
                    found_extension = ext
                    self.logger.info(f"Found exact match cover art: {potential_file}")
                    break
            
            # If no exact match, try fallback with playlist type + "all"
            if not source_image:
                self.logger.info(f"No exact match found, trying fallback patterns...")
                # Extract playlist type for fallback (e.g., "Top Tracks - all", "Discovery Mix - all")
                fallback_patterns = []
                
                if "Top Tracks -" in playlist_name:
                    fallback_patterns.append("Top Tracks - all")
                elif "Discovery Mix -" in playlist_name:
                    fallback_patterns.append("Discovery Mix - all")
                elif "Recent Favorites -" in playlist_name:
                    fallback_patterns.append("Recent Favorites - all")
                elif "Genre Mix -" in playlist_name:
                    fallback_patterns.append("Genre Mix - all")
                elif "This is" in playlist_name:
                    fallback_patterns.append("This is - all")
                elif "Radio" in playlist_name:
                    fallback_patterns.append("Radio - all")
                elif "Back to" in playlist_name:
                    fallback_patterns.append("Back to - all")
                
                self.logger.info(f"Trying fallback patterns: {fallback_patterns}")
                
                # Try fallback patterns
                for pattern in fallback_patterns:
                    for ext in extensions:
                        potential_file = cover_source_dir / f"{pattern}{ext}"
                        self.logger.debug(f"Checking fallback: {potential_file}")
                        if potential_file.exists():
                            source_image = potential_file
                            found_extension = ext
                            self.logger.info(f"Found fallback cover art: {potential_file}")
                            break
                    if source_image:
                        break
            
            if not source_image:
                self.logger.info(f"No custom cover art found for playlist: {playlist_name} (tried exact match and fallback)")
                # Try artist folder fallback for artist playlists
                if self._try_artist_folder_fallback(playlist_name, playlist_dir):
                    return True
                return False
            
            # Copy and rename to folder.[original_extension] in the playlist directory
            # Preserve the original extension but rename to "folder"
            destination_filename = f"folder{found_extension}"
            destination_image = playlist_dir / destination_filename
            
            self.logger.info(f"Copying cover art: {source_image} -> {destination_image}")
            
            import shutil
            shutil.copy2(source_image, destination_image)
            
            # Ensure cover art is world-readable on host mounts
            try:
                os.chmod(destination_image, 0o664)
            except Exception as chmod_err:
                self.logger.debug(f"chmod failed for {destination_image}: {chmod_err}")
            
            self.logger.info(f"Successfully copied custom cover art: {source_image} -> {destination_image}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error copying custom cover art for {playlist_name}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def _apply_decade_cover_art(self, playlist_name: str, playlist_dir: Path) -> bool:
        """Apply decade-specific cover art for decade playlists with fallback system"""
        try:
            # Extract decade from playlist name (e.g., "Back to the 1980s" -> "1980s")
            if "Back to the" not in playlist_name:
                self.logger.warning(f"Invalid decade playlist name format: {playlist_name}")
                return False
            
            decade = playlist_name.replace("Back to the ", "").strip()
            self.logger.info(f"🗓️ Looking for decade-specific cover art for: {decade}")
            
            cover_source_dir = Path(COVER_SOURCE_DIR)
            
            if not cover_source_dir.exists():
                self.logger.warning(f"Cover source directory does not exist: {cover_source_dir}")
                return False
            
            # Look for decade-specific cover art files in multiple naming formats
            # playlist_name is like "Back to the 1990s.jpg"
            # Decade-only format is like "1990s-cover.jpg"
            names = [playlist_name, f"{decade}-cover"]
            exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif']
            
            source_image = None
            for base in names:
                for ext in exts:
                    candidate = cover_source_dir / f"{base}{ext}"
                    if candidate.exists() and candidate.is_file():
                        source_image = candidate
                        self.logger.info(f"🖼️ Found decade cover art: {source_image}")
                        break
            
            # If no specific decade cover found, try fallback for pre-1900s music
            if not source_image and decade.endswith('s'):
                try:
                    decade_year = int(decade[:-1])  # Remove 's' and convert to int
                    if decade_year < 1900:
                        self.logger.info(f"🕰️ Decade {decade} is before 1900s, trying 1800s fallback...")
                        for ext in exts:
                            potential_fallback = cover_source_dir / f"1800s-cover{ext}"
                            if potential_fallback.exists() and potential_fallback.is_file():
                                source_image = potential_fallback
                                self.logger.info(f"🖼️ Found 1800s fallback cover art: {source_image}")
                                break
                except ValueError:
                    self.logger.warning(f"Could not parse decade year from: {decade}")
            
            if not source_image:
                self.logger.info(f"❌ No decade-specific cover art found for {decade}")
                return False
            
            # Determine destination filename (preserve original extension)
            source_extension = source_image.suffix
            destination_filename = f"cover{source_extension}"
            destination_image = playlist_dir / destination_filename
            
            self.logger.info(f"📋 Copying decade cover art: {source_image} -> {destination_image}")
            
            import shutil
            shutil.copy2(source_image, destination_image)
            
            # Ensure cover art is world-readable on host mounts
            try:
                os.chmod(destination_image, 0o664)
            except Exception as chmod_err:
                self.logger.debug(f"chmod failed for {destination_image}: {chmod_err}")
            
            self.logger.info(f"✅ Successfully applied decade cover art: {source_image} -> {destination_image}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error applying decade cover art for {playlist_name}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def _apply_genre_cover_art(self, playlist_name: str, genre_name: str, playlist_dir: Path) -> bool:
        """Apply genre-specific cover art with hybrid predefined/generated approach"""
        try:
            self.logger.info(f"🎵 Looking for genre cover art for: {genre_name}")
            
            cover_source_dir = Path(COVER_SOURCE_DIR)
            
            if not cover_source_dir.exists():
                self.logger.warning(f"Cover source directory does not exist: {cover_source_dir}")
                return False
            
            # First, try to find predefined genre cover art. Case does not matter: genre
            # names come from the library's tags, and file names on Linux are case sensitive.
            exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif']
            source_image = find_cover(cover_source_dir, [f"{genre_name} Radio", genre_name])
            if source_image:
                self.logger.info(f"🖼️ Found predefined genre cover art: {source_image}")
            
            # If predefined cover found, copy it directly
            if source_image:
                source_extension = source_image.suffix
                destination_filename = f"cover{source_extension}"
                destination_image = playlist_dir / destination_filename
                
                self.logger.info(f"📋 Copying predefined genre cover art: {source_image} -> {destination_image}")
                
                import shutil
                shutil.copy2(source_image, destination_image)
                
                # Ensure cover art is world-readable on host mounts
                try:
                    os.chmod(destination_image, 0o664)
                except Exception as chmod_err:
                    self.logger.debug(f"chmod failed for {destination_image}: {chmod_err}")
                
                self.logger.info(f"✅ Successfully applied predefined genre cover art: {source_image} -> {destination_image}")
                return True
            
            # If no predefined cover found, generate one using "Fallback Radio.jpg" background
            self.logger.info(f"🎨 No predefined cover found, generating custom genre cover...")
            
            background_image = None
            for ext in exts:
                potential_bg = cover_source_dir / f"Fallback Radio{ext}"
                if potential_bg.exists() and potential_bg.is_file():
                    background_image = potential_bg
                    self.logger.info(f"🖼️ Found background template: {background_image}")
                    break
            
            if not background_image:
                self.logger.warning(f"❌ No 'Fallback Radio' background template found for genre cover generation")
                return False
            
            # Generate custom genre cover with text overlay
            destination_image = playlist_dir / "cover.webp"
            success = self._generate_genre_cover_art(background_image, genre_name, destination_image)
            
            if success:
                self.logger.info(f"✅ Successfully generated genre cover art for: {genre_name}")
                return True
            else:
                self.logger.warning(f"❌ Failed to generate genre cover art for: {genre_name}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error applying genre cover art for {playlist_name}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def _generate_genre_cover_art(self, background_path: Path, genre_name: str, destination: Path) -> bool:
        """Draw a cover for a genre that has none shipped: see genre_cover.render_genre_cover"""
        try:
            render_genre_cover(background_path, genre_name, destination)
            self.logger.info(f"✅ Generated genre cover art: {destination}")
            return True
        except Exception as e:
            self.logger.error(f"Error generating genre cover art for {genre_name}: {e}")
            return False
    
    def _try_artist_folder_fallback(self, playlist_name: str, playlist_dir: Path) -> bool:
        """Generate custom cover art with 'This is <artist>' text overlay from artist images"""
        try:
            # Only apply this fallback for artist playlists
            if not playlist_name.startswith("This is "):
                return False
            
            # Extract artist name from playlist name ("This is Artist!" -> "Artist")
            artist_name = playlist_name.replace("This is ", "").rstrip("!")
            self.logger.info(f"Generating custom cover art for: {artist_name}")
            
            # Find artist folder and source image
            source_cover = self._find_artist_cover_image(artist_name)
            if source_cover is None:
                return False
            
            # Generate custom cover art with text overlay
            destination_cover = playlist_dir / "folder.webp"
            success = self._generate_custom_cover_art(source_cover, artist_name, destination_cover)
            
            if success:
                self.logger.info(f"✅ Successfully generated custom cover art: {destination_cover}")
                return True
            else:
                # Fallback to simple copy if text overlay fails
                self.logger.warning("Text overlay failed, falling back to simple copy")
                fallback_destination = playlist_dir / "folder.webp"
                Image.open(BytesIO(source_cover)).save(fallback_destination)
                
                # Ensure cover art is world-readable on host mounts
                try:
                    os.chmod(fallback_destination, 0o664)
                except Exception as chmod_err:
                    self.logger.debug(f"chmod failed for {fallback_destination}: {chmod_err}")
                
                self.logger.info(f"✅ Fallback: copied artist folder cover art: {fallback_destination}")
                return True
            
        except Exception as e:
            self.logger.error(f"❌ Error in artist folder fallback for {playlist_name}: {e}")
            import traceback
            self.logger.debug(f"Full traceback: {traceback.format_exc()}")
            return False
    
    def _find_artist_cover_image(self, artist_name: str) -> Optional[bytes]:
        """First, try getting artist image from Jellyfin API"""
        artist_image = self.jellyfin.get_artist_image_by_name(artist_name)
        if not (artist_image is None):
            return artist_image

        """Else, search artist folder for image"""
        self.logger.debug(f"🔍 Searching for artist folder: {artist_name}")
        # First try to get artist info from Jellyfin API
        artist_path = self._get_artist_path_from_jellyfin(artist_name)
        if artist_path:
            self.logger.debug(f"📡 Got artist path from Jellyfin API: {artist_path}")
            return self._find_cover_in_directory(artist_path)
        return None
    
    def _get_artist_path_from_jellyfin(self, artist_name: str) -> Path:
        """Get artist folder path from Jellyfin API with optimized caching"""
        try:
            # Check cache first to avoid repeated expensive API calls
            if artist_name in self._artist_path_cache:
                cached_result = self._artist_path_cache[artist_name]
                if cached_result is not None:
                    self.logger.debug(f"📋 Using cached path for artist: {artist_name} -> {cached_result}")
                else:
                    self.logger.debug(f"📋 Using cached negative result for artist: {artist_name}")
                return cached_result
            
            self.logger.debug(f"📡 Looking up artist path for: {artist_name}")
            
            # Get audio items from Jellyfin with caching - this should only happen once per session now
            audio_items = self._get_cached_audio_items()
            
            # Look for tracks by this artist and extract the path
            for item in audio_items:
                if 'Artists' in item and isinstance(item['Artists'], list):
                    for artist in item['Artists']:
                        if artist.lower() == artist_name.lower():
                            # Extract the directory path from the file path
                            file_path = item.get('Path')
                            if file_path:
                                # Get the parent directory (should be artist folder)
                                file_path_obj = Path(file_path)
                                # Go up directories to find artist folder
                                # Typical structure: /music/Artist/Album/Track.mp3
                                potential_artist_dir = file_path_obj.parent.parent
                                if potential_artist_dir.name.lower() == artist_name.lower():
                                    self.logger.debug(f"📁 Found artist directory from track path: {potential_artist_dir}")
                                    self._artist_path_cache[artist_name] = potential_artist_dir
                                    self.logger.debug(f"📡 Got artist path from Jellyfin API: {potential_artist_dir}")
                                    return potential_artist_dir
                                # Sometimes it might be: /music/Artist/Track.mp3
                                potential_artist_dir = file_path_obj.parent
                                if potential_artist_dir.name.lower() == artist_name.lower():
                                    self.logger.debug(f"📁 Found artist directory from track path: {potential_artist_dir}")
                                    self._artist_path_cache[artist_name] = potential_artist_dir
                                    self.logger.debug(f"📡 Got artist path from Jellyfin API: {potential_artist_dir}")
                                    return potential_artist_dir
        
            self.logger.debug(f"❌ No artist folder found for: {artist_name}")
            # Cache negative results too to prevent repeated failed lookups
            self._artist_path_cache[artist_name] = None
        
        except Exception as e:
            self.logger.debug(f"Error getting artist path from Jellyfin API: {e}")
            # Cache negative results for errors too
            self._artist_path_cache[artist_name] = None

        return None
    
    def _find_cover_in_directory(self, directory_path: Path) -> Optional[bytes]:
        pattern = re.compile(r"^(folder|cover|artist|thumb|front)\.(jpg|jpeg|png|webp|avif|bmp)$", re.IGNORECASE)
        for root, dirs, files in os.walk(directory_path):
            for fname in files:
                if pattern.match(fname):
                    self.logger.info(f"🖼️ Found cover art: {Path(root) / fname}")
                    return (Path(root) / fname).read_bytes()
        self.logger.debug(f"No cover art files found in: {directory_path}")
        return None
    
    def _sanitize_text_for_font(self, text: str) -> str:
        """Sanitize text to handle Unicode characters that cause font encoding errors"""
        try:
            # Replace common Unicode characters that cause font issues
            replacements = {
                '\u2010': '-',  # Unicode hyphen → ASCII hyphen
                '\u2011': '-',  # Non-breaking hyphen → ASCII hyphen
                '\u2012': '-',  # Figure dash → ASCII hyphen
                '\u2013': '-',  # En dash → ASCII hyphen
                '\u2014': '-',  # Em dash → ASCII hyphen
                '\u2015': '-',  # Horizontal bar → ASCII hyphen
                '\u2018': "'", # Left single quotation mark → ASCII apostrophe
                '\u2019': "'", # Right single quotation mark → ASCII apostrophe
                '\u201C': '"',  # Left double quotation mark → ASCII quote
                '\u201D': '"',  # Right double quotation mark → ASCII quote
                '\u00A0': ' ',  # Non-breaking space → regular space
            }
            
            sanitized = text
            for unicode_char, replacement in replacements.items():
                sanitized = sanitized.replace(unicode_char, replacement)
            
            # Try to encode as latin-1 to catch any remaining problematic characters
            try:
                sanitized.encode('latin-1')
                return sanitized
            except UnicodeEncodeError:
                # If still problematic, remove non-ASCII characters
                sanitized = ''.join(char for char in sanitized if ord(char) < 128)
                self.logger.warning(f"Removed non-ASCII characters from artist name: {text} → {sanitized}")
                return sanitized
                
        except Exception as e:
            self.logger.warning(f"Error sanitizing text '{text}': {e}")
            # Fallback: remove all non-ASCII characters
            return ''.join(char for char in text if ord(char) < 128)
    
    def _generate_custom_cover_art(self, source_image: bytes, artist_name: str, destination: Path) -> bool:
        """Artist cover: the artist's photo in the artist's own colour, name on top.

        Same treatment as the genre covers (see genre_cover.render_genre_cover), drawn with
        the bundled font. Replaces the old renderer, which looked for system fonts the
        Alpine image does not ship and computed a font size for a canvas twice as large.
        """
        tmp_bg = Path(str(destination) + '.bg.jpg')
        tmp_out = destination.with_suffix(destination.suffix + '.tmp' + destination.suffix)
        try:
            with Image.open(BytesIO(source_image)) as source:
                source.convert('RGB').save(tmp_bg, 'JPEG', quality=90)
            render_artist_cover(tmp_bg, artist_name, tmp_out)
            os.replace(tmp_out, destination)
            self.logger.info(f"✅ Generated artist cover art: {destination}")
            return True
        except Exception as e:
            self.logger.error(f"Error generating artist cover art for {artist_name}: {e}")
            return False
        finally:
            for junk in (tmp_bg, tmp_out):
                try:
                    junk.unlink()
                except OSError:
                    pass
    
    def _apply_discovery_diversity_controls(self, tracks: List[Dict]) -> List[Dict]:
        """Apply diversity controls to discovery playlist: limit songs per album and per artist"""
        try:
            album_counts = {}
            artist_counts = {}
            diverse_tracks = []
            
            self.logger.info(f"Applying diversity controls: max {self.config.discovery_max_songs_per_album} per album, {self.config.discovery_max_songs_per_artist} per artist")
            
            for track in tracks:
                # Get album and artist info
                album = track.get('Album', 'Unknown Album')
                artists = track.get('Artists', ['Unknown Artist'])
                
                # Check album limit
                if album_counts.get(album, 0) >= self.config.discovery_max_songs_per_album:
                    continue
                
                # Check artist limits for all artists on this track
                artist_limit_exceeded = False
                for artist in artists:
                    if artist_counts.get(artist, 0) >= self.config.discovery_max_songs_per_artist:
                        artist_limit_exceeded = True
                        break
                
                if artist_limit_exceeded:
                    continue
                
                # Track passes diversity checks, add it
                diverse_tracks.append(track)
                
                # Update counts
                album_counts[album] = album_counts.get(album, 0) + 1
                for artist in artists:
                    artist_counts[artist] = artist_counts.get(artist, 0) + 1
            
            self.logger.info(f"Diversity filtering: {len(tracks)} -> {len(diverse_tracks)} tracks (removed {len(tracks) - len(diverse_tracks)} for diversity)")
            return diverse_tracks
            
        except Exception as e:
            self.logger.error(f"Error applying diversity controls: {e}")
            return tracks  # Return original tracks if filtering fails

    def _sanitize_playlist_name(self, name: str) -> str:
        """Sanitize playlist name to remove problematic characters"""
        if not name:
            return "Unknown Playlist"
        
        # Log original name for debugging
        self.logger.debug(f"Original playlist name: {repr(name)}")
        
        # Normalize Unicode punctuation and remove null bytes
        sanitized = normalize_name(name)
        sanitized = sanitized.replace('\x00', '').replace('\0', '')

        # Normalize different types of hyphens and dashes to a standard hyphen
        hyphen_variants = ['\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015']
        for variant in hyphen_variants:
            sanitized = sanitized.replace(variant, '-')
        
        # Ensure it's not empty after sanitization
        if not sanitized:
            sanitized = "Unknown Playlist"
        
        # Log sanitized name if it changed
        if sanitized != name:
            self.logger.info(f"🧹 Sanitized playlist name: '{name}' -> '{sanitized}'")
    
        return sanitized
    
    def _jellyfin_playlist_dir(self, playlist_name: str) -> str:
        r"""
        Replace invalid filename characters with spaces as Jellyfin does so that
        the playlist and corresponding image are saved in the same directory.
          - Characters: \ / : * ? " < > | and ASCII control chars (0–31)
        """
        invalid_chars = r'<>:"/\\|?*\x00-\x1F'
        # Replace invalid chars with a space
        sanitized = re.sub(f"[{invalid_chars}]", " ", playlist_name).strip()
        self.logger.info(f"🧹 Sanitized playlist sub-directory: '{sanitized}'")
        return sanitized.strip()
    
    def save_playlist(self, playlist_type: str, name: str, tracks: List[Dict], user_id: str = None):
        """Save playlist using Jellyfin's REST API with proper privacy controls and custom cover art"""
        self.logger.info(f"=== STARTING PLAYLIST CREATION ===")
        self.logger.info(f"Playlist Type: {playlist_type}")
        self.logger.info(f"Original Playlist Name: {repr(name)}")
        
        self.planned_names.add(normalize_name(name))
        
        # Sanitize the playlist name to prevent filesystem errors
        sanitized_name = self._sanitize_playlist_name(name)
        playlist_subdir = self._jellyfin_playlist_dir(sanitized_name)
        self.logger.info(f"Sanitized Playlist Name: {sanitized_name}")
        self.logger.info(f"Track Count: {len(tracks)}")
        self.logger.info(f"User ID: {user_id}")
        
        try:
            if not tracks:
                self.logger.warning(f"No tracks to save for playlist: {name}")
                return None
            
            # Extract track IDs from the track objects
            track_ids = []
            for track in tracks:
                track_id = track.get('Id')
                if track_id:
                    track_ids.append(track_id)
                else:
                    self.logger.warning(f"Track missing ID: {track.get('Name', 'Unknown')}")
            
            if not track_ids:
                self.logger.error(f"No valid track IDs found for playlist: {name}")
                return None
            
            self.logger.info(f"Extracted {len(track_ids)} valid track IDs")
            
            # Determine privacy settings based on playlist type
            # Personalized playlists are private, general playlists are public
            is_public = playlist_type.lower() != "personal"
            privacy_text = "public" if is_public else "private"
            
            self.logger.info(f"Creating {privacy_text} {playlist_type} playlist: {sanitized_name} with {len(track_ids)} tracks")
            
            if self.config.dry_run:
                api_name = normalize_name(name)
                if not is_generated_playlist_name(api_name):
                    action = 'skip'
                else:
                    ours, foreign = self._classify_existing(api_name, user_id)
                    action = 'skip' if foreign else ('replace' if ours else 'create')
                self.dry_run_plan.append({'name': api_name, 'type': playlist_type,
                                          'action': action, 'tracks': len(track_ids)})
                self.logger.info(f"🔎 DRY RUN: would {action} '{api_name}' ({len(track_ids)} tracks)")
                return None

            # Jellyfin names each playlist's folder after the playlist and appends a
            # number when that name is taken, so creating a second copy before removing
            # the first gives you "House Radio" and "House Radio1". The folder also
            # outlives the item, which is what turns an orphan into an empty duplicate
            # on the next scan. So: remove the old copies and their folders first, keep
            # their track list, and put it back if the create then fails.
            api_name = normalize_name(name)
            self.logger.info(f"Checking for existing playlist: {api_name}")
            rollback_tracks, skip = self._remove_existing_playlists(api_name, user_id)
            if skip:
                self.logger.warning(
                    f"Skipping '{api_name}': a playlist with that name exists and JellyJams "
                    f"did not create it"
                )
                return None
            if rollback_tracks:
                self.logger.info(
                    f"Replacing the existing playlist, {len(rollback_tracks)} of its "
                    f"tracks kept in case the new create fails"
                )
        
            # Create the playlist via API with proper privacy settings (use original name for API)
            self.logger.info(f"🔨 Creating new playlist via Jellyfin API...")
            self.logger.debug(f"API call parameters: name={repr(api_name)}, track_count={len(track_ids)}, user_id={user_id}, is_public={is_public}")
            result = self.jellyfin.create_playlist(api_name, track_ids, user_id, is_public)
            
            if result['success']:
                self.created_playlists += 1
                new_id = result.get('playlist_id')
                if new_id:
                    managed = load_managed_playlists()
                    managed[api_name] = new_id
                    save_managed_playlists(managed)
                self.logger.info(f"✅ Successfully created {privacy_text} playlist '{sanitized_name}' with {result['track_count']} tracks")
                
                # Create directory for cover art storage (use Jellyfin-style sanitized name for filesystem)
                self.logger.debug(f"Creating directory with sanitized name: {sanitized_name}")
                playlist_dir = Path(self.config.playlist_folder) / playlist_subdir
                self.logger.debug(f"Full directory path: {playlist_dir}")
                
                try:
                    playlist_dir.mkdir(parents=True, exist_ok=True)
                    self.logger.info(f"📁 Created playlist directory: {playlist_dir}")
                except Exception as dir_error:
                    self.logger.error(f"❌ Error creating directory {playlist_dir}: {dir_error}")
                    self.logger.error(f"Directory path repr: {repr(str(playlist_dir))}")
                    raise
                
                # Handle cover art based on playlist type
                cover_added = False
                
                # For personalized playlists, try custom cover art first
                if playlist_type.lower() == "personal":
                    self.logger.info(f"Attempting to apply custom cover art for personalized playlist...")
                    cover_added = self.copy_custom_cover_art(name, playlist_dir)
                    if cover_added:
                        self.logger.info(f"✅ Applied custom cover art for personalized playlist: {name}")
                    else:
                        self.logger.info(f"No custom cover art found for personalized playlist: {name}")
                
                # For decade playlists, try decade-specific cover art
                elif playlist_type.lower() == "decade" and "Back to the" in name:
                    self.logger.info(f"🗓️ Attempting to apply decade-specific cover art...")
                    cover_added = self._apply_decade_cover_art(name, playlist_dir)
                    if cover_added:
                        self.logger.info(f"✅ Applied decade-specific cover art for playlist: {name}")
                    else:
                        self.logger.info(f"❌ No decade-specific cover art found for playlist: {name}")
                
                # For genre playlists, try genre-specific cover art
                elif playlist_type.lower() == "genre" and " Radio" in name:
                    # Extract genre name from "[Genre] Radio" format
                    genre_name = name.replace(" Radio", "").strip()
                    self.logger.info(f"🎵 Attempting to apply genre-specific cover art for: {genre_name}")
                    cover_added = self._apply_genre_cover_art(name, genre_name, playlist_dir)
                    if cover_added:
                        self.logger.info(f"✅ Applied genre-specific cover art for playlist: {name}")
                    else:
                        self.logger.info(f"❌ No genre-specific cover art found for playlist: {name}")
            
                # For artist playlists: Deezer (no key or account), then Spotify, then custom art
                self.logger.debug(f"🔍 Cover art check - cover_added: {cover_added}, 'This is' in name: {'This is' in name}, deezer enabled: {self.deezer.is_enabled()}, spotify enabled: {self.spotify.is_enabled()}")
                
                if not cover_added and "This is" in name:
                    # Extract artist name from "This is [Artist]!" format
                    artist_name = name.replace("This is ", "").replace("!", "").strip()
                    self.logger.info(f"🎯 Extracted artist name: {artist_name}")
                    
                    # Deezer first: public API, no credentials needed
                    if self.deezer.is_enabled():
                        self.logger.info(f"🎨 Attempting to apply Deezer cover art for artist playlist...")
                        if self.deezer.get_artist_cover_art(artist_name, playlist_dir):
                            cover_added = True
                            self.logger.info(f"✅ Applied Deezer cover art for artist playlist: {name}")
                        else:
                            self.logger.info(f"❌ No Deezer cover art found for artist: {artist_name}")
                    
                    # Then Spotify, if it is configured
                    if not cover_added and self.spotify.is_enabled():
                        self.logger.info(f"🎨 Attempting to apply Spotify cover art for artist playlist...")
                        if self.spotify.get_artist_cover_art(artist_name, playlist_dir):
                            cover_added = True
                            self.logger.info(f"✅ Applied Spotify cover art for artist playlist: {name}")
                        else:
                            self.logger.info(f"❌ No Spotify cover art found for artist: {artist_name}")
                    
                    # If neither worked, fall back to custom cover art generation
                    if not cover_added:
                        self.logger.info(f"🎨 Attempting custom cover art generation for artist: {artist_name}")
                        try:
                            # Always try to generate custom cover art with "This is [Artist]" text overlay
                            self.logger.info(f"🎨 Generating custom cover art for artist: {artist_name}")
                            
                            # First, find the source image to use as base
                            artist_image = self._find_artist_cover_image(artist_name)
                            if not (artist_image is None):
                                # Generate custom cover art with text overlay
                                cover_dest = playlist_dir / 'cover.webp'
                                if self._generate_custom_cover_art(artist_image, artist_name, cover_dest):
                                    cover_added = True
                                    self.logger.info(f"✅ Generated custom cover art for artist: {artist_name}")
                                else:
                                    self.logger.info(f"❌ Failed to generate custom cover art for artist: {artist_name}")
                                    # Fallback: copy the original image directly
                                    self.logger.info(f"🖼️ Fallback: Using original artist cover image")
                                    fallback_destination = playlist_dir / "folder.webp"
                                    Image.open(BytesIO(artist_image)).save(fallback_destination)
                                    # Ensure cover art is world-readable on host mounts
                                    try:
                                        os.chmod(fallback_destination, 0o664)
                                    except Exception as chmod_err:
                                        self.logger.debug(f"chmod failed for {fallback_destination}: {chmod_err}")
                                    
                                    cover_added = True
                                    self.logger.info(f"✅ Applied existing artist cover art as fallback for: {artist_name}")
                            else:
                                self.logger.info(f"❌ No artist cover art found for: {artist_name}")
                        except Exception as cover_error:
                            self.logger.error(f"❌ Error generating custom cover art for {artist_name}: {cover_error}")
                
                if not cover_added:
                    self.logger.info(f"No cover art applied for playlist: {name}")
                
                self.logger.info(f"=== PLAYLIST CREATION COMPLETED SUCCESSFULLY ===")
                return playlist_dir
            else:
                self.logger.error(f"❌ Failed to create playlist '{name}': {result.get('error', 'Unknown error')}")
                if rollback_tracks:
                    self.logger.warning(
                        f"⚠️ Restoring the previous playlist '{name}' from its track list"
                    )
                    restored = self.jellyfin.create_playlist(api_name, rollback_tracks, user_id, is_public)
                    if restored.get('success'):
                        self.logger.info(f"✅ Previous playlist '{name}' restored")
                    else:
                        self.logger.error(f"❌ Could not restore '{name}': {restored.get('error')}")
                self.logger.info(f"=== PLAYLIST CREATION FAILED ===")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ Error saving playlist {name}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            self.logger.info(f"=== PLAYLIST CREATION FAILED WITH EXCEPTION ===")
            return None

    def generate_genre_playlists(self, audio_items: List[Dict]):
        """Generate the genre playlists: the biggest genres plus a few rotating spotlights"""
        self.logger.info("🎵 Generating genre-based playlists...")
        self.logger.info(
            f"🔧 Up to {self.config.max_genre_playlists} genres plus {self.config.genre_spotlight_slots} "
            f"spotlight(s), grouping {'on' if self.config.genre_grouping_enabled else 'off'}"
        )
        
        selected = select_genre_playlists(self.config, audio_items)
        room = max(0, self.config.max_total_playlists - self.shared_planned)
        if len(selected) > room:
            self.logger.info(f"Total playlist cap ({self.config.max_total_playlists}): keeping {room} of {len(selected)} genres")
            selected = selected[:room]
        
        for entry in selected:
            playlist_name = f"{entry['genre']} Radio"
            tracks = self._select_tracks(entry['tracks'])
            self.shared_planned += 1
            self.save_playlist("Genre", playlist_name, tracks)
            self.logger.info(
                f"Genre playlist '{playlist_name}' ({entry['kind']}): {len(tracks)} tracks "
                f"from a pool of {len(entry['tracks'])} by {entry['artists']} artists"
            )

    def generate_year_playlists(self, audio_items: List[Dict]):
        """Generate playlists by decade (1980s, 1990s, 2000s, etc.)"""
        self.logger.info("🗓️ Generating decade-based playlists...")
        self.logger.info(f"Minimum albums required per decade: {self.config.min_albums_per_decade}")
        
        # Group tracks by decade and collect album data
        decade_data = {}
        for item in audio_items:
            year = item.get('ProductionYear')
            if not year or year < 1950:  # Skip very old or invalid years
                continue
            
            # Check if any artist in this track is excluded
            if self.config.excluded_artists and item.get('Artists'):
                excluded_artist_found = False
                for artist in item['Artists']:
                    if artist in self.config.excluded_artists:
                        excluded_artist_found = True
                        break
                if excluded_artist_found:
                    continue
            
            # Calculate decade (e.g., 1987 -> 1980s, 2003 -> 2000s)
            decade_start = (year // 10) * 10
            decade_name = f"{decade_start}s"
            
            if decade_name not in decade_data:
                decade_data[decade_name] = {
                    'tracks': [],
                    'albums': set(),
                    'artists': set()
                }
            
            decade_data[decade_name]['tracks'].append(item)
            
            # Track unique albums
            if item.get('Album'):
                decade_data[decade_name]['albums'].add(item['Album'])
            
            # Track unique artists
            if item.get('Artists'):
                for artist in item['Artists']:
                    decade_data[decade_name]['artists'].add(artist)
        
        # Create playlists for each decade with album threshold checking
        created_playlists = 0
        skipped_decades = []
        
        for decade, data in decade_data.items():
            tracks = data['tracks']
            unique_albums = data['albums']
            unique_artists = data['artists']
            
            # Check minimum track count
            if len(tracks) < self.config.min_tracks_per_playlist:
                self.logger.info(f"⏭️  Skipping decade '{decade}' - only {len(tracks)} tracks (minimum: {self.config.min_tracks_per_playlist})")
                skipped_decades.append(f"{decade} ({len(tracks)} tracks)")
                continue
            
            # Check minimum album count (NEW REQUIREMENT)
            if len(unique_albums) < self.config.min_albums_per_decade:
                self.logger.info(f"⏭️  Skipping decade '{decade}' - only {len(unique_albums)} albums (minimum: {self.config.min_albums_per_decade})")
                skipped_decades.append(f"{decade} ({len(unique_albums)} albums)")
                continue
            
            # Check artist diversity
            if len(unique_artists) < self.config.min_artist_diversity:
                self.logger.info(f"⏭️  Skipping decade '{decade}' - only {len(unique_artists)} artists (minimum: {self.config.min_artist_diversity})")
                skipped_decades.append(f"{decade} ({len(unique_artists)} artists)")
                continue
                
            # Pick a random sample of the pool (see _select_tracks)
            limited_tracks = self._select_tracks(tracks)
            
            if self.shared_planned >= self.config.max_total_playlists:
                self.logger.info(f"⏭️  Skipping decade '{decade}': total playlist cap ({self.config.max_total_playlists}) reached")
                continue
            
            playlist_name = f"Back to the {decade}"
            self.shared_planned += 1
            self.save_playlist("Decade", playlist_name, limited_tracks)
            created_playlists += 1
            self.logger.info(f"✅ Created decade playlist '{playlist_name}' with {len(limited_tracks)} tracks from {len(unique_albums)} albums and {len(unique_artists)} artists")
        
        # Summary logging
        self.logger.info(f"🗓️ Decade playlist generation complete: {created_playlists} playlists created")
        if skipped_decades:
            self.logger.info(f"⏭️  Skipped {len(skipped_decades)} decades due to insufficient content: {', '.join(skipped_decades[:3])}{'...' if len(skipped_decades) > 3 else ''}")

    def generate_artist_playlists(self, audio_items: List[Dict]):
        """Generate playlists by artist with proper null-byte parsing"""
        self.logger.info("🎤 Generating artist-based playlists...")
        self.logger.info(f"Minimum albums required per artist: {self.config.min_albums_per_artist}")
        
        # Collect artist data with proper null-byte parsing
        self.logger.info("🎵 Collecting artist data...")
        artist_data = {}
        processed_items = 0
        
        for item in audio_items:
            processed_items += 1
            if processed_items % 100 == 0:
                self.logger.debug(f"📊 Processed {processed_items}/{len(audio_items)} audio items")
            
            # Parse null-byte-separated artist lists
            parsed_artists = []
            for artist in item['Artists']:
                if '\x00' in artist or '\0' in artist:
                    # Split on null bytes to get individual artists
                    individual_artists = [a.strip() for a in artist.replace('\0', '\x00').split('\x00') if a.strip()]
                    self.logger.debug(f"🎵 Parsed multi-artist field: {repr(artist)} -> {individual_artists}")
                    parsed_artists.extend(individual_artists)
                else:
                    parsed_artists.append(artist)
            
            # Update the item with parsed artists
            item['Artists'] = parsed_artists
            
            for artist in parsed_artists:
                if artist not in artist_data:
                    artist_data[artist] = {'tracks': [], 'albums': set()}
                artist_data[artist]['tracks'].append(item)
                
                album = item.get('Album', 'Unknown Album')
                if album:
                    # Check for null bytes in album names too
                    if '\x00' in str(album) or '\0' in str(album):
                        self.logger.warning(f"⚠️ Found null byte in album name: {repr(album)}")
                        album = str(album).replace('\x00', '').replace('\0', '').strip()
                    artist_data[artist]['albums'].add(album)
        
        self.logger.info(f"📊 Processed {processed_items} audio items, found {len(artist_data)} unique artists")
        
        # Create playlists for the biggest artists that meet the requirements
        created_playlists = 0
        skipped_artists = []
        
        room = max(0, self.config.max_total_playlists - self.shared_planned)
        limit = min(self.config.max_artist_playlists, room)
        chosen = select_artist_playlists(self.config, artist_data, limit)
        self.logger.info(f"🎤 {len(chosen)} artist playlist(s) selected (cap {self.config.max_artist_playlists}, room under the total cap {room})")
        
        for artist in chosen:
            data = artist_data[artist]
            tracks = data['tracks']
            self.logger.info(f"✅ Creating playlist for {artist}: {len(tracks)} tracks from {len(data['albums'])} albums")
            
            # Pick a random sample of the pool (see _select_tracks)
            limited_tracks = self._select_tracks(tracks)
            playlist_name = f"This is {artist}!"
            self.shared_planned += 1
            playlist_dir = self.save_playlist("Artist", playlist_name, limited_tracks)
            if playlist_dir:
                created_playlists += 1
        
        # Summary logging
        self.logger.info(f"🎤 Artist playlist generation complete: {created_playlists} playlists created")
        if skipped_artists:
            self.logger.info(f"⏭️  Skipped {len(skipped_artists)} artists due to insufficient albums: {', '.join(skipped_artists[:5])}{'...' if len(skipped_artists) > 5 else ''}")
        
        if skipped_artists and len(skipped_artists) <= 10:
            self.logger.info(f"   Skipped artists: {', '.join(skipped_artists)}")
        elif len(skipped_artists) > 10:
            self.logger.info(f"   Skipped artists: {', '.join(skipped_artists[:10])} and {len(skipped_artists) - 10} more...")

    def _filter_users_for_personalized_playlists(self, users: List[Dict]) -> List[Dict]:
        """Filter users based on configuration settings for personalized playlists"""
        if self.config.personal_playlist_users == 'all':
            return users
        
        # Parse comma-separated list of usernames
        selected_usernames = [name.strip() for name in self.config.personal_playlist_users.split(',')]
        
        # Filter users by username
        filtered_users = []
        for user in users:
            user_name = user.get('Name', '')
            if user_name in selected_usernames:
                filtered_users.append(user)
                self.logger.info(f"User '{user_name}' selected for personalized playlists")
            else:
                self.logger.debug(f"User '{user_name}' not in selected list: {selected_usernames}")
        
        return filtered_users

    def generate_personalized_playlists(self, audio_items: List[Dict]):
        """Generate personalized playlists for selected users"""
        self.logger.info("Generating personalized playlists...")
        
        # Get all users
        users = self.jellyfin.get_users()
        if not users:
            self.logger.warning("No users found for personalized playlists")
            return
        
        # Filter users based on configuration
        selected_users = self._filter_users_for_personalized_playlists(users)
        if not selected_users:
            self.logger.info("No users selected for personalized playlist generation")
            return
        
        for user in selected_users:
            user_id = user.get('Id')
            user_name = user.get('Name', 'Unknown')
            
            if not user_id:
                continue
                
            self.logger.info(f"Generating personalized playlists for user: {user_name}")
            
            try:
                # Generate different types of personalized playlists
                self.generate_user_top_tracks_playlist(user_id, user_name, audio_items)
                self.generate_user_discovery_playlist(user_id, user_name, audio_items)
                self.generate_user_recent_favorites_playlist(user_id, user_name, audio_items)
                self.generate_user_genre_mix_playlist(user_id, user_name, audio_items)
                
            except Exception as e:
                self.logger.error(f"Error generating personalized playlists for {user_name}: {e}")

    def generate_user_top_tracks_playlist(self, user_id: str, user_name: str, audio_items: List[Dict]):
        """Generate a playlist of user's most played tracks"""
        try:
            self.logger.info(f"Generating top tracks playlist for {user_name}...")
            top_tracks = []
            
            # Method 1: real play counts from the Playback Reporting plugin, if present.
            # The window is read at the configured size and widened once when it holds too
            # little, because nobody listens every week and a short window would leave this
            # playlist empty. With no plugin, or too thin a history, nothing is set here and
            # the favourites and recently-played fallbacks below behave exactly as before.
            windows = [w for w in dict.fromkeys([self.config.listening_window_days, 365]) if w and w > 0]
            for index, window in enumerate(windows):
                try:
                    counts = self.jellyfin.get_user_play_counts(user_id, days=window)
                except Exception as stats_e:
                    self.logger.warning(f"Could not read play counts for {user_name}: {stats_e}")
                    counts = {}

                if counts:
                    by_id = {track.get('Id'): track for track in audio_items if track.get('Id')}
                    played = []
                    for item_id, count in counts.items():
                        track = by_id.get(item_id)
                        if track:
                            track = dict(track)  # the library list is cached and shared
                            track['play_count'] = count
                            played.append(track)
                    played.sort(key=lambda t: t['play_count'], reverse=True)

                    if len(played) >= self.config.min_tracks_per_playlist:
                        top_tracks = played[:self.config.max_tracks_per_playlist]
                        self.logger.info(
                            f"Using play counts from the last {window} days - {len(played)} of the "
                            f"played tracks are still in the library"
                        )
                        break

                    self.logger.info(
                        f"Only {len(played)} played tracks within {window} days, "
                        f"minimum is {self.config.min_tracks_per_playlist}"
                    )

                if index < len(windows) - 1:
                    self.logger.info(f"Widening the play-count window past {window} days")
                else:
                    self.logger.info("Not enough play history for a Top Tracks playlist, using fallbacks")
            
            # Method 2: Fallback to favorite tracks if no listening stats available
            if not top_tracks:
                try:
                    self.logger.info(f"Falling back to favorite tracks for {user_name}")
                    favorite_tracks = self.jellyfin.get_user_favorite_items(user_id)
                    if favorite_tracks:
                        top_tracks = favorite_tracks
                        self.logger.info(f"Using favorites - found {len(top_tracks)} favorite tracks")
                    else:
                        self.logger.info(f"No favorite tracks found for {user_name}")
                except Exception as fav_e:
                    self.logger.warning(f"Could not get favorite tracks for {user_name}: {fav_e}")
            
            # Method 3: Final fallback to recently played tracks
            if not top_tracks:
                try:
                    self.logger.info(f"Final fallback to recently played tracks for {user_name}")
                    recent_tracks = self.jellyfin.get_recently_played(user_id, limit=30)
                    if recent_tracks:
                        top_tracks = recent_tracks
                        self.logger.info(f"Using recent tracks - found {len(top_tracks)} recently played tracks")
                    else:
                        self.logger.info(f"No recently played tracks found for {user_name}")
                except Exception as recent_e:
                    self.logger.warning(f"Could not get recently played tracks for {user_name}: {recent_e}")
            
            # Create playlist if we have tracks
            if top_tracks:
                # Ensure we have enough tracks for a meaningful playlist
                if len(top_tracks) < self.config.min_tracks_per_playlist:
                    self.logger.warning(f"Only {len(top_tracks)} tracks found for {user_name}, minimum is {self.config.min_tracks_per_playlist}")
                    return
                
                limited_tracks = self._select_tracks(top_tracks)
                
                playlist_name = f"Top Tracks - {user_name}"
                self.save_playlist("Personal", playlist_name, limited_tracks, user_id)
                self.logger.info(f"✅ Created top tracks playlist for {user_name} with {len(limited_tracks)} tracks")
            else:
                self.logger.warning(f"❌ No tracks found for {user_name} - cannot create top tracks playlist")
                
        except Exception as e:
            self.logger.error(f"❌ Error generating top tracks playlist for {user_name}: {e}")
            import traceback
            self.logger.debug(f"Full traceback: {traceback.format_exc()}")

    def generate_user_discovery_playlist(self, user_id: str, user_name: str, audio_items: List[Dict]):
        """Generate a discovery playlist with similar songs based on user's listening habits"""
        try:
            # Get user's recently played and favorite tracks
            recent_tracks = self.jellyfin.get_recently_played(user_id, limit=20)
            favorite_tracks = self.jellyfin.get_user_favorite_items(user_id)
            
            # Combine and deduplicate reference tracks
            reference_tracks = []
            seen_ids = set()
            
            for track_list in [recent_tracks, favorite_tracks]:
                for track in track_list:
                    track_id = track.get('Id')
                    if track_id and track_id not in seen_ids:
                        reference_tracks.append(track)
                        seen_ids.add(track_id)
            
            if reference_tracks:
                # Find similar tracks based on genres
                similar_tracks = self.jellyfin.get_similar_tracks_by_genre(
                    reference_tracks, audio_items, limit=self.config.max_tracks_per_playlist * 3
                )
                
                if similar_tracks:
                    # Apply diversity controls: max songs per album and per artist
                    diverse_tracks = self._apply_discovery_diversity_controls(similar_tracks)
                    
                    final_tracks = self._select_tracks(diverse_tracks)
                    
                    playlist_name = f"Discovery Mix - {user_name}"
                    self.save_playlist("Personal", playlist_name, final_tracks, user_id)
                    self.logger.info(f"Created discovery playlist for {user_name} with {len(final_tracks)} tracks (applied diversity: max {self.config.discovery_max_songs_per_album} per album, {self.config.discovery_max_songs_per_artist} per artist)")
                else:
                    self.logger.info(f"No similar tracks found for {user_name}")
            else:
                self.logger.info(f"No reference tracks found for {user_name} discovery playlist")
                
        except Exception as e:
            self.logger.error(f"Error generating discovery playlist for {user_name}: {e}")

    def generate_user_recent_favorites_playlist(self, user_id: str, user_name: str, audio_items: List[Dict]):
        """Generate a playlist based on recently played tracks"""
        try:
            recent_tracks = self.jellyfin.get_recently_played(user_id, limit=30)
            
            if recent_tracks:
                limited_tracks = self._select_tracks(recent_tracks)
                
                playlist_name = f"Recent Favorites - {user_name}"
                self.save_playlist("Personal", playlist_name, limited_tracks, user_id)
                self.logger.info(f"Created recent favorites playlist for {user_name} with {len(limited_tracks)} tracks")
            else:
                self.logger.info(f"No recent tracks found for {user_name}")
                
        except Exception as e:
            self.logger.error(f"Error generating recent favorites playlist for {user_name}: {e}")

    def generate_user_genre_mix_playlist(self, user_id: str, user_name: str, audio_items: List[Dict]):
        """Generate a mixed playlist from user's favorite genres"""
        try:
            # Get user's favorite and recent tracks to determine preferred genres
            favorite_tracks = self.jellyfin.get_user_favorite_items(user_id)
            recent_tracks = self.jellyfin.get_recently_played(user_id, limit=20)
            
            # Combine reference tracks
            reference_tracks = favorite_tracks + recent_tracks
            
            if not reference_tracks:
                self.logger.info(f"No reference tracks found for {user_name} genre mix")
                return
            
            # Extract and count genres from user's listening history
            genre_counts = {}
            for track in reference_tracks:
                if track.get('Genres'):
                    track_genres = track['Genres'] if isinstance(track['Genres'], list) else [track['Genres']]
                    for genre in track_genres:
                        if isinstance(genre, str):
                            genre_counts[genre] = genre_counts.get(genre, 0) + 1
            
            # Get top 3 user genres
            top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            
            if not top_genres:
                self.logger.info(f"No genres found for {user_name}")
                return
            
            # Find tracks from user's top genres
            genre_mix_tracks = []
            tracks_per_genre = self.config.max_tracks_per_playlist // len(top_genres)
            
            for genre, count in top_genres:
                genre_tracks = []
                for track in audio_items:
                    if track.get('Genres'):
                        track_genres = track['Genres'] if isinstance(track['Genres'], list) else [track['Genres']]
                        if genre in track_genres:
                            # Skip if already in user's collection
                            if track.get('Id') not in {t.get('Id') for t in reference_tracks}:
                                genre_tracks.append(track)
                
                # Add random selection from this genre
                if genre_tracks:
                    import random
                    selected = random.sample(genre_tracks, min(tracks_per_genre, len(genre_tracks)))
                    genre_mix_tracks.extend(selected)
            
            if genre_mix_tracks:
                # Limit to max tracks
                limited_tracks = self._select_tracks(genre_mix_tracks)
                
                playlist_name = f"Genre Mix - {user_name}"
                self.save_playlist("Personal", playlist_name, limited_tracks, user_id)
                self.logger.info(f"Created genre mix playlist for {user_name} with {len(limited_tracks)} tracks")
            else:
                self.logger.info(f"No genre mix tracks found for {user_name}")
                
        except Exception as e:
            self.logger.error(f"Error generating genre mix playlist for {user_name}: {e}")

    def generate_playlists(self):
        """Run one generation pass and record its outcome for monitoring."""
        try:
            counts = self._generate_playlists()
        except Exception as e:
            self.logger.exception("Playlist generation raised an exception")
            if not self.config.dry_run:
                write_run_status(False, summary=f"{type(e).__name__}: {e}")
            raise

        # A dry run is not a run: it must not move the health status either way.
        if not self.config.dry_run:
            if counts is None:
                write_run_status(False, summary="aborted before any playlist was written")
            else:
                write_run_status(True, summary="completed", counts=counts)
        return counts

    def _generate_playlists(self):
        """Main playlist generation function. Returns counts, or None if it aborted."""
        self.created_playlists = 0
        self.dry_run_plan = []
        self.shared_planned = 0
        self.planned_names = set()
        if self.config.dry_run:
            self.logger.info("🔎 DRY RUN: reading the library, writing nothing to Jellyfin or /data")
        self.adopt_existing_playlists()
        self.logger.info("🎵 ========== STARTING JELLYJAMS PLAYLIST GENERATION ==========")
        self.logger.info(f"🔧 Configuration: Max tracks: {self.config.max_tracks_per_playlist}, Min tracks: {self.config.min_tracks_per_playlist}")
        self.logger.info(f"🔧 Playlist types: {', '.join(self.config.playlist_types)}")
        self.logger.info(f"🔧 Min albums per artist: {self.config.min_albums_per_artist}")
        self.logger.info(f"🔧 Excluded genres: {', '.join(self.config.excluded_genres) if self.config.excluded_genres else 'None'}")
        self.logger.info(f"🔧 Excluded artists: {', '.join(self.config.excluded_artists) if self.config.excluded_artists else 'None'}")
        
        # Test Jellyfin connection
        self.logger.info("🌐 Testing Jellyfin connection...")
        if not self.jellyfin.test_connection():
            self.logger.error("❌ Cannot connect to Jellyfin. Aborting playlist generation.")
            return None
        self.logger.info("✅ Jellyfin connection successful")
        
        # Get audio items
        self.logger.info("🎶 Fetching audio items from Jellyfin...")
        audio_items = self.jellyfin.get_audio_items()
        if not audio_items:
            self.logger.warning("⚠️ No audio items found. Aborting playlist generation.")
            return None
        
        self.logger.info(f"📊 Found {len(audio_items)} audio items in library")
        
        # Generate playlists based on configuration
        if 'Genre' in self.config.playlist_types:
            self.generate_genre_playlists(audio_items)
        
        if 'Year' in self.config.playlist_types:
            self.generate_year_playlists(audio_items)
        
        if 'Artist' in self.config.playlist_types:
            self.generate_artist_playlists(audio_items)
        
        if 'Personal' in self.config.playlist_types:
            self.generate_personalized_playlists(audio_items)
        
        # Trigger Jellyfin library scan to refresh playlists if enabled
        if self.config.dry_run:
            self._log_dry_run_report()
            return {'dry_run': True, 'planned': len(self.dry_run_plan)}
        
        retired = 0
        if self.config.retire_stale_playlists:
            retired = self.retire_stale_playlists()
            self.logger.info(f"🧹 Retired {retired} playlist(s) that are no longer generated")

        if self.config.trigger_library_scan:
            self.logger.info("Triggering Jellyfin media library scan to refresh playlists...")
            if self.jellyfin.trigger_library_scan():
                self.logger.info("✅ Media library scan triggered successfully")
            else:
                self.logger.warning("⚠️ Failed to trigger media library scan")
        
        self.logger.info("JellyJams playlist generation completed successfully!")
        return {'playlists_created': self.created_playlists, 'playlists_retired': retired}

def parse_args(argv):
    """Command line of vibecodeplugin.py. An unknown flag is an error, not a silent start:
    the plain command starts the scheduler and never returns, which is not what a
    mistyped --dry-run should do."""
    import argparse
    parser = argparse.ArgumentParser(
        prog='vibecodeplugin.py',
        description='JellyJams playlist generator. With no flags it runs the scheduler.')
    parser.add_argument('--dry-run', action='store_true',
                        help='read everything, write nothing, and print what a run would do')
    parser.add_argument('--retire-stale', action='store_true',
                        help='run once and remove generated playlists that are no longer generated')
    return parser.parse_args(argv)


def main():
    """Main application entry point"""
    args = parse_args(sys.argv[1:])
    config = Config()
    dry_run = args.dry_run
    config.dry_run = dry_run
    if args.retire_stale:
        config.retire_stale_playlists = True
    logger = setup_logging(config)
    
    if dry_run or args.retire_stale:
        # One pass, no scheduling. With --dry-run nothing is written. Run it inside the
        # container:
        #   docker exec <container> python /app/vibecodeplugin.py --dry-run
        #   docker exec <container> python /app/vibecodeplugin.py --retire-stale
        if not config.api_key:
            logger.error("JELLYFIN_API_KEY environment variable is required!")
            sys.exit(1)
        PlaylistGenerator(config, logger).generate_playlists()
        return

    logger.info("🎵 JellyJams Generator Starting...")
    logger.info(f"Jellyfin URL: {config.jellyfin_url}")
    logger.info(f"Playlist Folder: {config.playlist_folder}")
    logger.info(f"Schedule Mode: {config.schedule_mode}")
    logger.info(f"Auto Generate on Startup: {config.auto_generate_on_startup}")
    logger.info(f"Playlist Types: {', '.join(config.playlist_types)}")
    
    # Validate configuration
    if not config.api_key:
        logger.error("JELLYFIN_API_KEY environment variable is required!")
        sys.exit(1)
    
    # Create playlist generator
    generator = PlaylistGenerator(config, logger)
    
    # Run initial generation only if enabled
    if config.auto_generate_on_startup:
        logger.info("🚀 Running initial playlist generation (startup generation enabled)")
        generator.generate_playlists()
    else:
        logger.info("⏸️ Skipping initial playlist generation (startup generation disabled)")
    
    # Setup scheduling based on configuration
    WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

    def parse_time(value, fallback="04:30"):
        """Return a validated HH:MM string, falling back on a bad value."""
        try:
            hour, minute = map(int, str(value).split(':'))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        except (ValueError, AttributeError):
            pass
        logger.error(f"Invalid schedule time {value!r}, using {fallback}")
        return fallback

    if config.schedule_mode == 'manual':
        logger.info("📋 Manual mode: Playlists will only be generated via web UI or API calls")

    elif config.schedule_mode == 'daily':
        at = parse_time(config.schedule_time)
        schedule.every().day.at(at).do(generator.generate_playlists)
        logger.info(f"⏰ Daily generation scheduled at {at}")

    elif config.schedule_mode == 'weekly':
        at = parse_time(config.schedule_time)
        weekday = str(getattr(config, 'schedule_weekday', 'sunday')).lower()
        if weekday not in WEEKDAYS:
            logger.error(f"Invalid weekday {config.schedule_weekday!r}, using sunday")
            weekday = 'sunday'
        getattr(schedule.every(), weekday).at(at).do(generator.generate_playlists)
        logger.info(f"⏰ Weekly generation scheduled for {weekday.capitalize()} at {at}")

    elif config.schedule_mode == 'interval':
        schedule.every(config.generation_interval).hours.do(generator.generate_playlists)
        logger.info(f"⏰ Interval generation scheduled every {config.generation_interval} hours")

    else:
        logger.warning(f"Unknown schedule mode: {config.schedule_mode}. Defaulting to manual mode.")
    
    # Keep running
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

if __name__ == "__main__":
    main()
