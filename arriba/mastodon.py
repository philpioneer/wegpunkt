"""Beiträge über die Mastodon-Schnittstelle auf einem bestehenden Account veröffentlichen."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__, fmt
from .sports import sport
from .workouts import format_exercise
from .workouts import summary as workout_summary

STATUS_LIMIT = 500      # Zeichen, die Mastodon standardmäßig erlaubt
LINK_LENGTH = 23        # so viel zählt Mastodon für jede Adresse
VISIBILITIES = {
    "public": "Öffentlich",
    "unlisted": "Öffentlich, aber nicht in den Timelines",
    "private": "Nur für Follower",
}


class MastodonError(Exception):
    pass


def normalize_url(value: str) -> str:
    """'mastodon.social' oder 'https://mastodon.social/@ich' → 'https://mastodon.social'"""
    value = value.strip()
    if not value:
        raise MastodonError("Bitte gib die Adresse deiner Mastodon-Instanz an.")
    if "//" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise MastodonError("Die Adresse der Instanz sieht nicht richtig aus, z. B. https://mastodon.social")
    return f"https://{parts.netloc}"


def stats_line(a: dict[str, Any]) -> list[str]:
    """Die Kennzahlen einer Aktivität als kurze Textbausteine."""
    sp = sport(a["sport"])
    seconds = a["moving_s"] or a["duration_s"]
    stats = [f"{sp.emoji} {sp.name}"]
    if a.get("kind") == "workout":
        totals = workout_summary(a.get("workout"))
        if seconds:
            stats.append(fmt.hours(seconds))
        stats.append(f"{totals['sets']} Sätze")
        if totals["reps"]:
            stats.append(f"{totals['reps']} Wdh.")
        return stats
    stats.append(fmt.distance(a["distance_m"]))
    if seconds:
        stats += [fmt.duration(seconds), fmt.speed(a["sport"], a["distance_m"], seconds)]
    if a["elevation_gain_m"]:
        stats.append(f"↑ {fmt.meters(a['elevation_gain_m'])} m")
    if a["sport"] == "enduro" and a.get("elevation_loss_m"):
        stats.append(f"↓ {fmt.meters(a['elevation_loss_m'])} m")
    if a.get("extra", {}).get("avg_hr"):
        stats.append(f"♥ {a['extra']['avg_hr']} bpm")
    return stats


def compose_status(a: dict[str, Any], link: str | None = None, limit: int = STATUS_LIMIT) -> str:
    """Text des Beitrags. Passt immer ins Zeichenlimit: gekürzt wird zuerst die Beschreibung."""
    head = f"{a['title']}\n\n{' · '.join(stats_line(a))}"
    tail = f"#{sport(a['sport']).hashtag}"
    if link:
        tail = f"{link}\n{tail}"
    used = len(head) + 2 + len(tail) - (len(link) - LINK_LENGTH if link else 0)

    blocks: list[str] = []
    if a.get("kind") == "workout" and a.get("workout"):
        lines: list[str] = []
        for i, e in enumerate(a["workout"]):
            line = format_exercise(e)
            rest = len(a["workout"]) - i - 1
            reserve = len(f"\n… und {rest} weitere") if rest else 0
            if used + 2 + sum(len(x) + 1 for x in lines) + len(line) + reserve > limit:
                lines.append(f"… und {rest + 1} weitere")
                break
            lines.append(line)
        block = "\n".join(lines)
        if used + 2 + len(block) <= limit:
            blocks.append(block)
            used += 2 + len(block)
    description = (a.get("description") or "").strip()
    room = limit - used - 2
    if description and room >= 20:
        if len(description) > room:
            description = description[: room - 1].rstrip() + "…"
        blocks.append(description)
    text = "\n\n".join([head, *blocks, tail])
    if len(text) - (len(link) - LINK_LENGTH if link else 0) > limit:  # sehr langer Titel
        text = text[: limit - 1] + "…"
    return text


class Mastodon:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=30, transport=transport,
            headers={"Authorization": f"Bearer {token}", "User-Agent": f"Arriba/{__version__}"},
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MastodonError(f"Mastodon ist nicht erreichbar ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise MastodonError("Mastodon lehnt das Zugangs-Token ab. Bitte unter „Profil“ neu verbinden.")
        if resp.status_code == 403:
            raise MastodonError("Dem Token fehlt eine Berechtigung. Nötig sind: read, write:statuses, write:media.")
        if resp.status_code == 404:
            raise MastodonError("Mastodon kennt diese Adresse oder diesen Beitrag nicht (404).")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", "")
            except ValueError:
                detail = ""
            raise MastodonError(f"Mastodon meldet einen Fehler ({resp.status_code}). {detail}".strip())
        try:
            return resp.json()
        except ValueError as exc:
            raise MastodonError("Unerwartete Antwort. Ist das wirklich eine Mastodon-Instanz?") from exc

    def verify(self) -> dict[str, Any]:
        """Prüft das Token und gibt den Account zurück."""
        account = self._request("GET", "/api/v1/accounts/verify_credentials")
        if not isinstance(account, dict) or "acct" not in account:
            raise MastodonError("Unerwartete Antwort. Ist das wirklich eine Mastodon-Instanz?")
        return account

    def upload_media(self, path: Path, description: str) -> str:
        with path.open("rb") as f:
            media = self._request(
                "POST", "/api/v2/media",
                files={"file": (path.name, f, "image/png")}, data={"description": description[:1500]},
            )
        media_id = str(media["id"])
        # Größere Dateien verarbeitet Mastodon im Hintergrund: warten, bis sie bereit sind.
        for _ in range(20):
            if media.get("url"):
                break
            time.sleep(1)
            media = self._request("GET", f"/api/v1/media/{media_id}")
        return media_id

    def post(self, text: str, media_ids: list[str], visibility: str, key: str) -> dict[str, Any]:
        payload = {"status": text, "media_ids": media_ids, "visibility": visibility, "language": "de"}
        # Der Schlüssel verhindert doppelte Beiträge, falls die Anfrage wiederholt wird.
        return self._request("POST", "/api/v1/statuses", json=payload, headers={"Idempotency-Key": key})

    def edit(self, status_id: str, text: str, media_ids: list[str]) -> dict[str, Any]:
        payload = {"status": text, "media_ids": media_ids, "language": "de"}
        return self._request("PUT", f"/api/v1/statuses/{status_id}", json=payload)

    def delete(self, status_id: str) -> None:
        self._request("DELETE", f"/api/v1/statuses/{status_id}")

    def status(self, status_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/statuses/{status_id}")
