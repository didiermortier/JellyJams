"""The dashboard tiles count playlist folders. Hand-made playlists are not genres."""


def _make_folders(root, names):
    for name in names:
        folder = root / name
        folder.mkdir()
        (folder / "playlist.xml").write_text("<Item></Item>")


def test_manual_playlists_are_not_counted_as_genre(webapp_module, tmp_path, monkeypatch):
    _make_folders(tmp_path, [
        "House Radio", "Techno Radio",
        "Back to the 2010s",
        "This is Faithless!",
        "Top Tracks - didier",
        "#1 DEEP", "2012.11 November", "HOUSEMAFIA", "[_] Recently Added",
    ])
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    stats = webapp_module.get_playlist_stats()

    assert stats == {
        "total": 9, "genre": 2, "year": 1, "artist": 1, "personal": 1, "manual": 4,
    }


def test_personal_names_use_the_real_pattern(webapp_module, tmp_path, monkeypatch):
    _make_folders(tmp_path, [
        "Top Tracks - didier", "Discovery Mix - didier",
        "Recent Favorites - didier", "Genre Mix - didier",
    ])
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    assert webapp_module.get_playlist_stats()["personal"] == 4


def test_folders_without_playlist_xml_are_ignored(webapp_module, tmp_path, monkeypatch):
    (tmp_path / "Ghost Radio").mkdir()
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    assert webapp_module.get_playlist_stats()["total"] == 0


def test_playlists_page_uses_the_same_categories(webapp_module, tmp_path, monkeypatch):
    _make_folders(tmp_path, ["House Radio", "#1 DEEP", "Back to the Future"])
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    info = webapp_module.get_detailed_playlist_info()
    by_name = {p["name"]: p["category"] for p in info["playlists"]}

    assert by_name == {"House Radio": "genre", "#1 DEEP": "manual", "Back to the Future": "manual"}
    assert info["stats"]["genre"] == 1
    assert info["stats"]["manual"] == 2
