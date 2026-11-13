"""Hand-made playlists are never written to, and URL names cannot leave the playlist folder."""
import json

import pytest


@pytest.fixture
def folder(webapp_module, plugin, tmp_path, monkeypatch):
    """A playlist folder with two JellyJams playlists and three hand-made ones."""
    root = tmp_path / "playlists"
    root.mkdir()
    for name in ("Techno Radio", "This is Usher!", "Back to the 1980s",      # made by JellyJams
                 "Rock Radio", "This is my own mix", "HOUSEMAFIA"):           # made by hand
        (root / name).mkdir()
        (root / name / "playlist.xml").write_text("<Item><PlaylistItems></PlaylistItems></Item>")
    (tmp_path / "managed.json").write_text(json.dumps({"playlists": {
        "Techno Radio": "id1", "This is Usher!": "id2", "Back to the 1980s": "id3"}}))
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "managed.json"))
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(root))
    return root


def test_only_folders_in_the_record_count_as_made_by_jellyjams(webapp_module, folder):
    names = [d.name for d in webapp_module.folders_made_by_jellyjams(folder)]

    assert names == ["Back to the 1980s", "Techno Radio", "This is Usher!"]


def test_a_folder_matches_its_recorded_name_through_the_sanitiser(webapp_module, plugin, folder, tmp_path):
    """Jellyfin may spell a folder differently from the recorded name."""
    (folder / "Guns N Roses").mkdir()
    (tmp_path / "managed.json").write_text(json.dumps({"playlists": {"Guns N' Roses": "id9"}}))

    names = [d.name for d in webapp_module.folders_made_by_jellyjams(
        folder, lambda n: n.replace("'", ""))]

    assert names == ["Guns N Roses"]


class _Fake:
    """Stands in for the generator, the cover clients and the Discord notifier."""
    touched = []

    def __init__(self, *a, **k):
        pass

    def is_enabled(self):
        return False

    spotify = None

    def _get_cached_audio_items(self):
        return []

    def _sanitize_playlist_name(self, name):
        return name

    def _apply_genre_cover_art(self, name, genre, folder):
        _Fake.touched.append(name)
        return True

    def _apply_decade_cover_art(self, name, folder):
        _Fake.touched.append(name)
        return True

    def copy_custom_cover_art(self, name, folder):
        _Fake.touched.append(name)
        return True

    def _find_artist_cover_image(self, artist):
        return None

    def send_cover_art_summary(self, *a, **k):
        pass


def test_update_covers_leaves_hand_made_playlists_alone(webapp_module, folder, monkeypatch):
    _Fake.touched = []
    for attr in ("PlaylistGenerator", "SpotifyClient", "DeezerClient"):
        monkeypatch.setattr(webapp_module, attr, _Fake)
    monkeypatch.setattr(webapp_module, "Config", lambda: webapp_module.config)
    monkeypatch.setattr(webapp_module, "setup_logging", lambda c: webapp_module.logger)
    monkeypatch.setattr(webapp_module, "discord_notifier", _Fake())

    response = webapp_module.app.test_client().post("/api/update-covers")

    assert response.get_json()["success"] is True
    # The two that the fake touches are the generated genre and decade ones. Never the
    # hand-made "Rock Radio", "This is my own mix" or "HOUSEMAFIA".
    assert sorted(_Fake.touched) == ["Back to the 1980s", "Techno Radio"]


# ---- names from the URL --------------------------------------------------------------

@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "a\\b", "x\x00y", "../../etc"])
def test_unsafe_playlist_names_are_refused(webapp_module, folder, name):
    assert webapp_module.playlist_dir_for(name) is None


def test_a_normal_name_gives_its_folder(webapp_module, folder):
    assert webapp_module.playlist_dir_for("Techno Radio") == (folder / "Techno Radio").resolve()
    assert webapp_module.playlist_dir_for("This is Usher!") == (folder / "This is Usher!").resolve()


def test_cover_and_contents_endpoints_do_not_leave_the_playlist_folder(webapp_module, folder, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "folder.jpg").write_bytes(b"not for you")
    (outside / "playlist.xml").write_text("<Item><PlaylistItems><PlaylistItem><Path>/a/b/c/d.mp3</Path>"
                                          "</PlaylistItem></PlaylistItems></Item>")
    client = webapp_module.app.test_client()

    for bad in ("..%2fsecret", "%2e%2e%2fsecret"):
        assert client.get(f"/api/cover/{bad}").status_code == 404
        body = client.get(f"/api/playlist_contents/{bad}").get_json()
        assert body["success"] is False


def test_the_users_list_gives_only_ids_and_names(webapp_module, monkeypatch):
    class J:
        def __init__(self, *a, **k):
            pass

        def get_users(self):
            return [{"Id": "u1", "Name": "didier", "Policy": {"IsAdministrator": True},
                     "LastLoginDate": "2026-11-13", "Configuration": {"x": 1}}]

    monkeypatch.setattr(webapp_module, "JellyfinAPI", J)

    body = webapp_module.app.test_client().get("/api/users").get_json()

    assert body == {"success": True, "users": [{"Id": "u1", "Name": "didier"}]}


# ---- secrets never reach the browser -----------------------------------------------------

def _settings_with_secrets(webapp_module, monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"spotify_client_secret": "s3cret-value", "discord_webhook_url": "https://hook/x",
                                "spotify_client_id": "public-id"}))
    monkeypatch.setattr(webapp_module.config_manager, "config_file", str(path))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_ENABLED", raising=False)
    return path


def test_the_settings_api_masks_secrets(webapp_module, monkeypatch, tmp_path):
    _settings_with_secrets(webapp_module, monkeypatch, tmp_path)

    body = webapp_module.app.test_client().get("/api/settings").get_data(as_text=True)

    assert "s3cret-value" not in body and "https://hook/x" not in body
    assert webapp_module.SECRET_MASK in body
    assert "public-id" in body              # the client id is not a secret


def test_the_settings_page_does_not_contain_the_secret(webapp_module, monkeypatch, tmp_path):
    _settings_with_secrets(webapp_module, monkeypatch, tmp_path)

    page = webapp_module.app.test_client().get("/settings").get_data(as_text=True)

    assert "s3cret-value" not in page


def test_saving_with_the_mask_keeps_the_stored_secret(webapp_module, monkeypatch, tmp_path):
    path = _settings_with_secrets(webapp_module, monkeypatch, tmp_path)
    client = webapp_module.app.test_client()

    client.post("/api/settings", json={"spotify_client_secret": webapp_module.SECRET_MASK, "max_tracks_per_playlist": 80})

    stored = json.loads(path.read_text())
    assert stored["spotify_client_secret"] == "s3cret-value"
    assert stored["max_tracks_per_playlist"] == 80


def test_saving_a_new_or_empty_secret_replaces_it(webapp_module, monkeypatch, tmp_path):
    path = _settings_with_secrets(webapp_module, monkeypatch, tmp_path)
    client = webapp_module.app.test_client()

    client.post("/api/settings", json={"spotify_client_secret": "new-one"})
    assert json.loads(path.read_text())["spotify_client_secret"] == "new-one"

    client.post("/api/settings", json={"spotify_client_secret": ""})
    assert json.loads(path.read_text())["spotify_client_secret"] == ""


# ---- settings are checked before they are saved ----------------------------------------------

@pytest.fixture
def settings_file(webapp_module, monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"max_tracks_per_playlist": 100, "min_tracks_per_playlist": 5,
                                "schedule_mode": "weekly"}))
    monkeypatch.setattr(webapp_module.config_manager, "config_file", str(path))
    return path


@pytest.mark.parametrize("payload", [
    {"max_tracks_per_playlist": "abc"},
    {"max_tracks_per_playlist": -5},
    {"max_tracks_per_playlist": 5000},
    {"max_tracks_per_playlist": 12.5},
    {"max_tracks_per_playlist": True},
    {"generation_interval": 0},
    {"schedule_time": "25:99"},
    {"schedule_time": "soon"},
    {"schedule_mode": "hourly"},
    {"schedule_weekday": "someday"},
    {"log_level": "LOUD"},
    {"playlist_types": "Genre"},
    {"playlist_types": ["Genre", "Podcast"]},
    {"jellyfin_url": "not a url"},
    {"min_tracks_per_playlist": 50, "max_tracks_per_playlist": 10},
    {"genre_split_share": 2},
    {"max_genre_playlists": 101},
])
def test_a_bad_setting_is_refused_and_nothing_is_saved(webapp_module, settings_file, payload):
    before = settings_file.read_text()

    response = webapp_module.app.test_client().post("/api/settings", json=payload)

    assert response.status_code == 400, payload
    body = response.get_json()
    assert body["success"] is False and body["errors"] and body["message"].startswith("Not saved")
    assert settings_file.read_text() == before


def test_good_settings_are_still_saved(webapp_module, settings_file):
    payload = {"max_tracks_per_playlist": "80", "min_tracks_per_playlist": 10, "schedule_mode": "daily",
               "schedule_time": "04:30", "schedule_weekday": "friday", "log_level": "DEBUG",
               "playlist_types": ["Genre", "Artist"], "jellyfin_url": "http://jellyfin:8096",
               "generation_interval": 24, "max_genre_playlists": 0, "genre_split_share": 0.25}

    response = webapp_module.app.test_client().post("/api/settings", json=payload)

    assert response.status_code == 200 and response.get_json()["success"] is True
    assert json.loads(settings_file.read_text())["schedule_mode"] == "daily"


def test_a_payload_that_is_not_json_is_refused(webapp_module, settings_file):
    before = settings_file.read_text()

    response = webapp_module.app.test_client().post("/api/settings", data="nonsense", content_type="text/plain")

    assert response.status_code == 400
    assert settings_file.read_text() == before


def test_the_settings_page_shows_the_reason_a_save_was_refused(webapp_module):
    page = webapp_module.app.test_client().get("/settings").get_data(as_text=True)

    assert "xhr.responseJSON" in page


def test_the_settings_the_live_app_returns_pass_validation(webapp_module):
    """What the page loaded is what it posts back; that must never be refused."""
    live = {"artist_cover_name_overlay": True, "auto_generate_on_startup": False, "deezer_cover_art_enabled": True,
            "discovery_max_songs_per_album": 1, "discovery_max_songs_per_artist": 2, "enabled_artists": [],
            "excluded_genres": [], "generation_interval": 24, "genre_grouping_enabled": False,
            "genre_spotlight_slots": 2, "jellyfin_url": "http://10.64.1.2:8096", "listening_window_days": 90,
            "log_level": "INFO", "max_artist_playlists": 20, "max_genre_playlists": 14, "max_total_playlists": 45,
            "max_tracks_per_playlist": 100, "min_albums_per_artist": 3, "min_albums_per_decade": 3,
            "min_artist_diversity": 5, "min_tracks_per_playlist": 5, "personal_playlist_min_user_tracks": 25,
            "personal_playlist_users": "all", "playlist_types": ["Genre", "Year", "Artist", "Personal"],
            "schedule_mode": "weekly", "schedule_time": "04:30", "schedule_weekday": "sunday"}
    # the same values as the form sends them: everything a text field gives is a string
    as_form = {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in live.items()}

    assert webapp_module.validate_settings(live, live) == []
    assert webapp_module.validate_settings(as_form, live) == []
