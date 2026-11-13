"""JellyJams only replaces playlists it created, and a dry run changes nothing."""
import json
import logging

import pytest


class FakeJellyfin:
    """Stands in for JellyfinAPI and records every write."""

    def __init__(self, existing=(), audio=None):
        self.existing = [dict(p) for p in existing]
        self.audio = audio or []
        self.created, self.deleted, self.scans = [], [], 0

    # reads
    def test_connection(self):
        return True

    def get_audio_items(self):
        return self.audio

    def get_users(self):
        return [{"Id": "u1", "Name": "didier"}]

    def list_playlists(self, user_id=None):
        return list(self.existing)

    def find_playlists_by_name(self, name, user_id=None):
        return [p for p in self.existing if p["Name"].lower() == name.lower()]

    # writes
    def create_playlist(self, name, track_ids, user_id=None, is_public=True):
        new_id = f"new-{len(self.created) + 1}"
        self.created.append((name, list(track_ids)))
        return {"success": True, "playlist_id": new_id, "track_count": len(track_ids)}

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)
        return True

    def trigger_library_scan(self):
        self.scans += 1
        return True


def tracks(n=6):
    return [{"Id": f"t{i}", "Name": f"Track {i}", "Artists": [f"Artist {i}"], "Genres": ["House"]}
            for i in range(n)]


@pytest.fixture
def env(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "managed.json"))
    monkeypatch.setattr(plugin, "RUN_STATUS_FILE", str(tmp_path / "run_status.json"))
    config = plugin.Config()
    config.playlist_folder = str(tmp_path / "playlists")
    config.playlist_types = ["Genre"]
    (tmp_path / "playlists").mkdir()

    def make(existing=(), record=None, **flags):
        if record is not None:
            (tmp_path / "managed.json").write_text(json.dumps({"playlists": record}))
        for key, value in flags.items():
            setattr(config, key, value)
        gen = plugin.PlaylistGenerator(config, logging.getLogger("test"))
        gen.jellyfin = FakeJellyfin(existing, audio=tracks())
        gen._playlist_track_ids = lambda *a, **k: []
        return gen

    make.tmp = tmp_path
    make.plugin = plugin
    return make


def record_of(env):
    path = env.tmp / "managed.json"
    return json.loads(path.read_text())["playlists"] if path.exists() else None


# ---- ownership ---------------------------------------------------------------

def test_same_name_playlist_not_in_record_is_never_touched_even_on_first_run(env):
    gen = env(existing=[{"Id": "mine-by-hand", "Name": "Techno Radio", "Path": "/x/Techno Radio"}])

    result = gen.save_playlist("Genre", "Techno Radio", tracks())

    assert result is None
    assert gen.jellyfin.deleted == []
    assert gen.jellyfin.created == []


def test_recorded_playlist_is_replaced(env):
    gen = env(existing=[{"Id": "old", "Name": "Techno Radio", "Path": "/x/Techno Radio"}],
              record={"Techno Radio": "old"})

    gen.save_playlist("Genre", "Techno Radio", tracks())

    assert gen.jellyfin.deleted == ["old"]
    assert [name for name, _ in gen.jellyfin.created] == ["Techno Radio"]
    assert record_of(env) == {"Techno Radio": "new-1"}


def test_a_duplicate_that_is_not_recorded_blocks_the_replace(env):
    gen = env(existing=[
        {"Id": "old", "Name": "Techno Radio", "Path": "/x/a"},
        {"Id": "hand", "Name": "Techno Radio", "Path": "/x/b"},
    ], record={"Techno Radio": "old"})

    assert gen.save_playlist("Genre", "Techno Radio", tracks()) is None
    assert gen.jellyfin.deleted == []


def test_new_playlist_is_created_and_recorded(env):
    gen = env()

    gen.save_playlist("Genre", "Techno Radio", tracks())

    assert [name for name, _ in gen.jellyfin.created] == ["Techno Radio"]
    assert record_of(env) == {"Techno Radio": "new-1"}


def test_names_jellyjams_does_not_generate_are_refused(env):
    gen = env(existing=[{"Id": "x", "Name": "HOUSEMAFIA", "Path": "/x/HOUSEMAFIA"}])

    assert gen.save_playlist("Genre", "HOUSEMAFIA", tracks()) is None
    assert gen.jellyfin.deleted == [] and gen.jellyfin.created == []


# ---- adoption is opt in -------------------------------------------------------

EXISTING = [
    {"Id": "a", "Name": "House Radio", "Path": "/x/House Radio"},
    {"Id": "b", "Name": "This is Faithless!", "Path": "/x/f"},
    {"Id": "c", "Name": "#1 DEEP", "Path": "/x/#1 DEEP"},
    {"Id": "d", "Name": "HOUSEMAFIA", "Path": "/x/HOUSEMAFIA"},
]


def test_adoption_is_off_by_default_and_writes_nothing(env):
    gen = env(existing=EXISTING)

    assert gen.adopt_existing_playlists() == 0
    assert record_of(env) is None


def test_adoption_when_asked_takes_only_generated_names(env):
    gen = env(existing=EXISTING, adopt_existing_playlists=True)

    assert gen.adopt_existing_playlists() == 2
    assert record_of(env) == {"House Radio": "a", "This is Faithless!": "b"}


def test_adoption_does_not_replace_a_recorded_id(env):
    gen = env(existing=EXISTING, record={"House Radio": "a"}, adopt_existing_playlists=True)

    assert gen.adopt_existing_playlists() == 1
    assert record_of(env) == {"House Radio": "a", "This is Faithless!": "b"}


# ---- dry run ------------------------------------------------------------------

def test_dry_run_changes_nothing_and_reports_the_plan(env):
    gen = env(existing=[
        {"Id": "old", "Name": "Techno Radio", "Path": "/x/Techno Radio"},
        {"Id": "hand", "Name": "Trance Radio", "Path": "/x/Trance Radio"},
    ], record={"Techno Radio": "old"}, dry_run=True)

    gen.save_playlist("Genre", "Techno Radio", tracks())     # recorded: would replace
    gen.save_playlist("Genre", "Trance Radio", tracks())     # same name, not recorded: would skip
    gen.save_playlist("Genre", "Deep House Radio", tracks()) # absent: would create

    assert gen.jellyfin.created == [] and gen.jellyfin.deleted == []
    assert record_of(env) == {"Techno Radio": "old"}
    assert not (env.tmp / "playlists" / "Deep House Radio").exists()
    assert {(p["name"], p["action"]) for p in gen.dry_run_plan} == {
        ("Techno Radio", "replace"),
        ("Trance Radio", "skip"),
        ("Deep House Radio", "create"),
    }


def test_dry_run_of_a_whole_run_writes_nothing(env):
    gen = env(existing=[{"Id": "old", "Name": "Electronic Radio", "Path": "/x/e"}],
              record={"Electronic Radio": "old"}, dry_run=True)

    gen.generate_playlists()

    assert gen.jellyfin.created == [] and gen.jellyfin.deleted == []
    assert gen.jellyfin.scans == 0
    assert not (env.tmp / "run_status.json").exists()
    assert [p["action"] for p in gen.dry_run_plan] == ["replace"]


def test_dry_run_report_lists_generated_looking_playlists_it_would_not_manage(env):
    gen = env(existing=EXISTING, dry_run=True)

    report = gen.dry_run_report()

    assert sorted(report["unrecorded_generated"]) == ["House Radio", "This is Faithless!"]


def test_the_api_refuses_to_write_in_a_dry_run(env):
    plugin = env.plugin
    config = plugin.Config()
    config.dry_run = True
    api = plugin.JellyfinAPI(config, logging.getLogger("test"))

    with pytest.raises(plugin.DryRunWriteError):
        api.create_playlist("X Radio", ["t1"], "u1")
    with pytest.raises(plugin.DryRunWriteError):
        api.delete_playlist("abc")
    with pytest.raises(plugin.DryRunWriteError):
        api.trigger_library_scan()
