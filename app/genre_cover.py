"""Genre cover art: a photo, a colour per genre, and the genre name on top.

Used in two places. tools/make_genre_covers.py draws the covers that ship in app/cover/
(drawn once, committed, never regenerated at runtime). The generator calls
render_genre_cover for a genre that has no shipped cover, for example a genre only your
own library has, and render_artist_cover for every artist playlist. Everything is drawn
with Pillow from files in this repo: no network, no model, no system fonts.

Genre covers: the name centred and large, a small spaced 'RADIO' under it, on the photo
in the genre's own colour. Artist covers: the photo as it is, with a small 'THIS IS' and
the name at the bottom left.
"""
import colorsys
import hashlib
from pathlib import Path
from typing import Iterable, List, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = Path(__file__).resolve().parent / 'fonts'
GENRE_FONT = FONT_DIR / 'Montserrat-ExtraBold.ttf'     # genre covers: wide, solid
ARTIST_FONT = FONT_DIR / 'Oswald-Bold.ttf'             # artist covers: tall and narrow
FONT_PATH = FONT_DIR / 'DejaVuSans-Bold.ttf'           # fallback for letters the two above lack
COVER_SIZE = 600
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif')


def _digest(name: str) -> int:
    return int(hashlib.md5(name.strip().lower().encode('utf-8')).hexdigest(), 16)


def genre_hue(name: str) -> float:
    """A stable hue (0..1) for a genre. Same name, same colour, on every machine."""
    return (_digest(name) % 360) / 360.0


def _hsv(h: float, s: float, v: float):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def find_cover(cover_dir: Path, bases: Iterable[str]) -> Optional[Path]:
    """The first file in cover_dir named like one of `bases`, ignoring case and extension.

    'Hip Hop Radio.jpg' is found for 'hip hop radio', which matters on Linux where file
    names are case sensitive and genre names come straight from the library's tags.
    """
    try:
        files = {f.name.lower(): f for f in Path(cover_dir).iterdir() if f.is_file()}
    except OSError:
        return None
    for base in bases:
        for ext in IMAGE_EXTENSIONS:
            hit = files.get(f"{base}{ext}".lower())
            if hit:
                return hit
    return None


def _background(source_image, name: str, size: int, vary: bool = True) -> Image.Image:
    """The photo (a path or a PIL image), cropped to a square.

    With vary on, zoom and offset come from the name, so genres sharing one photo still
    look different. With it off the photo is only centre-cropped, which is right for a
    picture of a person.
    """
    if isinstance(source_image, Image.Image):
        image = ImageOps.exif_transpose(source_image).convert('RGB')
    else:
        with Image.open(source_image) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
    if not vary:
        side = min(image.size)
        left, top = (image.width - side) // 2, (image.height - side) // 2
        return image.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)
    digest = _digest(name)
    side = min(image.size)
    zoom = 1.0 + ((digest >> 8) % 36) / 100.0              # 1.00 .. 1.35
    crop = int(side / zoom)
    max_dx, max_dy = image.width - crop, image.height - crop
    left = int(max_dx * (((digest >> 16) % 101) / 100.0))
    top = int(max_dy * (((digest >> 24) % 101) / 100.0))
    return image.crop((left, top, left + crop, top + crop)).resize((size, size), Image.Resampling.LANCZOS)


def _tint(image: Image.Image, hue: float) -> Image.Image:
    """Duotone the photo into the genre's colour: shadows dark, highlights light."""
    grey = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=2)
    return ImageOps.colorize(grey, black=_hsv(hue, 0.85, 0.10), mid=_hsv(hue, 0.70, 0.55),
                             white=_hsv(hue, 0.25, 1.00), midpoint=118).convert('RGB')


def _title_lines(title: str) -> List[List[str]]:
    """Ways to set a title on lines: all on one, or split between words.

    'DEEP HOUSE' is [['DEEP HOUSE'], ['DEEP', 'HOUSE']]; the caller picks the one that
    lets the type come out largest.
    """
    words = title.upper().split()
    splits = [[' '.join(words)]]
    for cut in range(1, len(words)):
        splits.append([' '.join(words[:cut]), ' '.join(words[cut:])])
    return splits


def _glyph(font: ImageFont.FreeTypeFont, char: str) -> bytes:
    canvas = Image.new('L', (80, 80), 0)
    ImageDraw.Draw(canvas).text((10, 10), char, font=font, fill=255)
    return canvas.tobytes()


def _face(preferred: Path, text: str) -> Path:
    """The preferred font file, or DejaVu Bold when the text has letters the preferred one lacks.

    A letter a font does not have is drawn as an empty box; it looks the same as every
    other missing letter, which is how it is found.
    """
    probe = ImageFont.truetype(str(preferred), 40)
    box = _glyph(probe, '\uffff')
    if any(not c.isspace() and _glyph(probe, c) == box for c in set(text)):
        return FONT_PATH
    return preferred


def _font(preferred: Path, size: int, text: str) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_face(preferred, text)), size)


def _fit(draw: ImageDraw.ImageDraw, lines: List[str], width: float, ceiling: int, preferred: Path):
    """Largest font size at which every line fits the width. Returns (font, size)."""
    face = _face(preferred, ''.join(lines))
    for size in range(ceiling, 17, -1):
        font = ImageFont.truetype(str(face), size)
        if max(draw.textlength(line, font=font) for line in lines) <= width:
            return font, size
    return ImageFont.truetype(str(face), 18), 18


def _tracked_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: float) -> float:
    return sum(draw.textlength(c, font=font) for c in text) + tracking * (len(text) - 1)


def _draw_tracked(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, tracking: float, fill) -> None:
    for c in text:
        draw.text((x, y), c, font=font, fill=fill)
        x += draw.textlength(c, font=font) + tracking


def render_genre_cover(background_path, genre_name: str, destination, size: int = COVER_SIZE) -> Path:
    """Genre cover: duotone photo, the name centred and large, a small spaced RADIO below it."""
    title = genre_name.strip()
    k = size / 600.0                                       # every measure below is for a 600 px cover
    image = _tint(_background(Path(background_path) if not isinstance(background_path, Image.Image)
                              else background_path, title, size, vary=True), genre_hue(title))

    # A soft dark band behind the text keeps it readable on any photo.
    shade = Image.new('RGBA', image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle((0, int(size * 0.22), size, int(size * 0.78)), fill=(0, 0, 0, 90))
    image = Image.alpha_composite(image.convert('RGBA'), shade.filter(ImageFilter.GaussianBlur(size * 0.06)))

    probe = ImageDraw.Draw(image)
    width = size * 0.84
    best = max(((_fit(probe, lines, width, int(120 * k), GENRE_FONT), lines) for lines in _title_lines(title)),
               key=lambda candidate: candidate[0][1])
    (font, font_size), lines = best

    tag = 'RADIO'
    tag_size = max(int(18 * k), int(font_size * 0.22))
    tag_font = _font(GENRE_FONT, tag_size, tag)
    tracking = tag_size * 0.45

    heights = [probe.textbbox((0, 0), line, font=font)[3] - probe.textbbox((0, 0), line, font=font)[1] for line in lines]
    line_gap = int(font_size * 0.12)
    tag_gap = int(font_size * 0.35)
    block = sum(heights) + line_gap * (len(lines) - 1) + tag_gap + tag_size
    y = (size - block) // 2

    shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
    text = Image.new('RGBA', image.size, (0, 0, 0, 0))
    shadow_draw, text_draw = ImageDraw.Draw(shadow), ImageDraw.Draw(text)
    for line, height in zip(lines, heights):
        box = probe.textbbox((0, 0), line, font=font)
        x = (size - (box[2] - box[0])) // 2 - box[0]
        top = y - box[1]
        shadow_draw.text((x, top + 4 * k), line, font=font, fill=(0, 0, 0, 200))
        text_draw.text((x, top), line, font=font, fill=(255, 255, 255, 255))
        y += height + line_gap
    y += tag_gap - line_gap
    tag_box = probe.textbbox((0, 0), tag, font=tag_font)
    x = (size - _tracked_width(probe, tag, tag_font, tracking)) / 2
    top = y - tag_box[1]
    _draw_tracked(shadow_draw, x, top + 3 * k, tag, tag_font, tracking, (0, 0, 0, 200))
    _draw_tracked(text_draw, x, top, tag, tag_font, tracking, (255, 255, 255, 235))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(5 * k)))
    return _save(Image.alpha_composite(image, text).convert('RGB'), destination)


def _bottom_fade(image: Image.Image, start: float = 0.40, strength: int = 215) -> Image.Image:
    """Darken the lower part of the photo, softly, so text there reads on any picture."""
    w, h = image.size
    fade = Image.new('L', (1, h), 0)
    for y in range(h):
        t = max(0.0, (y / h - start) / (1 - start))
        fade.putpixel((0, y), int(strength * t ** 1.4))
    black = Image.new('RGBA', image.size, (0, 0, 0, 255))
    black.putalpha(fade.resize((w, h)))
    return Image.alpha_composite(image.convert('RGBA'), black)


def render_artist_cover(photo, artist_name: str, destination, size: int = COVER_SIZE) -> Path:
    """Artist cover: the photo in its own colours, a small THIS IS and the name at the bottom left."""
    name = artist_name.strip()
    k = size / 600.0
    image = _bottom_fade(_background(photo if isinstance(photo, Image.Image) else Path(photo),
                                     name, size, vary=False))
    probe = ImageDraw.Draw(image)
    margin = int(size * 0.07)
    width = size - 2 * margin

    # One line when it comes out at a readable size, otherwise split between words.
    single, *splits = _title_lines(name)
    font, font_size = _fit(probe, single, width, int(84 * k), ARTIST_FONT)
    lines = single
    if font_size < 44 * k and splits:
        font, font_size, lines = max(((*_fit(probe, split, width, int(84 * k), ARTIST_FONT), split) for split in splits),
                                     key=lambda candidate: candidate[1])

    tag = 'THIS IS'
    tag_size = max(int(16 * k), int(font_size * 0.50))
    tag_font = _font(ARTIST_FONT, tag_size, tag)
    heights = [probe.textbbox((0, 0), line, font=font)[3] - probe.textbbox((0, 0), line, font=font)[1] for line in lines]
    line_gap = int(font_size * 0.16)
    tag_gap = int(font_size * 0.22)
    tag_box = probe.textbbox((0, 0), tag, font=tag_font)

    shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
    text = Image.new('RGBA', image.size, (0, 0, 0, 0))
    shadow_draw, text_draw = ImageDraw.Draw(shadow), ImageDraw.Draw(text)

    # From the bottom edge up: the name lines, then the tag, then a short bar above it.
    y = size - margin - sum(heights) - line_gap * (len(lines) - 1)
    for line, height in zip(lines, heights):
        box = probe.textbbox((0, 0), line, font=font)
        shadow_draw.text((margin - box[0], y - box[1] + 3 * k), line, font=font, fill=(0, 0, 0, 190))
        text_draw.text((margin - box[0], y - box[1]), line, font=font, fill=(255, 255, 255, 255))
        y += height + line_gap
    tag_top = size - margin - sum(heights) - line_gap * (len(lines) - 1) - tag_gap - (tag_box[3] - tag_box[1])
    shadow_draw.text((margin - tag_box[0], tag_top - tag_box[1] + 3 * k), tag, font=tag_font, fill=(0, 0, 0, 190))
    text_draw.text((margin - tag_box[0], tag_top - tag_box[1]), tag, font=tag_font, fill=(255, 255, 255, 235))
    bar_y = tag_top - int(tag_size * 0.9)
    text_draw.rectangle((margin, bar_y, margin + int(size * 0.10), bar_y + max(4, int(size / 120))),
                        fill=(255, 255, 255, 255))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(4 * k)))
    return _save(Image.alpha_composite(image, text).convert('RGB'), destination)


def _save(image: Image.Image, destination) -> Path:
    destination = Path(destination)
    if destination.suffix.lower() in ('.jpg', '.jpeg'):
        image.save(destination, 'JPEG', quality=88, optimize=True)
    else:
        image.save(destination)
    return destination
