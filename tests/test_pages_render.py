"""The dashboard and playlists pages render and show the manual count."""


def test_pages_show_manual_counts(webapp_module, tmp_path, monkeypatch):
    for name in ("House Radio", "#1 DEEP", "HOUSEMAFIA"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "playlist.xml").write_text("<Item></Item>")
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))
    client = webapp_module.app.test_client()

    home = client.get("/")
    assert home.status_code == 200
    assert b"Manual Playlists" in home.data

    page = client.get("/playlists")
    assert page.status_code == 200
    assert b"Manual (2)" in page.data
    assert b"Genres (1)" in page.data
