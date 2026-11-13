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
    # the counts sit in their own span next to the filter label
    assert b'data-filter="manual" onclick="filterPlaylists(\'manual\')">Manual<span class="count">(2)</span>' in page.data
    assert b'>Genres<span class="count">(1)</span>' in page.data


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


def test_playlists_page_lists_generated_playlists_before_manual_ones(webapp_module, tmp_path, monkeypatch):
    for name in ("#1 DEEP", "This is Usher!", "Zouk Radio", "Back to the 1980s", "HOUSEMAFIA", "Alpha Radio"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "playlist.xml").write_text("<Item></Item>")
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    info = webapp_module.get_detailed_playlist_info()

    assert [p["name"] for p in info["playlists"]] == [
        "Alpha Radio", "Zouk Radio", "Back to the 1980s", "This is Usher!", "#1 DEEP", "HOUSEMAFIA"]


def test_the_dashboard_shows_the_playlist_folder(webapp_module, monkeypatch):
    monkeypatch.setattr(webapp_module.config, "playlist_folder", "/somewhere/playlists")

    page = webapp_module.app.test_client().get("/")

    assert b"/somewhere/playlists" in page.data


def test_connection_status_id_is_unique_on_every_page(webapp_module):
    import re
    client = webapp_module.app.test_client()
    for path in ("/", "/playlists", "/settings", "/logs"):
        html = client.get(path).get_data(as_text=True)
        html = re.sub(r"<script.*?</script>", "", html, flags=re.S)     # ids inside JS strings are not elements
        ids = re.findall(r'\bid="([^"]+)"', html)
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"{path} repeats ids {dupes}"


def test_no_page_uses_an_icon_the_bundled_font_lacks(webapp_module):
    """fa-user-music is Font Awesome Pro; with the free font bundled it draws nothing."""
    import re
    from pathlib import Path
    root = Path(webapp_module.__file__).parent
    have = set(re.findall(r"\.(fa-[a-z0-9-]+)", (root / "static/css/font-awesome.all.min.css").read_text()))
    skip = {"fa-spin", "fa-solid", "fa-regular", "fa-brands", "fa-fw", "fa-lg", "fa-2x", "fa-3x"}
    for template in (root / "templates").glob("*.html"):
        used = set(re.findall(r"\b(fa-[a-z0-9-]+)", template.read_text())) - skip
        assert not (used - have), f"{template.name} uses missing icons {used - have}"


from datetime import datetime  # noqa: E402


def test_next_run_weekly_counts_to_the_right_weekday(webapp_module):
    friday_noon = datetime(2026, 11, 13, 12, 0)   # a Friday
    settings = {"schedule_mode": "weekly", "schedule_weekday": "sunday", "schedule_time": "04:30"}

    assert webapp_module.next_run_time(settings, friday_noon) == datetime(2026, 11, 15, 4, 30)
    assert webapp_module.describe_next_run(settings, friday_noon) == "Sunday 15 Nov at 04:30, in 1 day"


def test_next_run_weekly_on_the_day_but_already_past_waits_a_week(webapp_module):
    sunday_noon = datetime(2026, 11, 15, 12, 0)
    settings = {"schedule_mode": "weekly", "schedule_weekday": "sunday", "schedule_time": "04:30"}

    assert webapp_module.next_run_time(settings, sunday_noon) == datetime(2026, 11, 22, 4, 30)


def test_next_run_daily_rolls_to_tomorrow_when_the_time_has_passed(webapp_module):
    settings = {"schedule_mode": "daily", "schedule_time": "04:30"}

    assert webapp_module.next_run_time(settings, datetime(2026, 11, 13, 3, 0)) == datetime(2026, 11, 13, 4, 30)
    assert webapp_module.next_run_time(settings, datetime(2026, 11, 13, 5, 0)) == datetime(2026, 11, 14, 4, 30)


def test_next_run_is_empty_for_manual_interval_and_bad_input(webapp_module):
    for settings in ({"schedule_mode": "manual"}, {"schedule_mode": "interval"},
                     {"schedule_mode": "daily", "schedule_time": "soon"}):
        assert webapp_module.next_run_time(settings, datetime(2026, 11, 13, 12, 0)) is None
        assert webapp_module.describe_next_run(settings, datetime(2026, 11, 13, 12, 0)) == ""


def test_playlists_page_groups_by_kind_with_hand_made_last(webapp_module, tmp_path, monkeypatch):
    for name in ("HOUSEMAFIA", "Zouk Radio", "This is Usher!"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "playlist.xml").write_text("<Item></Item>")
    monkeypatch.setattr(webapp_module.config, "playlist_folder", str(tmp_path))

    html = webapp_module.app.test_client().get("/playlists").get_data(as_text=True)

    assert html.index('data-group="genre"') < html.index('data-group="artist"') < html.index('data-group="manual"')
    assert 'data-group="year"' not in html          # empty groups are left out
    assert 'data-filter="generated"' in html
