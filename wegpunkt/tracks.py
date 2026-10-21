"""GPX einlesen, Kennzahlen berechnen, Privatzonen, Höhenprofil und Routenbild."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gpxpy

Point = tuple[float, float, float | None]  # lat, lon, ele

SPORTS: dict[str, tuple[str, str]] = {
    "run": ("Laufen", "🏃"),
    "ride": ("Radfahren", "🚴"),
    "hike": ("Wandern", "🥾"),
    "walk": ("Spazieren", "🚶"),
    "swim": ("Schwimmen", "🏊"),
    "other": ("Sonstiges", "💪"),
}

# Sportarten, bei denen ein Tempo (min/km) sinnvoller ist als km/h.
PACE_SPORTS = {"run", "hike", "walk"}

_GPX_TYPE_HINTS = {
    "run": "run", "running": "run", "laufen": "run", "9": "run",
    "ride": "ride", "cycling": "ride", "biking": "ride", "radfahren": "ride", "1": "ride",
    "hike": "hike", "hiking": "hike", "wandern": "hike", "4": "hike",
    "walk": "walk", "walking": "walk", "10": "walk",
    "swim": "swim", "swimming": "swim",
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
    gain = gpx.get_uphill_downhill().uphill if has_elevation else None

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
        elevation_gain_m=gain,
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


def elevation_profile(points: list[Point], width: int = 600, height: int = 140) -> dict | None:
    samples: list[tuple[float, float]] = []
    dist = 0.0
    prev: Point | None = None
    for p in points:
        if prev is not None:
            dist += haversine_m(prev, p)
        prev = p
        if p[2] is not None:
            samples.append((dist, p[2]))
    if len(samples) < 2:
        return None
    lo = min(e for _, e in samples)
    hi = max(e for _, e in samples)
    total = samples[-1][0] or 1.0
    span = (hi - lo) or 1.0
    step = max(1, len(samples) // 300)
    pts = samples[::step] + ([samples[-1]] if (len(samples) - 1) % step else [])
    coords = [
        (round(d / total * width, 1), round(height - (e - lo) / span * (height - 10) - 5, 1))
        for d, e in pts
    ]
    line = " ".join(f"{x},{y}" for x, y in coords)
    return {
        "width": width,
        "height": height,
        "line": line,
        "area": f"0,{height} {line} {width},{height}",
        "min": lo,
        "max": hi,
    }


def render_route_png(points: list[Point], path: Path, size: tuple[int, int] = (800, 500)) -> bool:
    """Zeichnet die Route als Bild, damit sie in Mastodon & Co. als Vorschau erscheint."""
    from PIL import Image, ImageDraw

    if len(points) < 2:
        return False
    scale = 2  # doppelt so groß zeichnen und verkleinern, für glatte Linien
    w, h = size[0] * scale, size[1] * scale
    pad = 40 * scale

    def project(p: Point) -> tuple[float, float]:
        lat = math.radians(max(min(p[0], 85.0), -85.0))
        return p[1], math.degrees(math.log(math.tan(math.pi / 4 + lat / 2)))

    xy = [project(p) for p in points]
    min_x, max_x = min(x for x, _ in xy), max(x for x, _ in xy)
    min_y, max_y = min(y for _, y in xy), max(y for _, y in xy)
    span = max(max_x - min_x, max_y - min_y, 1e-9)
    factor = min((w - 2 * pad) / max(max_x - min_x, span * 1e-3),
                 (h - 2 * pad) / max(max_y - min_y, span * 1e-3))
    off_x = (w - (max_x - min_x) * factor) / 2
    off_y = (h - (max_y - min_y) * factor) / 2
    pixels = [(off_x + (x - min_x) * factor, h - (off_y + (y - min_y) * factor)) for x, y in xy]

    img = Image.new("RGB", (w, h), (246, 244, 239))
    draw = ImageDraw.Draw(img)
    draw.line(pixels, fill=(232, 84, 20), width=7 * scale, joint="curve")
    r = 9 * scale
    for (px, py), color in ((pixels[0], (34, 139, 34)), (pixels[-1], (40, 40, 40))):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=color, outline="white", width=3 * scale)
    img.resize(size, Image.LANCZOS).save(path, "PNG", optimize=True)
    return True


# Anzeige ------------------------------------------------------------------

def format_distance(m: float | None) -> str:
    if m is None:
        return "–"
    if m < 1000:
        return f"{m:.0f} m"
    return f"{m / 1000:.2f} km".replace(".", ",")


def format_duration(s: float | None) -> str:
    if not s:
        return "–"
    s = int(round(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_speed(sport: str, distance_m: float, seconds: float | None) -> str:
    if not seconds or distance_m < 10:
        return "–"
    if sport in PACE_SPORTS:
        pace = seconds / (distance_m / 1000)
        m, s = divmod(int(round(pace)), 60)
        return f"{m}:{s:02d} /km"
    return f"{distance_m / seconds * 3.6:.1f} km/h".replace(".", ",")
