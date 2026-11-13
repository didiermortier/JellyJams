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


# ---- artist covers --------------------------------------------------------------

def test_artist_cover_is_drawn_from_the_artists_photo(plugin, tmp_path, monkeypatch):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (400, 400), (120, 40, 200)).save(buf, "JPEG")
    gen, _, folder = make_generator(plugin, tmp_path, monkeypatch)

    assert gen._generate_custom_cover_art(buf.getvalue(), "Nora en Pure", folder / "cover.webp")
    with Image.open(folder / "cover.webp") as image:
        assert image.size == (600, 600)
    assert not (folder / "cover.webp.tmp.webp").exists() and not (folder / "cover.webp.bg.jpg").exists()


def test_artist_cover_fails_cleanly_on_a_broken_photo(plugin, tmp_path, monkeypatch):
    gen, _, folder = make_generator(plugin, tmp_path, monkeypatch)

    assert gen._generate_custom_cover_art(b"not an image", "Nobody", folder / "cover.webp") is False
    assert not (folder / "cover.webp").exists()


# ---- artist covers say "THIS IS / <artist>" and keep the photo's colours --------------

def test_artist_layout_puts_the_tag_first(cover):
    assert cover._layouts("Usher", "THIS IS", True)[0] == ["THIS IS", "USHER"]
    assert cover._layouts("Purple Disco Machine", "THIS IS", True)[1] == ["THIS IS", "PURPLE", "DISCO MACHINE"]
    assert cover._layouts("Techno")[0] == ["TECHNO", "RADIO"]          # genres are unchanged


def photo(color, size=(500, 500)):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return Image.open(BytesIO(buf.getvalue()))


def test_artist_cover_keeps_the_photo_colours(cover, tmp_path):
    out = cover.render_artist_cover(photo((200, 30, 30)), "Usher", tmp_path / "a.jpg")
    corner = Image.open(out).convert("RGB").getpixel((10, 10))

    assert corner[0] > 150 and corner[1] < 90 and corner[2] < 90        # still red, not duotone


def test_genre_cover_is_tinted_in_the_genres_colour(cover, tmp_path):
    out = cover.render_genre_cover(BACKGROUND, "Trance", tmp_path / "g.jpg")
    r, g, b = Image.open(out).convert("RGB").resize((1, 1)).getpixel((0, 0))

    assert max(r, g, b) - min(r, g, b) > 20                             # a colour, not grey


def test_artist_cover_is_not_a_radio_cover(cover, tmp_path):
    artist = cover.render_artist_cover(photo((90, 90, 90)), "Usher", tmp_path / "a.jpg")
    radio = cover.render_genre_cover(photo((90, 90, 90)), "Usher", tmp_path / "r.jpg")

    assert digest(artist) != digest(radio)


def test_artist_cover_accepts_a_wide_photo(cover, tmp_path):
    out = cover.render_artist_cover(photo((10, 120, 200), (900, 300)), "Nora en Pure", tmp_path / "w.jpg")

    with Image.open(out) as image:
        assert image.size == (600, 600)


def test_the_artist_fallback_writes_this_is(plugin, tmp_path, monkeypatch):
    """_generate_custom_cover_art draws the artist renderer, not the genre one."""
    calls = []
    monkeypatch.setattr(plugin, "render_artist_cover", lambda src, name, dest, **kw: calls.append(name) or Path(dest).write_bytes(b"x"))
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (400, 400), (1, 2, 3)).save(buf, "JPEG")
    gen, _, folder = make_generator(plugin, tmp_path, monkeypatch)

    assert gen._generate_custom_cover_art(buf.getvalue(), "Usher", folder / "cover.webp")
    assert calls == ["Usher"]


class FakeDeezerResponse:
    def __init__(self, content=b"", payload=None):
        self.content, self._payload = content, payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def deezer_with(plugin, tmp_path, picture_bytes, overlay):
    config = plugin.Config()
    config.deezer_cover_art_enabled = True
    config.artist_cover_name_overlay = overlay
    client = plugin.DeezerClient(config, logging.getLogger("test"))

    def get(url, **kw):
        if "search/artist" in url:
            return FakeDeezerResponse(payload={"data": [{"name": "Usher", "picture_xl": "http://x/p.jpg"}]})
        return FakeDeezerResponse(content=picture_bytes)
    client.session.get = get
    folder = tmp_path / "pl"
    folder.mkdir()
    return client, folder


def jpeg_bytes(color=(180, 40, 40), size=(1000, 1000)):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def test_deezer_cover_gets_the_name_drawn_on_it(plugin, tmp_path):
    client, folder = deezer_with(plugin, tmp_path, jpeg_bytes(), overlay=True)
    (tmp_path / "plain").mkdir()
    plain, plain_folder = deezer_with(plugin, tmp_path / "plain", jpeg_bytes(), overlay=False)

    assert client.get_artist_cover_art("Usher", folder) is True
    assert plain.get_artist_cover_art("Usher", plain_folder) is True

    with Image.open(folder / "cover.jpg") as drawn, Image.open(plain_folder / "cover.jpg") as bare:
        assert drawn.size == (600, 600) and bare.size == (1000, 1000)
    assert digest(folder / "cover.jpg") != digest(plain_folder / "cover.jpg")


def test_a_failed_overlay_falls_back_to_the_plain_photo(plugin, tmp_path, monkeypatch):
    client, folder = deezer_with(plugin, tmp_path, jpeg_bytes(), overlay=True)
    monkeypatch.setattr(plugin, "render_artist_cover", lambda *a, **k: (_ for _ in ()).throw(OSError("no font")))

    assert client.get_artist_cover_art("Usher", folder) is True
    with Image.open(folder / "cover.jpg") as image:
        assert image.size == (1000, 1000)


def test_the_overlay_setting_defaults_on_and_is_read_from_settings(plugin, tmp_path, monkeypatch):
    import json
    assert plugin.Config().artist_cover_name_overlay is True
    settings = tmp_path / "s.json"
    settings.write_text(json.dumps({"artist_cover_name_overlay": False}))
    monkeypatch.setattr(plugin, "SETTINGS_FILE", str(settings))

    assert plugin.Config().artist_cover_name_overlay is False
