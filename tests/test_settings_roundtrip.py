"""The new limits can be saved from the settings page and are read back by the generator."""
import json


def test_generator_reads_the_limits_from_settings_json(plugin, tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "max_genre_playlists": "9", "genre_spotlight_slots": "0",       # the form posts strings
        "max_artist_playlists": 12, "max_total_playlists": "30",
    }))
    monkeypatch.setattr(plugin, "SETTINGS_FILE", str(settings_file))

    config = plugin.Config()

    assert (config.max_genre_playlists, config.genre_spotlight_slots,
            config.max_artist_playlists, config.max_total_playlists) == (9, 0, 12, 30)


def test_defaults_are_sensible_without_a_settings_file(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "SETTINGS_FILE", str(tmp_path / "missing.json"))

    config = plugin.Config()

    assert (config.max_genre_playlists, config.genre_spotlight_slots,
            config.max_artist_playlists, config.max_total_playlists) == (14, 2, 20, 45)


def test_saving_from_the_settings_page_applies_and_keeps_other_keys(webapp_module, plugin, tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"min_tracks_per_playlist": 7}))
    monkeypatch.setattr(plugin, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(webapp_module.config_manager, "config_file", str(settings_file))

    response = webapp_module.app.test_client().post("/api/settings", json={
        "max_genre_playlists": "9", "genre_spotlight_slots": "0",
        "max_artist_playlists": "12", "max_total_playlists": "30",
    })

    assert response.status_code == 200
    saved = json.loads(settings_file.read_text())
    assert saved["min_tracks_per_playlist"] == 7                 # a partial save keeps the rest
    assert saved["genre_spotlight_slots"] in ("0", 0)
    live = webapp_module.config
    assert (live.max_genre_playlists, live.genre_spotlight_slots,
            live.max_artist_playlists, live.max_total_playlists) == (9, 0, 12, 30)


def test_settings_page_shows_the_limits(webapp_module, plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "SETTINGS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(webapp_module.config_manager, "config_file", str(tmp_path / "none.json"))
    webapp_module.config.genre_spotlight_slots = 0

    page = webapp_module.app.test_client().get("/settings")

    assert page.status_code == 200
    for field in (b'id="max_genre_playlists"', b'id="genre_spotlight_slots"',
                  b'id="max_artist_playlists"', b'id="max_total_playlists"'):
        assert field in page.data
    assert b'id="genre_spotlight_slots" name="genre_spotlight_slots" value="0"' in page.data
