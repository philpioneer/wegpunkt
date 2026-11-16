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


def route_png(points: list, path: Path, color: Color, size: tuple[int, int] = (1200, 675)) -> bool:
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


TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256
MAX_TILES = 96


class TileSource:
    """Lädt Kartenkacheln von OpenStreetMap und merkt sie sich auf der Festplatte."""

    def __init__(self, cache_dir: Path, user_agent: str, timeout: float = 6.0):
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.timeout = timeout
        self._client = None

    def get(self, z: int, x: int, y: int) -> bytes:
        path = self.cache_dir / str(z) / str(x) / f"{y}.png"
        if path.exists():
            return path.read_bytes()
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, headers={"User-Agent": self.user_agent})
        resp = self._client.get(TILE_URL.format(z=z, x=x, y=y))
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return resp.content

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator: Position in Pixeln auf der Weltkarte der Zoomstufe."""
    n = TILE_SIZE * 2 ** zoom
    lat = math.radians(max(min(lat, 85.0), -85.0))
    return (lon + 180.0) / 360.0 * n, (1 - math.asinh(math.tan(lat)) / math.pi) / 2 * n


def route_map_png(
    points: list, path: Path, color: Color, tiles: TileSource,
    size: tuple[int, int] = (1200, 675), pad: int = 80,
) -> bool:
    """Route auf echtem Kartenhintergrund. Wirft eine Ausnahme, wenn Kacheln fehlen."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont

    if len(points) < 2:
        return False
    pts = _thin(points, 1500)
    w, h = size
    zoom = 16
    while zoom > 2:
        xy = [_world_px(p[0], p[1], zoom) for p in pts]
        span_x = max(x for x, _ in xy) - min(x for x, _ in xy)
        span_y = max(y for _, y in xy) - min(y for _, y in xy)
        if span_x <= w - 2 * pad and span_y <= h - 2 * pad:
            break
        zoom -= 1
    # Eine Stufe näher zeichnen und passend verkleinern: so füllt die Route das Bild.
    fill = min((w - 2 * pad) / max(span_x, 1), (h - 2 * pad) / max(span_y, 1))
    zoom += 1
    k = min(max(2 / fill, 1.0), 2.0)
    cw, ch = round(w * k), round(h * k)
    xy = [_world_px(p[0], p[1], zoom) for p in pts]
    left = (min(x for x, _ in xy) + max(x for x, _ in xy)) / 2 - cw / 2
    top = (min(y for _, y in xy) + max(y for _, y in xy)) / 2 - ch / 2

    count = 2 ** zoom
    x0, x1 = math.floor(left / TILE_SIZE), math.floor((left + cw - 1) / TILE_SIZE)
    y0, y1 = max(0, math.floor(top / TILE_SIZE)), min(count - 1, math.floor((top + ch - 1) / TILE_SIZE))
    if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_TILES:
        raise ValueError("zu viele Kacheln")
    img = Image.new("RGB", (cw, ch), (236, 233, 226))
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            tile = Image.open(io.BytesIO(tiles.get(zoom, tx % count, ty))).convert("RGB")
            img.paste(tile, (round(tx * TILE_SIZE - left), round(ty * TILE_SIZE - top)))
    img = img.resize(size, Image.LANCZOS)

    # Karte etwas zurücknehmen, damit die Route heraussticht
    img = ImageEnhance.Color(img).enhance(0.6)
    img = Image.blend(img, Image.new("RGB", size, (255, 255, 255)), 0.15)

    scale = 2
    layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    pixels = [((x - left) / k * scale, (y - top) / k * scale) for x, y in xy]

    def stroke(width: int, fill) -> None:
        draw.line(pixels, fill=fill, width=width)
        half = width / 2
        for px, py in pixels:
            draw.ellipse((px - half, py - half, px + half, py + half), fill=fill)

    stroke(15 * scale, (255, 255, 255, 235))
    stroke(8 * scale, (*color, 255))
    r = 12 * scale
    for (px, py), fill in ((pixels[0], (27, 30, 35, 255)), (pixels[-1], (*color, 255))):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=fill, outline=(255, 255, 255, 255), width=4 * scale)
    overlay = layer.resize(size, Image.LANCZOS)
    img.paste(overlay, (0, 0), overlay)

    # Pflichtangabe für OpenStreetMap-Karten
    draw = ImageDraw.Draw(img, "RGBA")
    font = ImageFont.load_default(size=18)
    text = "© OpenStreetMap"
    tw = draw.textlength(text, font=font)
    draw.rounded_rectangle((w - tw - 26, h - 38, w - 10, h - 10), radius=8, fill=(255, 255, 255, 215))
    draw.text((w - tw - 18, h - 34), text, fill=(60, 64, 72, 255), font=font)
    img.save(path, "PNG", optimize=True)
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
