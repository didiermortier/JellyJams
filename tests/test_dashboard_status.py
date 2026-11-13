"""The dashboard's run detail, stored preview and progress bar."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeJellyfin


def many_tracks(n=40):
    return [{"Id": f"t{i}", "Name": f"Track {i}", "Artists": [f"Artist {i}"], "Genres": ["House"]}
            for i in range(n)]


@pytest.fixture
def run(plugin, tmp_path, monkeypatch):
    """A generator against a fake Jellyfin, with every state file inside tmp_path."""
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "managed.json"))
    monkeypatch.setattr(plugin, "RUN_STATUS_FILE", str(tmp_path / "run_status.json"))
    monkeypatch.setattr(plugin, "PROGRESS_FILE", str(tmp_path / "progress.json"))
    config = plugin.Config()
    config.playlist_folder = str(tmp_path / "playlists")
    config.playlist_types = ["Genre"]
    (tmp_path / "playlists").mkdir()

    def make(dry_run=False, existing=()):
        config.dry_run = dry_run
        gen = plugin.PlaylistGenerator(config, logging.getLogger("test-status"))
        gen.jellyfin = FakeJellyfin(existing, audio=many_tracks())
        gen._playlist_track_ids = lambda *a, **k: []
        return gen

    make.tmp = tmp_path
    return make


def read(path):
    return json.loads(path.read_text())


# ---- last run detail ----------------------------------------------------------

def test_a_finished_run_records_how_long_it_took_and_how_many_problems(run):
    gen = run()
    gen.generate_playlists()

    status = read(run.tmp / "run_status.json")
    assert status["success"] is True
    assert isinstance(status["duration_seconds"], int) and status["duration_seconds"] >= 0
    assert status["errors"] == 0
    assert status["warnings"] >= 0


def test_errors_logged_during_a_run_are_counted(run):
    gen = run()
    original = gen._generate_playlists

    def noisy():
        gen.logger.error("one thing went wrong")
        gen.logger.error("another thing went wrong")
        gen.logger.warning("and a warning")
        return original()

    gen._generate_playlists = noisy
    gen.generate_playlists()

    status = read(run.tmp / "run_status.json")
    assert status["errors"] == 2
    assert status["warnings"] >= 1      # ours; the run may log its own as well


def test_a_crashed_run_is_recorded_as_failed_with_its_duration(run):
    gen = run()

    def boom():
        raise RuntimeError("jellyfin fell over")

    gen._generate_playlists = boom
    with pytest.raises(RuntimeError):
        gen.generate_playlists()

    status = read(run.tmp / "run_status.json")
    assert status["success"] is False
    assert "jellyfin fell over" in status["summary"]
    assert status["errors"] >= 1
    assert "duration_seconds" in status


def test_the_error_counter_is_removed_after_a_run(run):
    gen = run()
    before = list(gen.logger.handlers)
    gen.generate_playlists()
    assert list(gen.logger.handlers) == before


def test_a_dry_run_writes_no_status_and_no_progress(run):
    gen = run(dry_run=True)
    gen.generate_playlists()

    assert not (run.tmp / "run_status.json").exists()
    assert not (run.tmp / "progress.json").exists()


# ---- progress --------------------------------------------------------------------

def test_progress_says_running_while_a_run_writes_playlists(run):
    gen = run()
    seen = []
    real = gen.jellyfin.create_playlist

    def spy(*args, **kwargs):
        seen.append(read(run.tmp / "progress.json"))
        return real(*args, **kwargs)

    gen.jellyfin.create_playlist = spy
    gen.generate_playlists()

    assert seen, "no playlist was written, so the test proves nothing"
    assert all(p["running"] for p in seen)
    assert seen[0]["stage"]


def test_progress_is_marked_finished_when_the_run_ends(run):
    gen = run()
    gen.generate_playlists()
    assert read(run.tmp / "progress.json")["running"] is False


def test_progress_is_marked_finished_when_the_run_crashes(run):
    gen = run()
    gen._generate_playlists = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    with pytest.raises(RuntimeError):
        gen.generate_playlists()
    assert read(run.tmp / "progress.json")["running"] is False


def test_progress_expects_about_as_many_playlists_as_last_time(run):
    (run.tmp / "managed.json").write_text(json.dumps({"playlists": {f"P{i}": f"id{i}" for i in range(7)}}))
    gen = run()
    seen = []
    real = gen.jellyfin.create_playlist

    def spy(*args, **kwargs):
        seen.append(read(run.tmp / "progress.json"))
        return real(*args, **kwargs)

    gen.jellyfin.create_playlist = spy
    gen.generate_playlists()

    assert seen[0]["expected"] == 7


def _write_progress(tmp, **fields):
    payload = {"running": True, "stage": "Genre playlists", "written": 10, "expected": 40,
               "started": "x", "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    payload.update(fields)
    (tmp / "progress.json").write_text(json.dumps(payload))


def test_progress_endpoint_gives_a_percentage(webapp_module, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "PROGRESS_PATH", str(tmp_path / "progress.json"))
    _write_progress(tmp_path)

    body = webapp_module.app.test_client().get("/api/progress").get_json()

    assert body["running"] is True
    assert body["percent"] == 25
    assert body["stage"] == "Genre playlists"


def test_progress_endpoint_never_reaches_100_while_running(webapp_module, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "PROGRESS_PATH", str(tmp_path / "progress.json"))
    _write_progress(tmp_path, written=60, expected=40)

    assert webapp_module.app.test_client().get("/api/progress").get_json()["percent"] == 99


def test_progress_endpoint_has_no_percentage_without_an_expected_total(webapp_module, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "PROGRESS_PATH", str(tmp_path / "progress.json"))
    _write_progress(tmp_path, expected=0)

    body = webapp_module.app.test_client().get("/api/progress").get_json()
    assert body["running"] is True and body["percent"] is None


def test_progress_endpoint_ignores_a_run_that_died(webapp_module, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "PROGRESS_PATH", str(tmp_path / "progress.json"))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    _write_progress(tmp_path, updated=old)

    assert webapp_module.app.test_client().get("/api/progress").get_json() == {"running": False}


def test_progress_endpoint_without_a_file_or_with_garbage(webapp_module, tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    monkeypatch.setattr(webapp_module, "PROGRESS_PATH", str(path))
    client = webapp_module.app.test_client()

    assert client.get("/api/progress").get_json() == {"running": False}
    path.write_text("{not json")
    assert client.get("/api/progress").get_json() == {"running": False}


# ---- stored preview ------------------------------------------------------------------

REPORT = {
    "plan": [
        {"name": "Pop Rap Radio", "type": "Genre", "action": "create", "tracks": 50},
        {"name": "House Radio", "type": "Genre", "action": "replace", "tracks": 100},
        {"name": "Techno Radio", "type": "Genre", "action": "replace", "tracks": 100},
        {"name": "HOUSEMAFIA", "type": "Genre", "action": "skip", "tracks": 10},
    ],
    "retire": [{"name": "This is Clockwork!", "id": "c1", "kind": "artist", "path": "/x"}],
    "unrecorded_generated": [],
}


def test_a_stored_preview_becomes_counts_and_names(webapp_module):
    saved = {"when": "2026-11-13T10:00:00+00:00", "report": REPORT}

    summary = webapp_module.preview_summary(saved)

    assert summary["create"] == ["Pop Rap Radio"]
    assert summary["replace"] == 2
    assert summary["skip"] == ["HOUSEMAFIA"]
    assert summary["retire"] == ["This is Clockwork!"]
    assert summary["outdated"] is False


def test_a_preview_from_before_the_last_run_is_outdated(webapp_module):
    saved = {"when": "2026-11-13T10:00:00+00:00", "report": REPORT}

    assert webapp_module.preview_summary(saved, "2026-11-13T12:03:00+00:00")["outdated"] is True
    assert webapp_module.preview_summary(saved, "2026-11-13T09:00:00+00:00")["outdated"] is False
    assert webapp_module.preview_summary(saved, None)["outdated"] is False


def test_no_stored_preview_gives_none(webapp_module):
    assert webapp_module.preview_summary(None) is None
    assert webapp_module.preview_summary({}) is None


def test_preview_endpoint_roundtrip(webapp_module, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "LAST_PREVIEW_FILE", str(tmp_path / "last_preview.json"))
    monkeypatch.setattr(webapp_module, "RUN_STATUS_PATH", str(tmp_path / "run_status.json"))
    client = webapp_module.app.test_client()

    assert client.get("/api/last_preview").get_json() == {"preview": None}

    webapp_module.save_last_preview(REPORT)
    preview = client.get("/api/last_preview").get_json()["preview"]

    assert preview["create"] == ["Pop Rap Radio"]
    assert preview["outdated"] is False

    # a run that finishes later makes it outdated
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    (tmp_path / "run_status.json").write_text(json.dumps({"last_run_finished": later}))
    assert client.get("/api/last_preview").get_json()["preview"]["outdated"] is True


def test_the_dry_run_endpoint_keeps_its_report_for_the_dashboard(webapp_module, plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "LAST_PREVIEW_FILE", str(tmp_path / "last_preview.json"))
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "m.json"))
    monkeypatch.setattr(plugin, "RUN_STATUS_FILE", str(tmp_path / "r.json"))
    monkeypatch.setattr(plugin, "PROGRESS_FILE", str(tmp_path / "p.json"))
    monkeypatch.setattr(webapp_module.PlaylistGenerator, "generate_playlists", lambda self: None)
    monkeypatch.setattr(webapp_module.PlaylistGenerator, "dry_run_report", lambda self: REPORT)

    response = webapp_module.app.test_client().post("/api/dry_run", json={})

    assert response.status_code == 200
    stored = read(tmp_path / "last_preview.json")
    assert stored["report"]["plan"][0]["name"] == "Pop Rap Radio"
    assert stored["when"]


def test_the_dashboard_has_the_preview_card_and_progress_bar(webapp_module):
    page = webapp_module.app.test_client().get("/").get_data(as_text=True)

    assert 'id="previewBox"' in page
    assert 'id="runProgress"' in page
    assert "/api/last_preview" in page and "/api/progress" in page


def test_the_dashboard_still_reads_the_health_body_when_it_answers_503(webapp_module):
    """/api/health is 503 after a failed run; the dashboard must show that run, not 'Unavailable'."""
    page = webapp_module.app.test_client().get("/").get_data(as_text=True)

    assert "xhr.responseJSON" in page
