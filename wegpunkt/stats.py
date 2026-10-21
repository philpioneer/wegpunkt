"""Wochenübersicht und Diagramm der letzten Wochen."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from .tracks import SPORTS
from .workouts import summary as workout_summary


def _when(a: dict[str, Any], tz: tzinfo) -> datetime:
    value = a.get("started_at") or a["created_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)


def seconds(a: dict[str, Any]) -> float:
    return a.get("moving_s") or a.get("duration_s") or 0.0


def week_start(dt: datetime) -> datetime:
    day = dt - timedelta(days=dt.weekday())
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def this_week(activities: list[dict[str, Any]], tz: tzinfo, now: datetime | None = None) -> dict[str, Any]:
    start = week_start((now or datetime.now(tz)).astimezone(tz))
    week = [a for a in activities if _when(a, tz) >= start]
    return {
        "count": len(week),
        "distance_m": sum(a["distance_m"] or 0 for a in week),
        "seconds": sum(seconds(a) for a in week),
        "elevation_m": sum(a.get("elevation_gain_m") or 0 for a in week),
        "reps": sum(workout_summary(a.get("workout")).get("reps", 0) for a in week),
    }


def weekly_chart(
    activities: list[dict[str, Any]], tz: tzinfo, weeks: int = 12,
    now: datetime | None = None, width: int = 720, height: int = 180,
) -> dict[str, Any]:
    """Gestapelte Balken: Trainingszeit pro Woche, aufgeteilt nach Sportart."""
    current = week_start((now or datetime.now(tz)).astimezone(tz))
    starts = [current - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    totals = [{k: 0.0 for k in SPORTS} for _ in starts]
    for a in activities:
        idx = (week_start(_when(a, tz)) - starts[0]).days // 7
        if 0 <= idx < weeks and a["sport"] in SPORTS:
            totals[idx][a["sport"]] += seconds(a) / 3600

    top = max((sum(t.values()) for t in totals), default=0)
    scale_max = max(1, int(top) + (0 if top == int(top) else 1))  # auf volle Stunden
    plot_h = height - 24
    left = 34  # Platz für die Stunden-Beschriftung
    slot = (width - left) / weeks
    bar_w = slot * 0.56
    bars = []
    for i, (start, t) in enumerate(zip(starts, totals)):
        y = plot_h
        segments = []
        for key in SPORTS:
            if t[key] <= 0:
                continue
            h = t[key] / scale_max * (plot_h - 8)
            y -= h
            segments.append({"sport": key, "y": round(y, 1), "h": round(max(h, 1.5), 1)})
        hours = sum(t.values())
        bars.append({
            "x": round(left + i * slot + (slot - bar_w) / 2, 1),
            "w": round(bar_w, 1),
            "segments": segments,
            "label": f"KW {start.isocalendar().week}",
            "tooltip": f"KW {start.isocalendar().week}: {hours:.1f} h".replace(".", ","),
            "current": i == weeks - 1,
        })
    gridlines = [
        {"y": round(plot_h - v / scale_max * (plot_h - 8), 1), "label": f"{v} h"}
        for v in sorted({scale_max, round(scale_max / 2)} - {0})
    ]
    return {"width": width, "height": height, "plot_h": plot_h, "left": left, "bars": bars,
            "gridlines": gridlines, "empty": top == 0}
