"""Zahlen und Zeiten in deutscher Schreibweise."""

from __future__ import annotations

from .sports import sport


def de(value: float, digits: int = 0) -> str:
    text = f"{value:,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def distance_parts(m: float | None) -> tuple[str, str]:
    if m is None:
        return "–", ""
    if m < 1000:
        return f"{m:.0f}", "m"
    return de(m / 1000, 2 if m < 100_000 else 1), "km"


def distance(m: float | None) -> str:
    return " ".join(p for p in distance_parts(m) if p)


def duration(s: float | None) -> str:
    """1:02:03 oder 52:13"""
    if not s:
        return "–"
    s = int(round(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def hours(s: float | None) -> str:
    """Kurz für Summen: 3 h 25 min"""
    if not s:
        return "0 min"
    minutes = int(round(s / 60))
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def pace(seconds_per_km: float) -> str:
    m, s = divmod(int(round(seconds_per_km)), 60)
    return f"{m}:{s:02d}"


def speed_parts(sport_key: str, distance_m: float | None, seconds: float | None) -> tuple[str, str]:
    if not seconds or not distance_m or distance_m < 10:
        return "–", ""
    if sport(sport_key).metric == "pace":
        return pace(seconds / (distance_m / 1000)), "/km"
    return de(distance_m / seconds * 3.6, 1), "km/h"


def speed(sport_key: str, distance_m: float | None, seconds: float | None) -> str:
    return " ".join(p for p in speed_parts(sport_key, distance_m, seconds) if p)


def meters(m: float | None) -> str:
    return "–" if m is None else de(m, 0)
