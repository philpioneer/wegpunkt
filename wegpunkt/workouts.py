"""Workouts ohne GPS (Calisthenics): Übungen mit Sätzen von Hand eintragen."""

from __future__ import annotations

import re
from typing import Any

SUGGESTIONS = [
    "Klimmzüge", "Chin-ups", "Australian Pull-ups", "Muscle-ups", "Liegestütze",
    "Diamond-Liegestütze", "Pike Push-ups", "Handstand-Liegestütze", "Dips", "Ring Dips",
    "Pistol Squats", "Kniebeugen", "Ausfallschritte", "Hanging Leg Raises", "Toes to Bar",
    "L-Sit", "Plank", "Hollow Body Hold", "Handstand", "Front Lever", "Back Lever",
    "Planche", "Human Flag", "Burpees",
]

UNITS = {"reps": "Wdh.", "sec": "Sek."}


class WorkoutError(ValueError):
    pass


def parse_sets(text: str) -> list[int]:
    """'8 8 7 6', '8,8,7' oder '4x8' → Liste von Wiederholungen bzw. Sekunden je Satz."""
    text = (text or "").strip().lower()
    if not text:
        return []
    m = re.fullmatch(r"(\d+)\s*[x×*]\s*(\d+)", text)
    if m:
        count, value = int(m.group(1)), int(m.group(2))
        if count > 50:
            raise WorkoutError(f"„{text}“: höchstens 50 Sätze pro Übung.")
        return [value] * count
    parts = [p for p in re.split(r"[\s,;/·+]+", text) if p]
    if not all(p.isdigit() for p in parts):
        raise WorkoutError(f"„{text}“ verstehe ich nicht. Beispiele: „8 8 7 6“ oder „4x8“.")
    if len(parts) > 50:
        raise WorkoutError(f"„{text}“: höchstens 50 Sätze pro Übung.")
    return [int(p) for p in parts]


def parse_weight(text: str) -> float | None:
    text = (text or "").strip().replace(",", ".").removesuffix("kg").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise WorkoutError(f"Zusatzgewicht „{text}“ ist keine Zahl.") from exc
    return value or None


def build_workout(
    names: list[str], sets: list[str], units: list[str], weights: list[str]
) -> list[dict[str, Any]]:
    exercises = []
    for i, name in enumerate(names):
        name = name.strip()[:80]
        set_text = sets[i] if i < len(sets) else ""
        if not name and not set_text.strip():
            continue  # leere Zeile
        if not name:
            raise WorkoutError("Bitte gib jeder Übung einen Namen.")
        values = parse_sets(set_text)
        if not values:
            raise WorkoutError(f"Bei „{name}“ fehlen die Sätze.")
        unit = units[i] if i < len(units) and units[i] in UNITS else "reps"
        exercises.append({
            "name": name,
            "sets": values,
            "unit": unit,
            "weight_kg": parse_weight(weights[i] if i < len(weights) else ""),
        })
    if not exercises:
        raise WorkoutError("Trag mindestens eine Übung ein.")
    return exercises


def summary(exercises: list[dict[str, Any]] | None) -> dict[str, int]:
    exercises = exercises or []
    return {
        "exercises": len(exercises),
        "sets": sum(len(e["sets"]) for e in exercises),
        "reps": sum(sum(e["sets"]) for e in exercises if e["unit"] == "reps"),
        "hold_s": sum(sum(e["sets"]) for e in exercises if e["unit"] == "sec"),
    }


def format_weight(kg: float | None) -> str:
    if not kg:
        return ""
    text = f"{kg:g}".replace(".", ",")
    return f"+{text} kg" if kg > 0 else f"{text} kg"


def format_exercise(e: dict[str, Any]) -> str:
    """Für Fediverse-Beiträge: 'Klimmzüge (+10 kg): 8 · 8 · 7 · 6 Wdh.'"""
    weight = format_weight(e.get("weight_kg"))
    label = f"{e['name']} ({weight})" if weight else e["name"]
    return f"{label}: {' · '.join(str(v) for v in e['sets'])} {UNITS[e['unit']]}"


def form_rows(exercises: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Ein gespeichertes Workout zurück in Formularzeilen, zum Bearbeiten oder als Vorlage."""
    return [
        {
            "name": e["name"],
            "sets": " ".join(str(v) for v in e["sets"]),
            "unit": e["unit"],
            "weight": f"{e['weight_kg']:g}".replace(".", ",") if e.get("weight_kg") else "",
        }
        for e in exercises or []
    ]


def exercise_key(name: str) -> str:
    return " ".join(name.lower().split())
