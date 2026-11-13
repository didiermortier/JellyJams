"""Which playlist names JellyJams treats as its own, and how it files them."""
import pytest


GENERATED = [
    ("Techno Radio", "genre"),
    ("Drum & Bass Radio", "genre"),
    ("alternative hip hop Radio", "genre"),
    ("Back to the 1990s", "year"),
    ("Back to the 2020s", "year"),
    ("This is Tiësto!", "artist"),
    ("This is AC/DC!", "artist"),
    ("Top Tracks - didier", "personal"),
    ("Discovery Mix - didier", "personal"),
    ("Recent Favorites - didier", "personal"),
    ("Genre Mix - didier", "personal"),
]

# Names taken from a real library: hand-made playlists that must never be claimed.
MANUAL = [
    "#9 AFROBEAT",
    "#1 DEEP",
    "HOUSEMAFIA",
    "2012.11 November",
    "[/] Recently Added",
    "TRAP & RHITHM",
    "Back to the Future",       # starts like a decade playlist, is not one
    "This is my jam",           # starts like an artist playlist, no trailing "!"
    "Genre experiments",        # starts with a personal prefix word, is not one
    "Recent finds",
    "Discovery",
    "Radio",                    # no genre in front of it
    "",
]


@pytest.mark.parametrize("name,category", GENERATED)
def test_generated_names_get_their_category(plugin, name, category):
    assert plugin.categorize_playlist(name) == category


@pytest.mark.parametrize("name", MANUAL)
def test_manual_names_are_manual(plugin, name):
    assert plugin.categorize_playlist(name) == "manual"


@pytest.mark.parametrize("name,_category", GENERATED)
def test_is_generated_true_for_generated(plugin, name, _category):
    assert plugin.is_generated_playlist_name(name) is True


@pytest.mark.parametrize("name", MANUAL)
def test_is_generated_false_for_manual(plugin, name):
    assert plugin.is_generated_playlist_name(name) is False


def test_is_generated_agrees_with_categorize(plugin):
    for name, _ in GENERATED:
        assert plugin.is_generated_playlist_name(name) == (plugin.categorize_playlist(name) != "manual")
    for name in MANUAL:
        assert plugin.is_generated_playlist_name(name) == (plugin.categorize_playlist(name) != "manual")
