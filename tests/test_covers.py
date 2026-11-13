"""Genre covers: drawn from files in the repo, deterministic, and found regardless of case."""
import hashlib
import importlib.util
import logging
import subprocess
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
COVER_DIR = ROOT / "app" / "cover"
BACKGROUND = COVER_DIR / "Fallback Radio.jpg"


@pytest.fixture
def cover(plugin):
    import genre_cover
    return genre_cover


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- drawing ------------------------------------------------------------------

@pytest.mark.parametrize("genre", [
    "House", "Progressive House", "Singer-Songwriter", "R&B", "Drum & Bass",
    "Experimental Industrial Noise Ambient Drone Revival",
])
def test_covers_are_square_jpegs(cover, tmp_path, genre):
    out = cover.render_genre_cover(BACKGROUND, genre, tmp_path / "c.jpg")

    with Image.open(out) as image:
        assert image.size == (600, 600)
        assert image.format == "JPEG"


def test_the_same_genre_always_draws_the_same_cover(cover, tmp_path):
    a = cover.render_genre_cover(BACKGROUND, "Trance", tmp_path / "a.jpg")
    b = cover.render_genre_cover(BACKGROUND, "trance", tmp_path / "b.jpg")      # case does not matter

    assert digest(a) == digest(b)


def test_different_genres_look_different(cover, tmp_path):
    seen = {digest(cover.render_genre_cover(BACKGROUND, g, tmp_path / f"{i}.jpg"))
            for i, g in enumerate(["House", "Trance", "Techno", "Dubstep", "Disco", "Funk"])}

    assert len(seen) == 6


def test_the_name_is_drawn(cover, tmp_path):
    """The text is white on a tinted photo: the middle band has far more white than the top edge."""
    out = cover.render_genre_cover(BACKGROUND, "Trance", tmp_path / "c.jpg")
    image = Image.open(out).convert("L")

    def white_share(box):
        region = image.crop(box)
        return sum(1 for p in region.getdata() if p > 235) / (region.width * region.height)

    assert white_share((0, 220, 600, 380)) > 3 * white_share((0, 0, 600, 60)) + 0.01


def test_text_fits_the_width_for_long_names(cover, tmp_path):
    from PIL import ImageDraw
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for genre in ("Progressive House", "Singer-Songwriter", "Experimental Industrial Noise Ambient Drone"):
        best = max((cover._fit(probe, lines, 504)[1], lines) for lines in cover._layouts(genre))
        font, size = cover._fit(probe, best[1], 504)
        assert max(probe.textlength(line, font=font) for line in best[1]) <= 504 or size == 24


# ---- finding shipped covers -----------------------------------------------------

def test_find_cover_ignores_case_and_extension(cover, tmp_path):
    (tmp_path / "Hip Hop Radio.JPG").write_bytes(b"x")
    (tmp_path / "Techno Radio.png").write_bytes(b"x")

    assert cover.find_cover(tmp_path, ["hip hop radio"]).name == "Hip Hop Radio.JPG"
    assert cover.find_cover(tmp_path, ["Techno Radio"]).name == "Techno Radio.png"
    assert cover.find_cover(tmp_path, ["Nope Radio"]) is None
    assert cover.find_cover(tmp_path / "missing", ["x"]) is None


def make_generator(plugin, tmp_path, monkeypatch, shipped=()):
    cover_dir = tmp_path / "data_cover"
    cover_dir.mkdir()
    for name in shipped:
        (cover_dir / name).write_bytes(b"shipped")
    monkeypatch.setattr(plugin, "COVER_SOURCE_DIR", str(cover_dir))
    config = plugin.Config()
    config.playlist_folder = str(tmp_path / "playlists")
    gen = plugin.PlaylistGenerator(config, logging.getLogger("test"))
    folder = tmp_path / "pl"
    folder.mkdir()
    return gen, cover_dir, folder


def test_a_shipped_cover_is_used_whatever_the_case(plugin, tmp_path, monkeypatch):
    gen, _, folder = make_generator(plugin, tmp_path, monkeypatch, shipped=["Hip Hop Radio.jpg"])

    assert gen._apply_genre_cover_art("hip hop Radio", "hip hop", folder) is True
    assert (folder / "cover.jpg").read_bytes() == b"shipped"


def test_a_genre_without_a_shipped_cover_gets_one_drawn(plugin, tmp_path, monkeypatch):
    gen, cover_dir, folder = make_generator(plugin, tmp_path, monkeypatch)
    (cover_dir / "Fallback Radio.jpg").write_bytes(BACKGROUND.read_bytes())

    assert gen._apply_genre_cover_art("Zouk Radio", "Zouk", folder) is True
    with Image.open(folder / "cover.webp") as image:
        assert image.size == (600, 600)


def test_no_template_means_no_cover_not_a_crash(plugin, tmp_path, monkeypatch):
    gen, _, folder = make_generator(plugin, tmp_path, monkeypatch)

    assert gen._apply_genre_cover_art("Zouk Radio", "Zouk", folder) is False


# ---- what ships ------------------------------------------------------------------

def tool():
    spec = importlib.util.spec_from_file_location("make_genre_covers", ROOT / "tools" / "make_genre_covers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_genre_the_tool_lists_has_a_shipped_cover(cover):
    missing = [g for g in tool().GENRES if cover.find_cover(COVER_DIR, [f"{g} Radio"]) is None]

    assert missing == []


def test_every_genre_group_has_a_shipped_cover(plugin, cover):
    groups = plugin.Config().genre_mappings.keys()
    missing = [g for g in groups if cover.find_cover(COVER_DIR, [f"{g} Radio"]) is None]

    assert missing == []


def test_the_top_genres_of_a_house_library_have_covers(cover):
    for genre in ("House", "Progressive House", "Deep House", "Electro House", "Tech House", "Trance",
                  "Techno", "Dubstep", "Hip Hop", "Trap", "Pop", "Electronic", "Schlager", "Moombahton"):
        assert cover.find_cover(COVER_DIR, [f"{genre} Radio"]), genre


def test_shipped_covers_are_the_expected_size(cover):
    for path in COVER_DIR.glob("*Radio.jpg"):
        with Image.open(path) as image:
            assert image.size == (600, 600), path.name


def test_the_font_ships_with_the_app(cover):
    assert cover.FONT_PATH.is_file()


# ---- syncing covers into the data folder -----------------------------------------

def run_sync(src, dst):
    return subprocess.run([str(ROOT / "app" / "sync_covers.sh"), str(src), str(dst)],
                          capture_output=True, text=True)


def test_sync_copies_new_covers_and_never_overwrites(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(), dst.mkdir()
    (src / "Jazz Radio.jpg").write_bytes(b"shipped jazz")
    (src / "Trance Radio.jpg").write_bytes(b"shipped trance")
    (dst / "Jazz Radio.jpg").write_bytes(b"my own jazz cover")

    result = run_sync(src, dst)

    assert result.returncode == 0, result.stderr
    assert (dst / "Jazz Radio.jpg").read_bytes() == b"my own jazz cover"
    assert (dst / "Trance Radio.jpg").read_bytes() == b"shipped trance"
    assert "1 new" in result.stdout


def test_sync_creates_the_data_folder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "A B Radio.jpg").write_bytes(b"x")

    assert run_sync(src, tmp_path / "new" / "cover").returncode == 0
    assert (tmp_path / "new" / "cover" / "A B Radio.jpg").exists()
