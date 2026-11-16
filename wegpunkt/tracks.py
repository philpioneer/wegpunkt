"""GPS-Aufzeichnungen einlesen (GPX, FIT) und auswerten."""

from __future__ import annotations

import gzip
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .sports import guess_sport

MAX_UNPACKED_BYTES = 80 * 1024 * 1024
MOVING_SPEED_MS = 0.28          # ab ca. 1 km/h gilt man als in Bewegung
ELEVATION_THRESHOLD_M = 3.0     # kleinere Schwankungen zählen als Rauschen
EFFORT_DISTANCES = {"1 km": 1000.0, "5 km": 5000.0, "10 km": 10000.0,
                    "Halbmarathon": 21097.5, "Marathon": 42195.0}
ANALYSIS_VERSION = 2


class TrackError(ValueError):
    pass


@dataclass
class Sample:
    lat: float
    lon: float
    ele: float | None = None
    time: datetime | None = None
    hr: int | None = None


@dataclass
class Track:
    samples: list[Sample]
    name: str | None = None
    sport_hint: str | None = None
    source: str = "gpx"
    # Ausgewertet in analyze():
    started_at: datetime | None = None
    distance_m: float = 0.0
    duration_s: float | None = None
    moving_s: float | None = None
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Laufende Summen je Sample (Distanz in m, Zeit in s seit Start)
    cum_dist: list[float] = field(default_factory=list)
    cum_time: list[float | None] = field(default_factory=list)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# Einlesen ---------------------------------------------------------------------

def parse(data: bytes) -> Track:
    """Erkennt das Format am Inhalt: GPX oder FIT, jeweils auch gzip-gepackt."""
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                data = f.read(MAX_UNPACKED_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise TrackError("Die gepackte Datei lässt sich nicht öffnen.") from exc
        if len(data) > MAX_UNPACKED_BYTES:
            raise TrackError("Die Datei ist entpackt zu groß.")
    track = _parse_fit(data) if data[8:12] == b".FIT" else _parse_gpx(data)
    if len(track.samples) < 2:
        raise TrackError("Die Datei enthält keine GPS-Punkte.")
    analyze(track)
    return track


def _gpx_heart_rate(point: Any) -> int | None:
    for ext in point.extensions or []:
        for el in ext.iter():
            if el.tag.rsplit("}", 1)[-1].lower() in ("hr", "heartrate") and el.text:
                try:
                    return int(float(el.text))
                except ValueError:
                    return None
    return None


def _parse_gpx(data: bytes) -> Track:
    import gpxpy

    try:
        gpx = gpxpy.parse(data.decode("utf-8-sig", errors="replace"))
    except Exception as exc:  # gpxpy wirft verschiedene Fehlertypen
        raise TrackError("Die Datei ist weder eine gültige GPX- noch eine FIT-Datei.") from exc

    raw = [p for t in gpx.tracks for s in t.segments for p in s.points]
    if not raw:
        raw = [p for r in gpx.routes for p in r.points]
    first = gpx.tracks[0] if gpx.tracks else None
    name = (first.name if first else None) or gpx.name
    return Track(
        samples=[
            Sample(p.latitude, p.longitude, p.elevation, _utc(p.time), _gpx_heart_rate(p))
            for p in raw
        ],
        name=name.strip() if name else None,
        sport_hint=guess_sport(first.type if first else None),
        source="gpx",
    )


def _parse_fit(data: bytes) -> Track:
    import fitdecode

    semicircle = 180.0 / 2**31
    samples: list[Sample] = []
    sport_text = ""
    try:
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "record":
                    lat = frame.get_value("position_lat", fallback=None)
                    lon = frame.get_value("position_long", fallback=None)
                    if lat is None or lon is None:
                        continue
                    ele = frame.get_value("enhanced_altitude", fallback=None)
                    if ele is None:
                        ele = frame.get_value("altitude", fallback=None)
                    hr = frame.get_value("heart_rate", fallback=None)
                    samples.append(Sample(
                        lat * semicircle, lon * semicircle,
                        float(ele) if isinstance(ele, (int, float)) else None,
                        _utc(frame.get_value("timestamp", fallback=None)),
                        int(hr) if isinstance(hr, (int, float)) else None,
                    ))
                elif frame.name in ("session", "sport") and not sport_text.strip():
                    sport_text = " ".join(
                        str(frame.get_value(f, fallback="") or "") for f in ("sport", "sub_sport")
                    )
    except Exception as exc:  # fitdecode wirft verschiedene Fehlertypen
        raise TrackError("Die FIT-Datei ist beschädigt.") from exc
    return Track(samples=samples, sport_hint=guess_sport(sport_text), source="fit")


# Auswerten --------------------------------------------------------------------

def _smooth(values: list[float], window: int = 5) -> list[float]:
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - half): i + half + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _climb(elevations: list[float]) -> tuple[float, float]:
    """Auf- und Abstieg mit Schwelle, damit GPS-Rauschen nicht mitzählt."""
    if len(elevations) < 2:
        return 0.0, 0.0
    smooth = _smooth(elevations)
    ref = smooth[0]
    up = down = 0.0
    for e in smooth[1:]:
        if e - ref >= ELEVATION_THRESHOLD_M:
            up += e - ref
            ref = e
        elif ref - e >= ELEVATION_THRESHOLD_M:
            down += ref - e
            ref = e
    return up, down


def _interp(xs: list[float], ys: list[float], x: float, start: int = 0) -> tuple[float, int]:
    """Lineare Interpolation in aufsteigenden xs. Gibt (y, index) zurück."""
    i = start
    while i < len(xs) - 2 and xs[i + 1] < x:
        i += 1
    x0, x1 = xs[i], xs[i + 1]
    f = 0.0 if x1 <= x0 else min(max((x - x0) / (x1 - x0), 0.0), 1.0)
    return ys[i] + (ys[i + 1] - ys[i]) * f, i


def analyze(track: Track) -> None:
    s = track.samples
    cum_dist = [0.0]
    for a, b in zip(s, s[1:]):
        cum_dist.append(cum_dist[-1] + haversine_m(a.lat, a.lon, b.lat, b.lon))
    track.cum_dist = cum_dist
    track.distance_m = cum_dist[-1]

    timed = all(p.time is not None for p in s)
    t0 = s[0].time if timed else None
    track.started_at = t0 or next((p.time for p in s if p.time), None)
    extra: dict[str, Any] = {"version": ANALYSIS_VERSION, "source": track.source}

    if timed and t0 is not None:
        cum_time = [max(0.0, (p.time - t0).total_seconds()) for p in s]  # type: ignore[operator]
        for i in range(1, len(cum_time)):  # Zeitsprünge rückwärts glätten
            cum_time[i] = max(cum_time[i], cum_time[i - 1])
        track.cum_time = list(cum_time)
        track.duration_s = cum_time[-1]

        cum_moving = [0.0]
        max_speed = 0.0
        for i in range(1, len(s)):
            dt = cum_time[i] - cum_time[i - 1]
            dd = cum_dist[i] - cum_dist[i - 1]
            moving = dt > 0 and dd / dt >= MOVING_SPEED_MS and dt < 300
            cum_moving.append(cum_moving[-1] + (dt if moving else 0.0))
            if moving and dt >= 1:
                max_speed = max(max_speed, dd / dt)
        track.moving_s = cum_moving[-1] or None
        extra["splits"] = _splits(track, cum_moving)
        extra["efforts"] = _best_efforts(cum_dist, cum_time)
        if 0 < max_speed < 40:  # über 144 km/h ist ein GPS-Fehler
            extra["max_speed_ms"] = round(max_speed, 2)
    else:
        track.cum_time = [None] * len(s)

    elevations = [p.ele for p in s if p.ele is not None]
    if len(elevations) > 1:
        track.elevation_gain_m, track.elevation_loss_m = _climb(elevations)
        extra["max_ele"] = round(max(elevations), 1)
        extra["min_ele"] = round(min(elevations), 1)

    heart = [p.hr for p in s if p.hr]
    if len(heart) > 10:
        extra["avg_hr"] = round(sum(heart) / len(heart))
        extra["max_hr"] = max(heart)
    track.extra = extra


def _splits(track: Track, cum_moving: list[float]) -> list[dict[str, float]]:
    """Zeit je Kilometer (Bewegungszeit) samt Höhenunterschied."""
    total = track.cum_dist[-1]
    if total < 1000:
        return []
    eles = [p.ele for p in track.samples]
    has_ele = all(e is not None for e in eles)
    marks = [k * 1000.0 for k in range(1, int(total // 1000) + 1)]
    if total - marks[-1] >= 50:
        marks.append(total)
    out = []
    prev_t = prev_d = 0.0
    prev_e = eles[0] if has_ele else None
    idx = 0
    for d in marks:
        t, idx = _interp(track.cum_dist, cum_moving, d, idx)
        split: dict[str, float] = {"m": round(d - prev_d), "s": round(t - prev_t, 1)}
        if has_ele:
            e, _ = _interp(track.cum_dist, eles, d, idx)  # type: ignore[arg-type]
            split["ele"] = round(e - prev_e, 1)  # type: ignore[operator]
            prev_e = e
        out.append(split)
        prev_t, prev_d = t, d
    return out[:500]


def _best_efforts(cum_dist: list[float], cum_time: list[float]) -> dict[str, float]:
    """Schnellste Zeit über feste Distanzen irgendwo innerhalb der Aufzeichnung."""
    out: dict[str, float] = {}
    n = len(cum_dist)
    for label, target in EFFORT_DISTANCES.items():
        if cum_dist[-1] < target:
            continue
        best = math.inf
        j = 1
        for i in range(n):
            goal = cum_dist[i] + target
            if goal > cum_dist[-1]:
                break
            while cum_dist[j] < goal:
                j += 1
            # Zeitpunkt, an dem die Zieldistanz genau erreicht ist
            d0, d1 = cum_dist[j - 1], cum_dist[j]
            f = 1.0 if d1 <= d0 else (goal - d0) / (d1 - d0)
            t = cum_time[j - 1] + (cum_time[j] - cum_time[j - 1]) * f
            best = min(best, t - cum_time[i])
        if math.isfinite(best) and best > 0:
            out[label] = round(best, 1)
    return out


# Für die Anzeige --------------------------------------------------------------

def visible_points(track: Track, privacy_radius_m: float, max_points: int = 1500) -> list[list]:
    """Öffentlich sichtbarer Teil: [lat, lon, höhe, sekunden seit Start, puls].

    Punkte im Umkreis von Start und Ziel fallen weg, damit z. B. die Wohnadresse
    verborgen bleibt.
    """
    s = track.samples
    i, j = 0, len(s)
    if privacy_radius_m > 0:
        first, last = s[0], s[-1]
        while i < j and haversine_m(s[i].lat, s[i].lon, first.lat, first.lon) < privacy_radius_m:
            i += 1
        while j > i and haversine_m(s[j - 1].lat, s[j - 1].lon, last.lat, last.lon) < privacy_radius_m:
            j -= 1
    if j - i < 2:
        return []
    indexes = list(range(i, j))
    if len(indexes) > max_points:
        step = len(indexes) / max_points
        indexes = [indexes[int(k * step)] for k in range(max_points - 1)] + [indexes[-1]]
    out = []
    for k in indexes:
        p = s[k]
        t = track.cum_time[k] if track.cum_time else None
        out.append([
            round(p.lat, 6), round(p.lon, 6),
            round(p.ele, 1) if p.ele is not None else None,
            round(t) if t is not None else None,
            p.hr,
        ])
    return out


def profile(points: list[list]) -> list[list]:
    """Ergänzt die gespeicherten Punkte um die Strecke: [lat, lon, km, höhe, sek, puls]."""
    out = []
    dist = 0.0
    prev = None
    for p in points:
        if prev is not None:
            dist += haversine_m(prev[0], prev[1], p[0], p[1])
        prev = p
        out.append([p[0], p[1], round(dist / 1000, 3), p[2] if len(p) > 2 else None,
                    p[3] if len(p) > 3 else None, p[4] if len(p) > 4 else None])
    return out
