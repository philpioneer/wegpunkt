"""Auswertungen über alle Aktivitäten: Woche, Diagramm, Kalender, Bestwerte, Fortschritt."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from . import fmt
from .sports import SPORTS
from .workouts import UNITS, exercise_key
from .workouts import summary as workout_summary

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def when(a: dict[str, Any], tz: tzinfo) -> datetime:
    value = a.get("started_at") or a["created_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)


def seconds(a: dict[str, Any]) -> float:
    return a.get("moving_s") or a.get("duration_s") or 0.0


def week_start(dt: datetime) -> datetime:
    day = dt - timedelta(days=dt.weekday())
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "distance_m": sum(a["distance_m"] or 0 for a in items),
        "seconds": sum(seconds(a) for a in items),
        "elevation_m": sum(a.get("elevation_gain_m") or 0 for a in items),
        "reps": sum(workout_summary(a.get("workout"))["reps"] for a in items),
    }


def this_week(activities: list[dict[str, Any]], tz: tzinfo, now: datetime | None = None) -> dict[str, Any]:
    """Summen der laufenden Woche und, zum Vergleich, der Vorwoche."""
    start = week_start((now or datetime.now(tz)).astimezone(tz))
    previous = start - timedelta(days=7)
    current = _totals([a for a in activities if when(a, tz) >= start])
    before = _totals([a for a in activities if previous <= when(a, tz) < start])
    current["previous_seconds"] = before["seconds"]
    return current


def weekly_chart(
    activities: list[dict[str, Any]], tz: tzinfo, weeks: int = 12,
    now: datetime | None = None, width: int = 720, height: int = 180,
) -> dict[str, Any]:
    """Gestapelte Balken: Trainingszeit pro Woche, aufgeteilt nach Sportart."""
    current = week_start((now or datetime.now(tz)).astimezone(tz))
    first = (current - timedelta(weeks=weeks - 1)).date()
    totals = [{k: 0.0 for k in SPORTS} for _ in range(weeks)]
    for a in activities:
        idx = (week_start(when(a, tz)).date() - first).days // 7
        if 0 <= idx < weeks and a["sport"] in SPORTS:
            totals[idx][a["sport"]] += seconds(a) / 3600

    top = max((sum(t.values()) for t in totals), default=0)
    scale_max = max(1, int(top) + (0 if top == int(top) else 1))  # auf volle Stunden
    plot_h = height - 24
    left = 34  # Platz für die Stunden-Beschriftung
    slot = (width - left) / weeks
    bar_w = slot * 0.56
    bars = []
    for i, t in enumerate(totals):
        start = first + timedelta(weeks=i)
        y = float(plot_h)
        segments = []
        for key in SPORTS:
            if t[key] <= 0:
                continue
            h = t[key] / scale_max * (plot_h - 8)
            y -= h
            segments.append({"sport": key, "y": round(y, 1), "h": round(max(h, 1.5), 1)})
        hours = sum(t.values())
        week_no = start.isocalendar().week
        parts = [f"{SPORTS[k].name} {fmt.hours(v * 3600)}" for k, v in t.items() if v > 0]
        bars.append({
            "x": round(left + i * slot + (slot - bar_w) / 2, 1),
            "w": round(bar_w, 1),
            "segments": segments,
            "label": f"KW {week_no}",
            "tooltip": f"KW {week_no}: {fmt.hours(hours * 3600)}" + (f" ({', '.join(parts)})" if parts else ""),
            "current": i == weeks - 1,
        })
    gridlines = [
        {"y": round(plot_h - v / scale_max * (plot_h - 8), 1), "label": f"{v} h"}
        for v in sorted({scale_max, round(scale_max / 2)} - {0})
    ]
    return {"width": width, "height": height, "plot_h": plot_h, "left": left, "bars": bars,
            "gridlines": gridlines, "empty": top == 0}


def calendar(
    activities: list[dict[str, Any]], tz: tzinfo, weeks: int = 53, now: datetime | None = None,
) -> dict[str, Any]:
    """Jahreskalender: ein Feld pro Tag, eingefärbt nach Sportart, kräftiger bei mehr Zeit."""
    today = (now or datetime.now(tz)).astimezone(tz).date()
    first = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    per_day: dict[date, Counter] = {}
    for a in activities:
        d = when(a, tz).date()
        if first <= d <= today and a["sport"] in SPORTS:
            # Mindestens 10 Minuten zählen, damit auch Workouts ohne Dauer sichtbar sind
            per_day.setdefault(d, Counter())[a["sport"]] += max(seconds(a), 600)

    cell, gap = 12, 3
    step = cell + gap
    cells, months = [], []
    last_month = None
    for w in range(weeks):
        monday = first + timedelta(weeks=w)
        if monday.month != last_month:
            if w < weeks - 2:
                months.append({"x": w * step, "label": MONTHS[monday.month - 1]})
            last_month = monday.month
        for wd in range(7):
            d = monday + timedelta(days=wd)
            if d > today:
                continue
            sports = per_day.get(d)
            item: dict[str, Any] = {"x": w * step, "y": wd * step, "sport": None, "level": 0,
                                    "title": f"{d.day}. {MONTHS[d.month - 1]}: keine Aktivität"}
            if sports:
                total = sum(sports.values())
                item["sport"] = sports.most_common(1)[0][0]
                item["level"] = 1 if total < 45 * 60 else 2 if total < 90 * 60 else 3 if total < 180 * 60 else 4
                names = ", ".join(SPORTS[k].name for k in sports)
                item["title"] = f"{d.day}. {MONTHS[d.month - 1]}: {names} · {fmt.hours(total)}"
            cells.append(item)
    return {
        "cells": cells, "months": months, "cell": cell,
        "width": weeks * step - gap, "height": 7 * step - gap,
        "active_days": len(per_day),
    }


def records(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bestwerte, gruppiert nach Sportart."""
    out: list[dict[str, Any]] = []

    def add(sport: str, label: str, value: str, unit: str, a: dict[str, Any]) -> None:
        out.append({"sport": sport, "label": label, "value": value, "unit": unit,
                    "id": a["id"], "title": a["title"]})

    def best(items: list[dict[str, Any]], key) -> dict[str, Any] | None:
        scored = [(key(a), a) for a in items if key(a)]
        return max(scored, key=lambda t: t[0])[1] if scored else None

    by_sport = {k: [a for a in activities if a["sport"] == k] for k in SPORTS}

    if a := best(by_sport["gravel"], lambda a: a["distance_m"]):
        add("gravel", "Längste Tour", *fmt.distance_parts(a["distance_m"]), a)
    if a := best(by_sport["gravel"], lambda a: a.get("elevation_gain_m")):
        add("gravel", "Höhenmeter", fmt.meters(a["elevation_gain_m"]), "m", a)
    if a := best(by_sport["enduro"], lambda a: a.get("elevation_loss_m")):
        add("enduro", "Längste Abfahrt", fmt.meters(a["elevation_loss_m"]), "m", a)
    if a := best(by_sport["enduro"], lambda a: a.get("elevation_gain_m")):
        add("enduro", "Höhenmeter", fmt.meters(a["elevation_gain_m"]), "m", a)
    for label in ("5 km", "10 km", "Halbmarathon"):
        timed = [a for a in by_sport["run"] if a["extra"].get("efforts", {}).get(label)]
        if timed:
            a = min(timed, key=lambda a: a["extra"]["efforts"][label])
            add("run", label, fmt.duration(a["extra"]["efforts"][label]), "", a)
    if a := best(by_sport["run"], lambda a: a["distance_m"]):
        add("run", "Längster Lauf", *fmt.distance_parts(a["distance_m"]), a)

    # Calisthenics: bester Einzelsatz der am häufigsten trainierten Übungen
    frequency: Counter = Counter()
    top: dict[str, tuple[int, dict[str, Any], dict[str, Any]]] = {}
    for a in by_sport["calisthenics"]:
        for e in a.get("workout") or []:
            key = exercise_key(e["name"]) + "|" + e["unit"]
            frequency[key] += 1
            value = max(e["sets"])
            if key not in top or value > top[key][0]:
                top[key] = (value, e, a)
    for key, _ in frequency.most_common(3):
        value, e, a = top[key]
        add("calisthenics", e["name"], str(value), UNITS[e["unit"]], a)
    return [
        {"sport": key, "items": [r for r in out if r["sport"] == key]}
        for key in SPORTS if any(r["sport"] == key for r in out)
    ]


def exercise_history(
    activities: list[dict[str, Any]], current: dict[str, Any], tz: tzinfo, limit: int = 8,
) -> dict[str, dict[str, Any]]:
    """Für jede Übung des aktuellen Workouts: frühere Einheiten, Trend und Bestwert."""
    now = when(current, tz)
    earlier = sorted(
        (a for a in activities
         if a["sport"] == "calisthenics" and a["id"] != current["id"] and when(a, tz) < now),
        key=lambda a: when(a, tz),
    )
    out: dict[str, dict[str, Any]] = {}
    for e in current.get("workout") or []:
        key = exercise_key(e["name"])
        past = [
            (a, p) for a in earlier for p in a.get("workout") or []
            if exercise_key(p["name"]) == key and p["unit"] == e["unit"]
        ]
        totals = [sum(p["sets"]) for _, p in past][-(limit - 1):] + [sum(e["sets"])]
        best_before = max((max(p["sets"]) for _, p in past), default=None)
        info: dict[str, Any] = {
            "sessions": len(past) + 1,
            "totals": totals,
            "spark": _sparkline(totals),
            "record": best_before is not None and max(e["sets"]) > best_before,
            "first": not past,
            "last": None,
        }
        if past:
            a, p = past[-1]
            d = when(a, tz)
            info["last"] = {
                "sets": " · ".join(str(v) for v in p["sets"]),
                "date": f"{d.day}. {MONTHS[d.month - 1]}",
                "delta": sum(e["sets"]) - sum(p["sets"]),
                "id": a["id"],
            }
        out[e["name"]] = info
    return out


def _sparkline(values: list[int], w: int = 96, h: int = 28) -> dict[str, Any] | None:
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = [
        (round(i / (len(values) - 1) * (w - 6) + 3, 1), round(h - 4 - (v - lo) / span * (h - 8), 1))
        for i, v in enumerate(values)
    ]
    return {"w": w, "h": h, "points": " ".join(f"{x},{y}" for x, y in pts),
            "last": pts[-1]}
