"""Cover images for the app's own playlists.

A cover is drawn from the playlist's **name and your description only** (no song, artist or
album data, and no album artwork). A colour theme is picked by plain word rules from your
description ("grunge" → dark red, "yacht" → sunset), or you can give your own image file,
which is cropped square, resized and compressed to fit Spotify's 256 KB limit.
"""

from __future__ import annotations

import base64
import hashlib
import io
import random
import re
from dataclasses import dataclass
from pathlib import Path

SIZE = 640
MAX_ENCODED = 256 * 1024  # Spotify's limit for the base64-encoded JPEG


@dataclass(frozen=True)
class Theme:
    name: str
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    accent: tuple[int, int, int]
    text: tuple[int, int, int]
    motif: str  # sun | rings | bars | grain


# name: top colour, bottom colour, accent, text, motif
THEMES: dict[str, Theme] = {
    "rock": Theme("rock", (24, 22, 24), (120, 18, 24), (214, 40, 40), (240, 234, 222), "bars"),
    "sunset": Theme(
        "sunset", (255, 170, 110), (40, 120, 140), (255, 236, 180), (255, 250, 240), "sun"
    ),
    "night": Theme("night", (16, 22, 52), (96, 84, 160), (200, 190, 255), (236, 236, 250), "rings"),
    "groove": Theme(
        "groove", (60, 18, 70), (230, 140, 40), (255, 210, 120), (255, 246, 230), "rings"
    ),
    "green": Theme(
        "green", (22, 56, 40), (170, 160, 90), (230, 220, 160), (246, 244, 232), "grain"
    ),
    "neon": Theme("neon", (10, 10, 30), (20, 160, 170), (255, 60, 170), (240, 255, 255), "bars"),
    "ink": Theme("ink", (30, 30, 34), (90, 92, 100), (220, 220, 220), (250, 250, 250), "grain"),
}

# Words in *your* description -> theme. First match wins, so the order matters.
THEME_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("sunset", ("yacht", "soft rock", "breezy", "summer", "beach", "golden hour", "surf")),
    ("rock", ("grunge", "rock", "heavy", "metal", "punk", "raw", "gritty", "riffs")),
    ("groove", ("groove", "funk", "soul", "jazz", "disco", "r&b")),
    ("night", ("chill", "calm", "sleep", "study", "late night", "dreamy", "ambient", "lo-fi")),
    ("green", ("folk", "country", "acoustic", "indie")),
    ("neon", ("edm", "electronic", "game", "gaming", "hype", "workout", "rap", "hip hop")),
]


def pick_theme(name: str, description: str = "") -> Theme:
    text = f" {name} {description} ".lower()
    for theme, words in THEME_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return THEMES[theme]
    pool = list(THEMES.values())
    return pool[int(hashlib.sha1(name.encode()).hexdigest(), 16) % len(pool)]


def _font(size: int):
    from PIL import ImageFont

    for f in (
        "arialbd.ttf",  # Windows
        "segoeuib.ttf",
        "Arial Bold.ttf",  # macOS
        "DejaVuSans-Bold.ttf",  # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def generate(name: str, description: str = "", theme: Theme | None = None):
    """A 640×640 cover: gradient, a simple motif, and the playlist name. Returns a PIL image."""
    from PIL import Image, ImageDraw, ImageFilter

    th = theme or pick_theme(name, description)
    rng = random.Random(name)  # same name -> same cover
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            px[x, y] = _lerp(th.top, th.bottom, (0.75 * y + 0.25 * x) / SIZE)

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ac = th.accent
    if th.motif == "sun":
        cx, cy, r = SIZE * 0.66, SIZE * 0.38, SIZE * 0.22
        sun = Image.new("L", (SIZE, SIZE), 0)
        sd = ImageDraw.Draw(sun)
        sd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=235)
        for i in range(5):  # retro stripes cut through the lower half of the sun
            yy = cy + r * (0.12 + 0.18 * i)
            sd.rectangle((0, yy, SIZE, yy + 3 + i * 3), fill=0)
        d.bitmap((0, 0), sun, fill=(*ac, 235))
    elif th.motif == "rings":
        cx, cy = SIZE * rng.uniform(0.55, 0.75), SIZE * rng.uniform(0.25, 0.4)
        for i in range(7):
            r = 40 + i * 34
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*ac, 150 - i * 18), width=5)
    elif th.motif == "bars":
        x = SIZE * 0.08
        while x < SIZE * 0.92:
            h = rng.uniform(0.12, 0.5) * SIZE
            d.rectangle((x, SIZE * 0.58 - h, x + 14, SIZE * 0.58), fill=(*ac, 200))
            x += 26
    layer = layer.filter(ImageFilter.GaussianBlur(0.6))
    img = Image.alpha_composite(img.convert("RGBA"), layer)

    grain = Image.effect_noise((SIZE, SIZE), 22).convert("L")
    img = Image.blend(img.convert("RGB"), Image.merge("RGB", (grain, grain, grain)), 0.06)

    d = ImageDraw.Draw(img)
    margin = 44
    size = 112
    while size > 40:
        font = _font(size)
        lines = _wrap(d, name, font, SIZE - 2 * margin)
        widest = max(d.textlength(line, font=font) for line in lines)
        if widest <= SIZE - 2 * margin and len(lines) * size * 1.08 <= SIZE * 0.42:
            break
        size -= 6
    y = SIZE - margin - len(lines) * size * 1.05
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    yy = y
    for line in lines:
        sdraw.text((margin + 2, yy + 4), line, font=font, fill=(0, 0, 0, 120))
        yy += size * 1.05
    img = Image.alpha_composite(
        img.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(6))
    ).convert("RGB")
    d = ImageDraw.Draw(img)
    for line in lines:
        d.text((margin, y), line, font=font, fill=th.text)
        y += size * 1.05
    d.rectangle((margin, SIZE - margin + 10, margin + 90, SIZE - margin + 16), fill=ac)
    return img


def from_file(path: str | Path):
    """Your own image, cropped to a centred square and resized to 640×640."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        return ImageOps.fit(im, (SIZE, SIZE), Image.Resampling.LANCZOS)


def to_jpeg(img) -> bytes:
    """JPEG bytes small enough for Spotify (≤ 256 KB once base64-encoded)."""
    for quality in (92, 85, 78, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(base64.b64encode(data)) <= MAX_ENCODED:
            return data
    raise ValueError("couldn't compress the image under Spotify's 256 KB limit")


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip().replace(" ", "_") or "playlist"
