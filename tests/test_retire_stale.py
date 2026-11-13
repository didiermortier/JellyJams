"""Old generated playlists that a run no longer makes can be retired, safely."""
import json
import logging

import pytest

from fakes import FakeJellyfin
from test_genre_selection import make_tracks


def pl(id_, name):
    return {"Id": id_, "Name": name, "Path": f"/config/data/data/playlists/{name}"}


@pytest.fixture
def setup(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "MANAGED_PLAYLISTS_FILE", str(tmp_path / "managed.json"))
    monkeypatch.setattr(plugin, "RUN_STATUS_FILE", str(tmp_path / "run_status.json"))
    folders = tmp_path / "playlists"
    folders.mkdir()

    def make(existing, record, audio=None, **flags):
        (tmp_path / "managed.json").write_text(json.dumps({"playlists": record}))
        for p in existing:                                  # the folder Jellyfin leaves on disk
            (folders / p["Name"]).mkdir(exist_ok=True)
        config = plugin.Config()
        config.playlist_folder = str(folders)
        config.playlist_types = ["Genre", "Year", "Artist"]
        for key, value in flags.items():
            setattr(config, key, value)
        gen = plugin.PlaylistGenerator(config, logging.getLogger("test"))
        gen.jellyfin = FakeJellyfin(existing, audio=audio if audio is not None else make_tracks("Jazz", 10))
        gen._playlist_track_ids = lambda *a, **k: []
        return gen

    make.tmp, make.folders = tmp_path, folders
    return make


def record_of(setup):
    return json.loads((setup.tmp / "managed.json").read_text())["playlists"]


OLD = [pl("g1", "Gangsta Rap Radio"), pl("g2", "Samba Radio"), pl("a1", "This is Nobody!"),
       pl("d1", "Back to the 1970s"), pl("j1", "Jazz Radio")]
OLD_RECORD = {p["Name"]: p["Id"] for p in OLD}


def test_a_real_run_retires_what_it_no_longer_makes_and_keeps_the_rest(setup):
    gen = setup(OLD, OLD_RECORD, retire_stale_playlists=True)

    counts = gen.generate_playlists()

    # Jazz Radio is made again (replaced). The test library has tracks from 2015 only, so
    # "Back to the 1970s" is retired along with the genres this run does not make. No
    # artist qualifies, so no artist playlist is retired: that kind was not made at all.
    assert sorted(gen.jellyfin.deleted) == sorted(["g1", "g2", "d1", "j1"])
    assert counts["playlists_retired"] == 3
    assert "Gangsta Rap Radio" not in record_of(setup)
    assert not (setup.folders / "Gangsta Rap Radio").exists()
    assert (setup.folders / "This is Nobody!").exists()


def test_nothing_is_retired_unless_asked(setup):
    gen = setup(OLD, OLD_RECORD)

    gen.generate_playlists()

    assert "g1" not in gen.jellyfin.deleted and "g2" not in gen.jellyfin.deleted


def test_only_recorded_generated_playlists_are_retired(setup):
    existing = OLD + [pl("h1", "HOUSEMAFIA"), pl("u1", "Techno Radio"), pl("p1", "Top Tracks - didier")]
    record = dict(OLD_RECORD, **{"HOUSEMAFIA": "h1", "Top Tracks - didier": "p1"})   # a polluted record
    gen = setup(existing, record, retire_stale_playlists=True)

    gen.generate_playlists()

    deleted = set(gen.jellyfin.deleted)
    assert "h1" not in deleted           # not a generated name, even though it is in the record
    assert "p1" not in deleted           # personal mixes are never retired
    assert "u1" not in deleted           # generated-looking but not in the record


def test_a_playlist_renamed_since_is_left_alone(setup):
    existing = [pl("g1", "My Own Mix"), pl("j1", "Jazz Radio")]       # g1 was "Gangsta Rap Radio"
    gen = setup(existing, {"Gangsta Rap Radio": "g1", "Jazz Radio": "j1"}, retire_stale_playlists=True)

    gen.generate_playlists()

    assert "g1" not in gen.jellyfin.deleted


def test_a_type_that_is_switched_off_is_not_touched(setup):
    gen = setup(OLD, OLD_RECORD, retire_stale_playlists=True, playlist_types=["Genre"])

    gen.generate_playlists()

    assert not {"a1", "d1"} & set(gen.jellyfin.deleted)


def test_a_run_that_made_no_genres_does_not_wipe_them(setup):
    gen = setup(OLD, OLD_RECORD, audio=[{"Id": "x", "Artists": ["a"]}], retire_stale_playlists=True)

    gen.generate_playlists()

    assert gen.jellyfin.deleted == []


def test_an_aborted_run_retires_nothing(setup):
    gen = setup(OLD, OLD_RECORD, audio=[], retire_stale_playlists=True)

    gen.generate_playlists()

    assert gen.jellyfin.deleted == []


def test_dry_run_previews_the_retirement_and_deletes_nothing(setup):
    gen = setup(OLD, OLD_RECORD, dry_run=True)

    gen.generate_playlists()
    retire = gen.dry_run_report()["retire"]

    assert sorted(e["name"] for e in retire) == ["Back to the 1970s", "Gangsta Rap Radio", "Samba Radio"]
    assert gen.jellyfin.deleted == []
    assert (setup.folders / "Gangsta Rap Radio").exists()
    assert record_of(setup) == OLD_RECORD


def test_adopt_then_retire_in_one_pass_cleans_an_install_without_a_record(setup):
    gen = setup(OLD, {}, adopt_existing_playlists=True, retire_stale_playlists=True)

    gen.generate_playlists()

    assert {"g1", "g2"} <= set(gen.jellyfin.deleted)


def test_dry_run_with_adopt_shows_what_the_real_run_would_do(setup):
    gen = setup(OLD, {}, adopt_existing_playlists=True, dry_run=True)

    gen.generate_playlists()
    report = gen.dry_run_report()

    assert sorted(e["name"] for e in report["retire"]) == ["Back to the 1970s", "Gangsta Rap Radio", "Samba Radio"]
    actions = {p["name"]: p["action"] for p in gen.dry_run_plan}
    assert actions["Jazz Radio"] == "replace"


def test_a_recorded_name_with_curly_punctuation_still_counts_as_planned(setup):
    """The record can hold a name as Jellyfin spelled it; the run plans it normalized."""
    gen = setup([pl("a1", "This is Guns N\u2019 Roses!")], {"This is Guns N\u2019 Roses!": "a1"},
                retire_stale_playlists=True)
    gen.planned_names = {"This is Guns N' Roses!", "Jazz Radio"}

    assert gen.plan_retirement() == []


def test_dry_run_endpoint_does_not_change_the_live_config(webapp_module, monkeypatch):
    webapp_module.config.excluded_genres = ["x"]
    seen = {}
    import vibecodeplugin

    class Spy(vibecodeplugin.PlaylistGenerator):
        def generate_playlists(self):
            self.config.excluded_genres.append("leak")
            seen["dry"] = self.config.dry_run
            return {}

        def dry_run_report(self):
            return {"plan": [], "unrecorded_generated": [], "retire": []}

    monkeypatch.setattr(webapp_module, "PlaylistGenerator", Spy)
    response = webapp_module.app.test_client().post("/api/dry_run", json={})

    assert response.status_code == 200 and seen["dry"] is True
    assert webapp_module.config.excluded_genres == ["x"]
    assert webapp_module.config.dry_run is False
