"""Die Sportarten dieser Instanz."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sport:
    key: str
    name: str
    emoji: str
    hashtag: str
    kind: str            # "gps" oder "workout"
    metric: str | None   # "pace" (min/km) oder "speed" (km/h)
    color: tuple[int, int, int]


SPORTS: dict[str, Sport] = {
    s.key: s
    for s in (
        Sport("gravel", "Gravel", "🚴", "Gravel", "gps", "speed", (196, 122, 44)),
        Sport("enduro", "Enduro MTB", "🚵", "EnduroMTB", "gps", "speed", (46, 139, 87)),
        Sport("run", "Joggen", "🏃", "Joggen", "gps", "pace", (37, 99, 235)),
        Sport("calisthenics", "Calisthenics", "🤸", "Calisthenics", "workout", None, (160, 63, 209)),
    )
}
GPS_SPORTS = [k for k, v in SPORTS.items() if v.kind == "gps"]


def sport(key: str) -> Sport:
    return SPORTS.get(key, SPORTS["run"])


# Hinweise aus GPX-<type> und FIT-sport/sub_sport. Reihenfolge: spezifisch vor allgemein.
_HINTS = (
    (("mountain", "mtb", "enduro", "downhill"), "enduro"),
    (("gravel", "cycl", "bik", "ride", "radfahr"), "gravel"),
    (("run", "lauf", "jog", "trail"), "run"),
)
_STRAVA_CODES = {"1": "gravel", "9": "run"}


def guess_sport(*hints: str | None) -> str | None:
    """Errät die Sportart aus Texten wie 'mountain_biking' oder 'Running'."""
    for hint in hints:
        text = (hint or "").strip().lower()
        if not text or text == "generic":
            continue
        if text in _STRAVA_CODES:
            return _STRAVA_CODES[text]
        for needles, key in _HINTS:
            if any(n in text for n in needles):
                return key
    return None
