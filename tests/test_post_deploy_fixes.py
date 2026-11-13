"""Bugs found on the first live run: the restore net, a silent CLI, a stale dashboard line."""
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import FakeJellyfin

APP = Path(__file__).resolve().parent.parent / "app"


class Session:
    """Records the Jellyfin calls and answers like Jellyfin: no UserId, no playlist."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        outer = self

        class Response:
            def raise_for_status(self_inner):
                if not (params or {}).get("UserId"):
                    raise RuntimeError("400 Guid can't be empty")

            def json(self_inner):
                return {"Items": [{"Id": "t1"}, {"Id": "t2"}]}
        return Response()


@pytest.fixture
def generator(plugin, tmp_path):
    config = plugin.Config()
    config.playlist_folder = str(tmp_path)
    gen = plugin.PlaylistGenerator(config, logging.getLogger("test"))
    gen.jellyfin = FakeJellyfin()
    gen.jellyfin.session = Session()
    gen.jellyfin.config = config
    return gen


def test_a_shared_playlist_is_read_with_a_real_user_id(generator):
    ids = generator._playlist_track_ids("pl1", None)

    assert ids == ["t1", "t2"]
    assert generator.jellyfin.session.calls[0][1] == {"UserId": "u1"}


def test_the_stand_in_user_is_looked_up_once(generator):
    for _ in range(5):
        generator._playlist_track_ids("pl", "")

    assert len(generator.jellyfin.session.calls) == 5
    assert generator._reader_user_id == "u1"


def test_a_given_user_id_is_used_as_is(generator):
    generator._playlist_track_ids("pl1", "someone")

    assert generator.jellyfin.session.calls[0][1] == {"UserId": "someone"}


def test_no_users_at_all_returns_nothing_and_warns(generator, caplog):
    generator.jellyfin.get_users = lambda: []

    with caplog.at_level(logging.WARNING):
        assert generator._playlist_track_ids("pl1", None) == []
    assert "no Jellyfin user" in caplog.text


def test_a_failed_read_is_a_warning_not_a_debug_line(generator, caplog):
    generator.jellyfin.session.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))

    with caplog.at_level(logging.WARNING):
        assert generator._playlist_track_ids("pl1", "u1") == []
    assert "boom" in caplog.text


# ---- command line ------------------------------------------------------------------

def run_cli(*args):
    return subprocess.run([sys.executable, str(APP / "vibecodeplugin.py"), *args],
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(APP)})


def test_help_prints_usage_and_exits():
    result = run_cli("--help")

    assert result.returncode == 0
    assert "--dry-run" in result.stdout and "--retire-stale" in result.stdout


def test_an_unknown_flag_is_an_error_not_a_scheduler():
    result = run_cli("--dryrun")          # a typo; used to start the scheduler loop and never return

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_parse_args_knows_the_two_flags(plugin):
    args = plugin.parse_args(["--dry-run", "--retire-stale"])

    assert args.dry_run and args.retire_stale
    assert not plugin.parse_args([]).dry_run


# ---- dashboard ---------------------------------------------------------------------

@pytest.mark.parametrize("settings,expected", [
    ({"schedule_mode": "weekly", "schedule_weekday": "sunday", "schedule_time": "04:30"}, "Every Sunday at 04:30"),
    ({"schedule_mode": "daily", "schedule_time": "03:00"}, "Every day at 03:00"),
    ({"schedule_mode": "interval", "generation_interval": 12}, "Every 12 hours"),
    ({"schedule_mode": "manual"}, "Manual only"),
])
def test_dashboard_shows_the_real_schedule(webapp_module, monkeypatch, settings, expected):
    full = dict(webapp_module.config_manager.load_settings(), **settings)
    monkeypatch.setattr(webapp_module.config_manager, "load_settings", lambda: full)

    page = webapp_module.app.test_client().get("/")

    assert page.status_code == 200
    assert expected.encode() in page.data
    assert (b"Disabled" in page.data) == (settings["schedule_mode"] == "manual")
