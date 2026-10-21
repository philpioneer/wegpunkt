"""GPX einlesen, Kennzahlen berechnen, Privatzonen, Höhenprofil und Routenbild."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gpxpy

Point = tuple[float, float, float | None]  # lat, lon, ele


@dataclass(frozen=True)
class Sport:
    name: str
    emoji: str
    hashtag: str
    kind: str          # "gps" oder "workout"
    metric: str | None  # "pace" (min/km) oder "speed" (km/h)
    color: tuple[int, int, int]


SPORTS: dict[str, Sport] = {
    "gravel": Sport("Gravel", "🚴", "Gravel", "gps", "speed", (196, 122, 44)),
    "enduro": Sport("Enduro MTB", "🚵", "EnduroMTB", "gps", "speed", (46, 139, 87)),
    "run": Sport("Joggen", "🏃", "Joggen", "gps", "pace", (37, 99, 235)),
    "calisthenics": Sport("Calisthenics", "🤸", "Calisthenics", "workout", None, (160, 63, 209)),
}
GPS_SPORTS = [k for k, v in SPORTS.items() if v.kind == "gps"]


def sport(key: str) -> Sport:
    return SPORTS.get(key, SPORTS["run"])


_GPX_TYPE_HINTS = {
    "run": "run", "running": "run", "laufen": "run", "joggen": "run", "trail_running": "run", "9": "run",
    "cycling": "gravel", "biking": "gravel", "ride": "gravel", "gravel": "gravel",
    "gravel_cycling": "gravel", "radfahren": "gravel", "1": "gravel",
    "mountain_biking": "enduro", "mountainbiking": "enduro", "mtb": "enduro", "enduro": "enduro",
    "mountainbike": "enduro",
}


class TrackError(ValueError):
    pass


@dataclass
class Track:
    name: str | None
    sport_hint: str | None
    started_at: datetime | None
    distance_m: float
    duration_s: float | None
    moving_s: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    points: list[Point]


def haversine_m(a: Point, b: Point) -> float:
    r = 6_371_000.0
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def downsample(points: list[Point], max_points: int) -> list[Point]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = [points[int(i * step)] for i in range(max_points - 1)]
    return picked + [points[-1]]


def parse_gpx(data: bytes) -> Track:
    try:
        gpx = gpxpy.parse(data.decode("utf-8-sig", errors="replace"))
    except Exception as exc:  # gpxpy wirft verschiedene Fehlertypen
        raise TrackError("Die Datei ist keine gültige GPX-Datei.") from exc

    raw = [p for t in gpx.tracks for s in t.segments for p in s.points]
    if not raw:
        raw = [p for r in gpx.routes for p in r.points]
    if len(raw) < 2:
        raise TrackError("Die GPX-Datei enthält keine Trackpunkte.")

    start, end = gpx.get_time_bounds()
    duration = (end - start).total_seconds() if start and end else None
    moving = gpx.get_moving_data() if start else None
    has_elevation = any(p.elevation is not None for p in raw)
    updown = gpx.get_uphill_downhill() if has_elevation else None

    track_type = (gpx.tracks[0].type or "").strip().lower() if gpx.tracks else ""
    name = (gpx.tracks[0].name if gpx.tracks else None) or gpx.name

    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    return Track(
        name=name.strip() if name else None,
        sport_hint=_GPX_TYPE_HINTS.get(track_type),
        started_at=start.astimezone(timezone.utc) if start else None,
        distance_m=gpx.length_2d(),
        duration_s=duration,
        moving_s=moving.moving_time if moving and moving.moving_time else None,
        elevation_gain_m=updown.uphill if updown else None,
        elevation_loss_m=updown.downhill if updown else None,
        points=[(p.latitude, p.longitude, p.elevation) for p in raw],
    )


def apply_privacy_zone(points: list[Point], radius_m: float) -> list[Point]:
    """Entfernt Punkte rund um Start und Ziel, damit z. B. die Wohnadresse verborgen bleibt."""
    if radius_m <= 0 or len(points) < 2:
        return points
    start, end = points[0], points[-1]
    i, j = 0, len(points)
    while i < j and haversine_m(points[i], start) < radius_m:
        i += 1
    while j > i and haversine_m(points[j - 1], end) < radius_m:
        j -= 1
    trimmed = points[i:j]
    return trimmed if len(trimmed) >= 2 else []


def track_profile(points: list[Point], max_points: int = 700) -> list[list[float | None]]:
    """[lat, lon, km, höhe] für Karte und Höhenprofil im Browser."""
    out: list[list[float | None]] = []
    dist = 0.0
    prev: Point | None = None
    for p in points:
        if prev is not None:
            dist += haversine_m(prev, p)
        prev = p
        ele = round(p[2], 1) if p[2] is not None else None
        out.append([round(p[0], 6), round(p[1], 6), round(dist / 1000, 3), ele])
    if len(out) > max_points:
        step = len(out) / max_points
        out = [out[int(i * step)] for i in range(max_points - 1)] + [out[-1]]
    return out


def _project(p: Point) -> tuple[float, float]:
    lat = math.radians(max(min(p[0], 85.0), -85.0))
    return p[1], math.degrees(math.log(math.tan(math.pi / 4 + lat / 2)))


def _fit(points: list[Point], w: float, h: float, pad: float) -> list[tuple[float, float]]:
    xy = [_project(p) for p in points]
    min_x, max_x = min(x for x, _ in xy), max(x for x, _ in xy)
    min_y, max_y = min(y for _, y in xy), max(y for _, y in xy)
    dx, dy = max(max_x - min_x, 1e-9), max(max_y - min_y, 1e-9)
    factor = min((w - 2 * pad) / dx, (h - 2 * pad) / dy)
    off_x = (w - dx * factor) / 2
    off_y = (h - dy * factor) / 2
    return [(off_x + (x - min_x) * factor, h - (off_y + (y - min_y) * factor)) for x, y in xy]


def route_path(points: list, w: float = 160, h: float = 110, pad: float = 10) -> str:
    """SVG-Pfad der Route für Vorschaubilder in der Liste."""
    pts = [tuple(p) for p in points]
    if len(pts) < 2:
        return ""
    pts = downsample(pts, 160)
    coords = _fit(pts, w, h, pad)
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)


def render_route_png(
    points: list[Point], path: Path, color: tuple[int, int, int] = (232, 84, 20),
    size: tuple[int, int] = (1200, 750),
) -> bool:
    """Zeichnet die Route als Bild, damit sie in Mastodon & Co. als Vorschau erscheint."""
    from PIL import Image, ImageDraw, ImageFilter

    if len(points) < 2:
        return False
    scale = 2  # doppelt so groß zeichnen und verkleinern, für glatte Linien
    w, h = size[0] * scale, size[1] * scale
    pixels = _fit(points, w, h, 90 * scale)

    bg = (21, 24, 29)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    grid = (32, 36, 43)
    step = 60 * scale
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=grid, width=scale)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=grid, width=scale)
    def stroke(target: ImageDraw.ImageDraw, width: int, fill) -> None:
        # Linie mit runden Ecken: Segmente plus Kreise an jedem Punkt
        target.line(pixels, fill=fill, width=width)
        half = width / 2
        for px, py in pixels:
            target.ellipse((px - half, py - half, px + half, py + half), fill=fill)

    glow_layer = Image.new("L", (w, h), 0)
    stroke(ImageDraw.Draw(glow_layer), 26 * scale, 255)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(14 * scale))
    img.paste(Image.new("RGB", (w, h), color), (0, 0), glow_layer.point(lambda v: int(v * 0.35)))
    stroke(draw, 9 * scale, color)
    r = 13 * scale
    for (px, py), fill in ((pixels[0], (255, 255, 255)), (pixels[-1], color)):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=fill, outline=bg, width=4 * scale)
    img.resize(size, Image.LANCZOS).save(path, "PNG", optimize=True)
    return True


# Anzeige ------------------------------------------------------------------

def _de(value: float, digits: int) -> str:
    text = f"{value:,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def distance_parts(m: float | None) -> tuple[str, str]:
    if m is None:
        return "–", ""
    if m < 1000:
        return f"{m:.0f}", "m"
    return _de(m / 1000, 2 if m < 100_000 else 1), "km"


def format_distance(m: float | None) -> str:
    value, unit = distance_parts(m)
    return f"{value} {unit}".strip()


def format_duration(s: float | None) -> str:
    if not s:
        return "–"
    s = int(round(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_hours(s: float | None) -> str:
    """Kurz für Summen: 3 h 25 min"""
    if not s:
        return "0 min"
    minutes = int(round(s / 60))
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def speed_parts(sport_key: str, distance_m: float, seconds: float | None) -> tuple[str, str]:
    if not seconds or distance_m < 10:
        return "–", ""
    if sport(sport_key).metric == "pace":
        pace = seconds / (distance_m / 1000)
        m, s = divmod(int(round(pace)), 60)
        return f"{m}:{s:02d}", "/km"
    return _de(distance_m / seconds * 3.6, 1), "km/h"


def format_speed(sport_key: str, distance_m: float, seconds: float | None) -> str:
    value, unit = speed_parts(sport_key, distance_m, seconds)
    return f"{value} {unit}".strip()


def format_meters(m: float | None) -> str:
    return "–" if m is None else _de(m, 0)
