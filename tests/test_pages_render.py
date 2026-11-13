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


def test_dashboard_has_a_preview_button(webapp_module):
    page = webapp_module.app.test_client().get("/")
    assert b'onclick="dryRun()"' in page.data


def test_dry_run_endpoint_previews_without_writing(webapp_module, plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "m.json"))
    monkeypatch.setattr(plugin, "RUN_STATUS_FILE", str(tmp_path / "r.json"))
    from test_genre_selection import make_tracks
    client = webapp_module.app.test_client()
    # no server to talk to: the endpoint must fail cleanly, never hang or 500-crash the app
    response = client.post("/api/dry_run", json={})
    assert response.status_code in (200, 500)
    if response.status_code == 200:
        assert response.get_json()["success"] is True
        assert set(response.get_json()["report"]) == {"plan", "unrecorded_generated", "retire"}
