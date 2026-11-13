"""Genre cover art: a photo, a colour per genre, and the genre name on top.

Used in two places. tools/make_genre_covers.py draws the covers that ship in app/cover/
(drawn once, committed, never regenerated at runtime). The generator calls
render_genre_cover for a genre that has no shipped cover, for example a genre only your
own library has. Everything is drawn with Pillow from files in this repo: no network, no
model, no system fonts.
"""
import colorsys
import hashlib
from pathlib import Path
from typing import Iterable, List, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_PATH = Path(__file__).resolve().parent / 'fonts' / 'DejaVuSans-Bold.ttf'
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


def _background(path: Path, name: str, size: int) -> Image.Image:
    """The photo, cropped to a square at a zoom and offset taken from the genre name."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
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


def _layouts(genre: str) -> List[List[str]]:
    """Ways to set 'GENRE / RADIO' on lines: one line for the genre, or two when it has words."""
    words = genre.upper().split()
    layouts = [[' '.join(words), 'RADIO']]
    if len(words) > 1:
        for cut in range(1, len(words)):
            layouts.append([' '.join(words[:cut]), ' '.join(words[cut:]), 'RADIO'])
    return layouts


def _fit(draw: ImageDraw.ImageDraw, lines: List[str], width: int, ceiling: int = 150):
    """Largest font size at which every line fits the width. Returns (font, size)."""
    for size in range(ceiling, 23, -2):
        font = ImageFont.truetype(str(FONT_PATH), size)
        if max(draw.textlength(line, font=font) for line in lines) <= width:
            return font, size
    return ImageFont.truetype(str(FONT_PATH), 24), 24


def render_genre_cover(background_path, genre_name: str, destination, size: int = COVER_SIZE) -> Path:
    """Draw the cover for one genre and save it at `destination` (format from the extension)."""
    genre_name = genre_name.strip()
    image = _tint(_background(Path(background_path), genre_name, size), genre_hue(genre_name))

    # A soft dark band behind the text keeps it readable on any photo.
    shade = Image.new('RGBA', image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle((0, int(size * 0.22), size, int(size * 0.78)), fill=(0, 0, 0, 90))
    image = Image.alpha_composite(image.convert('RGBA'), shade.filter(ImageFilter.GaussianBlur(size * 0.06)))

    probe = ImageDraw.Draw(image)
    width = int(size * 0.84)
    best = max(((_fit(probe, lines, width), lines) for lines in _layouts(genre_name)),
               key=lambda candidate: candidate[0][1])
    (font, font_size), lines = best

    spacing = int(font_size * 0.18)
    heights = [probe.textbbox((0, 0), line, font=font)[3] - probe.textbbox((0, 0), line, font=font)[1] for line in lines]
    block = sum(heights) + spacing * (len(lines) - 1)
    y = (size - block) // 2

    shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
    text = Image.new('RGBA', image.size, (0, 0, 0, 0))
    shadow_draw, text_draw = ImageDraw.Draw(shadow), ImageDraw.Draw(text)
    for line, height in zip(lines, heights):
        box = probe.textbbox((0, 0), line, font=font)
        x = (size - (box[2] - box[0])) // 2 - box[0]
        top = y - box[1]
        shadow_draw.text((x, top + 4), line, font=font, fill=(0, 0, 0, 200))
        text_draw.text((x, top), line, font=font, fill=(255, 255, 255, 255))
        y += height + spacing
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(5)))
    image = Image.alpha_composite(image, text).convert('RGB')

    destination = Path(destination)
    if destination.suffix.lower() in ('.jpg', '.jpeg'):
        image.save(destination, 'JPEG', quality=88, optimize=True)
    else:
        image.save(destination)
    return destination
