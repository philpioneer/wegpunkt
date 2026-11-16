"""Bilder: Routenvorschau für Fediverse-Beiträge, Vorschaupfade, Avatar."""

from __future__ import annotations

import io
import math
from pathlib import Path

Color = tuple[int, int, int]
LOGO_COLORS: list[Color] = [(227, 154, 82), (63, 174, 111), (75, 124, 240), (176, 100, 227)]


def _project(lat: float, lon: float) -> tuple[float, float]:
    lat = math.radians(max(min(lat, 85.0), -85.0))
    return lon, math.degrees(math.log(math.tan(math.pi / 4 + lat / 2)))


def _fit(points: list, w: float, h: float, pad: float) -> list[tuple[float, float]]:
    xy = [_project(p[0], p[1]) for p in points]
    min_x, max_x = min(x for x, _ in xy), max(x for x, _ in xy)
    min_y, max_y = min(y for _, y in xy), max(y for _, y in xy)
    dx, dy = max(max_x - min_x, 1e-9), max(max_y - min_y, 1e-9)
    factor = min((w - 2 * pad) / dx, (h - 2 * pad) / dy)
    off_x = (w - dx * factor) / 2
    off_y = (h - dy * factor) / 2
    return [(off_x + (x - min_x) * factor, h - (off_y + (y - min_y) * factor)) for x, y in xy]


def _thin(points: list, max_points: int) -> list:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points - 1)] + [points[-1]]


def route_path(points: list, w: float = 160, h: float = 110, pad: float = 12) -> str:
    """SVG-Pfad der Route für die Vorschau in Listen."""
    if len(points) < 2:
        return ""
    coords = _fit(_thin(points, 160), w, h, pad)
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)


def route_png(points: list, path: Path, color: Color, size: tuple[int, int] = (1200, 750)) -> bool:
    """Zeichnet die Route als Bild, damit sie in Mastodon & Co. als Vorschau erscheint."""
    from PIL import Image, ImageDraw, ImageFilter

    if len(points) < 2:
        return False
    scale = 2  # doppelt so groß zeichnen und verkleinern, für glatte Linien
    w, h = size[0] * scale, size[1] * scale
    pixels = _fit(_thin(points, 1500), w, h, 90 * scale)

    bg = (21, 24, 29)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    step = 60 * scale
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=(32, 36, 43), width=scale)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=(32, 36, 43), width=scale)

    def stroke(target: ImageDraw.ImageDraw, width: int, fill) -> None:
        # Linie mit runden Ecken: Segmente plus Kreise an jedem Punkt
        target.line(pixels, fill=fill, width=width)
        half = width / 2
        for px, py in pixels:
            target.ellipse((px - half, py - half, px + half, py + half), fill=fill)

    glow = Image.new("L", (w, h), 0)
    stroke(ImageDraw.Draw(glow), 26 * scale, 255)
    glow = glow.filter(ImageFilter.GaussianBlur(14 * scale))
    img.paste(Image.new("RGB", (w, h), color), (0, 0), glow.point(lambda v: int(v * 0.35)))
    stroke(draw, 9 * scale, color)
    r = 13 * scale
    for (px, py), fill in ((pixels[0], (255, 255, 255)), (pixels[-1], color)):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=fill, outline=bg, width=4 * scale)
    img.resize(size, Image.LANCZOS).save(path, "PNG", optimize=True)
    return True


def default_avatar(path: Path, size: int = 400) -> None:
    """Das Wegpunkt-Zeichen als Profilbild, solange kein eigenes hochgeladen ist."""
    from PIL import Image, ImageDraw

    scale = 3
    n = size * scale
    # Der Verlauf ist weich, also klein berechnen und hochskalieren.
    small = 48
    gradient = Image.new("RGB", (small, small))
    px = gradient.load()
    stops = LOGO_COLORS
    for y in range(small):
        for x in range(small):
            t = (x + y) / (2 * small) * (len(stops) - 1)
            i = min(int(t), len(stops) - 2)
            f = t - i
            px[x, y] = tuple(round(stops[i][c] + (stops[i + 1][c] - stops[i][c]) * f) for c in range(3))
    gradient = gradient.resize((n, n), Image.BICUBIC)
    c = n / 2
    img = Image.new("RGB", (n, n), (21, 23, 27))
    mask = Image.new("L", (n, n), 0)
    outer = n * 0.36
    ImageDraw.Draw(mask).polygon([(c, c - outer), (c + outer, c), (c, c + outer), (c - outer, c)], fill=255)
    img.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    inner = n * 0.17
    draw.line([(c, c - inner), (c + inner, c), (c, c + inner), (c - inner, c), (c, c - inner)],
              fill="white", width=round(n * 0.028), joint="curve")
    dot = n * 0.055
    draw.ellipse((c - dot, c - dot, c + dot, c + dot), fill="white")
    img.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)


class AvatarError(ValueError):
    pass


def save_avatar(data: bytes, path: Path, size: int = 400) -> None:
    """Hochgeladenes Bild quadratisch zuschneiden und ohne Metadaten als PNG speichern."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AvatarError("Das Bild lässt sich nicht lesen. Bitte PNG, JPEG oder WebP verwenden.") from exc
    ImageOps.fit(img, (size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
