#!/usr/bin/env python3
"""Draw the genre covers that ship in app/cover/. Run by hand, never by the container.

    python tools/make_genre_covers.py            # draw the covers that are missing
    python tools/make_genre_covers.py --force "Trance" "House"   # redraw these

Each cover is app/cover/<Genre> Radio.jpg: the Fallback Radio photo in the genre's own
colour with the name on top (see app/genre_cover.py). Covers that already exist, drawn
by hand or by this script, are never touched unless named with --force. The files are
committed, so the result never changes unless somebody decides it should.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))

from genre_cover import find_cover, render_genre_cover  # noqa: E402

COVER_DIR = ROOT / 'app' / 'cover'
BACKGROUND = COVER_DIR / 'Fallback Radio.jpg'

# The 25 groups the generator folds genres into, plus the subgenres that stand on their own
# in a large library, plus the genres people most often tag.
GENRES = [
    # groups
    'Alternative', 'Ambient', 'Blues', 'Classical', 'Country', 'Disco', 'Electronic', 'Folk',
    'Funk', 'Gospel', 'Hip Hop', 'Industrial', 'Jazz', 'Lounge', 'Metal', 'New Wave', 'Pop',
    'Punk', 'R&B', 'Reggae', 'Rock', 'Rockabilly', 'Shoegaze', 'Singer-Songwriter', 'World',
    # electronic and dance
    'House', 'Progressive House', 'Deep House', 'Electro House', 'Tech House', 'Future House',
    'Funky House', 'Acid House', 'Disco House', 'Vocal House', 'Big Room', 'Trance',
    'Hard Trance', 'Psytrance', 'Techno', 'Minimal', 'Dubstep', 'Drum & Bass', 'Breakbeat',
    'Garage', 'UK Garage', 'Electro', 'Electropop', 'Synthpop', 'Synthwave', 'Vaporwave',
    'Downtempo', 'Chillout', 'Trip Hop', 'Eurodance', 'Dance', 'Hardstyle', 'Hard Dance',
    'Hardcore', 'Happy Hardcore', 'Jumpstyle', 'Moombahton', 'Brazilian Funk',
    # hip hop and urban
    'Trap', 'Rap', 'Grime', 'Soul', 'Dancehall', 'Dub', 'Ska', 'Reggaeton',
    # everything else people tag
    'Latin', 'Indie', 'Indie Pop', 'Indie Rock', 'Soundtrack', 'Ballad', 'Schlager', 'K-Pop',
    'Afrobeat', 'Celtic', 'Comedy', 'Easy Listening', 'Grunge', 'Britpop',
]


def main(argv):
    force = {name.lower() for name in argv if not name.startswith('-')} if '--force' in argv else set()
    if not BACKGROUND.exists():
        sys.exit(f"Missing background photo: {BACKGROUND}")
    made, kept = [], []
    for genre in GENRES:
        existing = find_cover(COVER_DIR, [f"{genre} Radio", genre])
        if existing and genre.lower() not in force:
            kept.append(genre)
            continue
        render_genre_cover(BACKGROUND, genre, COVER_DIR / f"{genre} Radio.jpg")
        made.append(genre)
    print(f"drew {len(made)} cover(s), kept {len(kept)} that already exist")
    for genre in made:
        print(f"  + {genre} Radio.jpg")


if __name__ == '__main__':
    main(sys.argv[1:])
