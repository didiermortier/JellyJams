"""Which genre and artist playlists get made: fewer, bigger, and still fresh."""
import itertools
import logging

import pytest

from fakes import FakeJellyfin

_ids = itertools.count(1)


def make_tracks(genre, n, artists=None):
    """n tracks tagged `genre`, each by a different artist unless `artists` says otherwise."""
    out = []
    for i in range(n):
        artist = artists[i % len(artists)] if artists else f"{genre} artist {i}"
        out.append({"Id": f"t{next(_ids)}", "Name": f"{genre} {i}", "Genres": [genre],
                    "Artists": [artist], "Album": f"{genre} album {i // 3}", "ProductionYear": 2015})
    return out


@pytest.fixture
def config(plugin):
    c = plugin.Config()
    c.max_genre_playlists = 14
    c.genre_spotlight_slots = 2
    return c


def names(selection):
    return [e["genre"] for e in selection]


# ---- tags ---------------------------------------------------------------------

def test_tags_differing_only_in_case_are_one_genre(plugin, config):
    items = make_tracks("Hip Hop", 6) + make_tracks("hip hop", 6) + make_tracks("HIP HOP", 6)

    selection = plugin.select_genre_playlists(config, items)

    assert names(selection) == ["Hip Hop"]
    assert len(selection[0]["tracks"]) == 18


def test_aliases_merge(plugin, config):
    items = make_tracks("Drum And Bass", 6) + make_tracks("drum & bass", 6) + make_tracks("DnB", 6)

    assert names(plugin.select_genre_playlists(config, items)) == ["Drum & Bass"]


def test_a_tag_holding_several_genres_counts_for_each(plugin, config):
    items = []
    for i in range(6):
        items.append({"Id": f"m{i}", "Genres": ["Jazz;Blues"], "Artists": [f"a{i}"]})

    assert sorted(names(plugin.select_genre_playlists(config, items))) == ["Blues", "Jazz"]


def test_a_track_is_counted_once_per_genre(plugin, config):
    items = [{"Id": f"d{i}", "Genres": ["Jazz", "jazz", "JAZZ"], "Artists": [f"a{i}"]} for i in range(6)]

    selection = plugin.select_genre_playlists(config, items)

    assert len(selection[0]["tracks"]) == 6


@pytest.mark.parametrize("tag,shown", [
    ("hip hop", "Hip Hop"), ("r&b", "R&B"), ("dance-pop", "Dance-Pop"),
    ("progressive house", "Progressive House"), ("edm", "EDM"), ("uk garage", "UK Garage"),
])
def test_display_names(plugin, tag, shown):
    assert plugin.display_genre(tag) == shown


# ---- grouping and splitting ---------------------------------------------------

def test_subgenres_fold_into_a_normal_sized_group(plugin, config):
    rock = ["Classic Rock", "Hard Rock", "Indie Rock", "Grunge"]
    items = []
    for genre in rock:
        items += make_tracks(genre, 8)
    items += make_tracks("Jazz", 100)    # keep Rock well under the split share

    selection = plugin.select_genre_playlists(config, items)

    assert "Rock" in names(selection)
    assert not set(rock) & set(names(selection))


def test_a_group_that_dominates_the_library_is_split(plugin, config):
    """80 percent electronic: House and Techno must stand on their own."""
    items = (make_tracks("House", 400) + make_tracks("Techno", 150) + make_tracks("Trance", 100)
             + make_tracks("Jazz", 100))

    got = names(plugin.select_genre_playlists(config, items))

    assert {"House", "Techno", "Trance", "Jazz"} <= set(got)
    assert "Electronic" not in got


def test_grouping_off_keeps_every_tag_apart(plugin, config):
    config.genre_grouping_enabled = False
    items = make_tracks("Classic Rock", 8) + make_tracks("Hard Rock", 8)

    assert sorted(names(plugin.select_genre_playlists(config, items))) == ["Classic Rock", "Hard Rock"]


def test_map_genre_to_group_ignores_case(plugin):
    config = plugin.Config()
    assert config.map_genre_to_group("hip hop") == config.map_genre_to_group("Hip Hop")
    assert config.map_genre_to_group("classic ROCK") == "Rock"
    assert config.map_genre_to_group("Something Unmapped") == "Something Unmapped"


# ---- how many ---------------------------------------------------------------

def many_genres(count, size=30):
    items = []
    for i in range(count):
        items += make_tracks(f"Genre{i:02d}", size + (count - i))   # distinct sizes, G00 biggest
    return items


def test_only_the_biggest_genres_are_always_made(plugin, config):
    selection = plugin.select_genre_playlists(config, many_genres(40), week=1)

    top = [e for e in selection if e["kind"] == "top"]
    assert len(top) == 14
    assert names(top) == [f"Genre{i:02d}" for i in range(14)]
    assert len(selection) == 16


def test_spotlights_rotate_with_the_week_and_never_repeat_the_top(plugin, config):
    week1 = plugin.select_genre_playlists(config, many_genres(40), week=1)
    week2 = plugin.select_genre_playlists(config, many_genres(40), week=2)
    again = plugin.select_genre_playlists(config, many_genres(40), week=1)

    spot = lambda sel: [e["genre"] for e in sel if e["kind"] == "spotlight"]
    assert spot(week1) != spot(week2)
    assert spot(week1) == spot(again)                     # same week, same pick
    assert not set(spot(week1)) & {f"Genre{i:02d}" for i in range(14)}


def test_every_genre_gets_a_turn_over_the_weeks(plugin, config):
    seen = set()
    for week in range(1, 54):
        sel = plugin.select_genre_playlists(config, many_genres(20), week=week)
        seen |= {e["genre"] for e in sel if e["kind"] == "spotlight"}

    assert seen == {f"Genre{i:02d}" for i in range(14, 20)}


def test_tiny_genres_are_not_spotlighted(plugin, config):
    items = many_genres(14) + make_tracks("Ballad", 8) + make_tracks("Other", 9)

    sel = plugin.select_genre_playlists(config, items, week=1)

    assert names(sel) == [f"Genre{i:02d}" for i in range(14)]


def test_no_spotlights_when_set_to_zero(plugin, config):
    config.genre_spotlight_slots = 0

    assert len(plugin.select_genre_playlists(config, many_genres(30), week=1)) == 14


def test_genres_below_the_track_or_artist_minimum_are_dropped(plugin, config):
    items = make_tracks("Big", 20) + make_tracks("Few", 3) + make_tracks("OneArtist", 20, artists=["solo"])

    assert names(plugin.select_genre_playlists(config, items)) == ["Big"]


def test_excluded_genres_and_artists_are_left_out(plugin, config):
    config.excluded_genres = ["jazz"]
    config.excluded_artists = ["Banned"]
    items = make_tracks("Jazz", 10) + make_tracks("Zouk", 10) + make_tracks("Cumbia", 10, artists=["Banned"])

    assert names(plugin.select_genre_playlists(config, items)) == ["Zouk"]


def test_empty_library(plugin, config):
    assert plugin.select_genre_playlists(config, []) == []


# ---- artists ------------------------------------------------------------------

def artist_data(spec):
    """spec: {artist: (tracks, albums)}"""
    return {a: {"tracks": [{"Id": f"{a}{i}"} for i in range(t)], "albums": {f"{a}-album{j}" for j in range(al)}}
            for a, (t, al) in spec.items()}


def test_artists_are_ranked_by_track_count_and_capped(plugin, config):
    data = artist_data({f"A{i}": (10 + i, 3) for i in range(30)})

    assert plugin.select_artist_playlists(config, data, 5) == ["A29", "A28", "A27", "A26", "A25"]


def test_artist_minimums_and_non_artists(plugin, config):
    data = artist_data({
        "Real": (20, 3), "FewTracks": (2, 3), "OneAlbum": (20, 1),
        "Various Artists": (500, 40), "Banned": (50, 5),
    })
    config.excluded_artists = ["Banned"]

    assert plugin.select_artist_playlists(config, data, 20) == ["Real"]


def test_a_limit_of_zero_selects_nobody(plugin, config):
    assert plugin.select_artist_playlists(config, artist_data({"A": (20, 3)}), 0) == []


# ---- the whole generator ---------------------------------------------------------

def run_dry(plugin, tmp_path, monkeypatch, items, **settings):
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "managed.json"))
    config = plugin.Config()
    config.playlist_folder = str(tmp_path)
    config.dry_run = True
    config.playlist_types = ["Genre", "Year", "Artist"]
    for key, value in settings.items():
        setattr(config, key, value)
    gen = plugin.PlaylistGenerator(config, logging.getLogger("test"))
    gen.jellyfin = FakeJellyfin(audio=items)
    gen.generate_playlists()
    return gen


def library():
    items = many_genres(30, size=40)
    for i, item in enumerate(items):          # give a few artists many tracks, over several albums
        item["Artists"] = [f"Star {i % 25}"]
        item["Album"] = f"Album {i % 7}"
    return items


def test_a_run_makes_the_capped_number_of_playlists(plugin, tmp_path, monkeypatch):
    gen = run_dry(plugin, tmp_path, monkeypatch, library())

    by_type = {}
    for p in gen.dry_run_plan:
        by_type.setdefault(p["type"], []).append(p["name"])
    assert len(by_type["Genre"]) == 16
    assert len(by_type["Artist"]) <= 20
    assert len(gen.dry_run_plan) <= 45


def test_the_total_cap_wins(plugin, tmp_path, monkeypatch):
    gen = run_dry(plugin, tmp_path, monkeypatch, library(), max_total_playlists=10)

    assert len(gen.dry_run_plan) == 10
    assert {p["type"] for p in gen.dry_run_plan} == {"Genre"}      # genres come first


def test_artist_playlists_get_what_the_genres_leave(plugin, tmp_path, monkeypatch):
    gen = run_dry(plugin, tmp_path, monkeypatch, library(), max_total_playlists=20)

    types = [p["type"] for p in gen.dry_run_plan]
    assert len(types) <= 20
    assert types.count("Genre") == 16
    assert types.count("Artist") <= 4
