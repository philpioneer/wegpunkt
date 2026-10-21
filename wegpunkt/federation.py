"""ActivityPub: eigene Objekte bauen, fremde Accounts laden, zustellen, Inbox verarbeiten."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import uuid
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from . import __version__
from .config import Settings
from .db import Database
from .httpsig import public_key_pem, sign_request
from .tracks import format_distance, format_duration, format_hours, format_meters, format_speed, sport
from .workouts import format_exercise
from .workouts import summary as workout_summary

log = logging.getLogger("wegpunkt.federation")

AS_CONTEXT = "https://www.w3.org/ns/activitystreams"
SECURITY_CONTEXT = "https://w3id.org/security/v1"
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
AP_MEDIA_TYPE = "application/activity+json"
ACCEPT_AP = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
ACTOR_CACHE_S = 24 * 3600


class FetchError(Exception):
    pass


def wants_activitypub(accept: str | None) -> bool:
    accept = (accept or "").lower()
    return "application/activity+json" in accept or "application/ld+json" in accept


def object_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("br", "p") and self.parts:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """Fremdes HTML nie direkt anzeigen: nur Text übernehmen."""
    parser = _TextExtractor()
    parser.feed(value or "")
    return "".join(parser.parts).strip()


class Federation:
    def __init__(self, settings: Settings, db: Database, key: rsa.RSAPrivateKey):
        self.s = settings
        self.db = db
        self.key = key
        self.user_agent = f"Wegpunkt/{__version__} (+{settings.base_url})"

    # Eigene Objekte ---------------------------------------------------------

    def activity_url(self, activity_id: str) -> str:
        return f"{self.s.base_url}/activities/{activity_id}"

    def local_activity_id(self, url: str | None) -> str | None:
        prefix = f"{self.s.base_url}/activities/"
        if not url or not url.startswith(prefix):
            return None
        return url[len(prefix):].split("#")[0].split("/")[0] or None

    def actor(self) -> dict[str, Any]:
        s = self.s
        return {
            "@context": [AS_CONTEXT, SECURITY_CONTEXT],
            "id": s.actor_url,
            "type": "Person",
            "preferredUsername": s.username,
            "name": s.display_name,
            "summary": html.escape(s.summary),
            "url": s.base_url + "/",
            "inbox": f"{s.actor_url}/inbox",
            "outbox": f"{s.actor_url}/outbox",
            "followers": f"{s.actor_url}/followers",
            "following": f"{s.actor_url}/following",
            "endpoints": {"sharedInbox": f"{s.base_url}/inbox"},
            "manuallyApprovesFollowers": False,
            "discoverable": True,
            "publicKey": {
                "id": s.key_id,
                "owner": s.actor_url,
                "publicKeyPem": public_key_pem(self.key),
            },
        }

    def note(self, a: dict[str, Any]) -> dict[str, Any]:
        url = self.activity_url(a["id"])
        sp = sport(a["sport"])
        seconds = a["moving_s"] or a["duration_s"]
        stats = [f"{sp.emoji} {sp.name}"]
        lines: list[str] = []
        if a.get("kind") == "workout":
            totals = workout_summary(a.get("workout"))
            if seconds:
                stats.append(format_hours(seconds))
            stats.append(f"{totals['sets']} Sätze")
            if totals["reps"]:
                stats.append(f"{totals['reps']} Wdh.")
            lines = [format_exercise(e) for e in a.get("workout") or []]
        else:
            stats.append(format_distance(a["distance_m"]))
            if seconds:
                stats += [format_duration(seconds), format_speed(a["sport"], a["distance_m"], seconds)]
            if a["elevation_gain_m"]:
                stats.append(f"↑ {format_meters(a['elevation_gain_m'])} m")
            if a["sport"] == "enduro" and a.get("elevation_loss_m"):
                stats.append(f"↓ {format_meters(a['elevation_loss_m'])} m")
        tag_url = f"{self.s.base_url}/?sport={a['sport']}"

        content = f"<p><strong>{html.escape(a['title'])}</strong></p>"
        content += f"<p>{html.escape(' · '.join(stats))}</p>"
        if lines:
            content += "<p>" + "<br>".join(html.escape(line) for line in lines) + "</p>"
        if a["description"]:
            content += f"<p>{html.escape(a['description']).replace(chr(10), '<br>')}</p>"
        content += (
            f'<p><a href="{url}">{url}</a> '
            f'<a href="{tag_url}" class="mention hashtag" rel="tag">#<span>{sp.hashtag}</span></a></p>'
        )

        note: dict[str, Any] = {
            "@context": AS_CONTEXT,
            "id": url,
            "type": "Note",
            "attributedTo": self.s.actor_url,
            "url": url,
            "published": a["created_at"],
            "to": [AS_PUBLIC],
            "cc": [f"{self.s.actor_url}/followers"],
            "content": content,
            "tag": [{"type": "Hashtag", "href": tag_url, "name": f"#{sp.hashtag}"}],
        }
        if a["has_image"]:
            note["attachment"] = [{
                "type": "Document",
                "mediaType": "image/png",
                "url": f"{self.s.base_url}/media/{a['id']}.png",
                "name": f"Karte der Route: {a['title']}",
            }]
        return note

    def create(self, a: dict[str, Any]) -> dict[str, Any]:
        note = self.note(a)
        return {
            "@context": AS_CONTEXT,
            "id": f"{note['id']}#create",
            "type": "Create",
            "actor": self.s.actor_url,
            "published": note["published"],
            "to": note["to"],
            "cc": note["cc"],
            "object": {k: v for k, v in note.items() if k != "@context"},
        }

    def delete(self, activity_id: str) -> dict[str, Any]:
        url = self.activity_url(activity_id)
        return {
            "@context": AS_CONTEXT,
            "id": f"{url}#delete",
            "type": "Delete",
            "actor": self.s.actor_url,
            "to": [AS_PUBLIC],
            "cc": [f"{self.s.actor_url}/followers"],
            "object": {"id": url, "type": "Tombstone"},
        }

    # Netzwerk ---------------------------------------------------------------

    async def fetch(self, url: str) -> dict[str, Any]:
        if urlsplit(url).scheme not in ("https", "http"):
            raise FetchError(f"Ungültige URL: {url}")
        headers = sign_request("GET", url, self.key, self.s.key_id)
        headers |= {"Accept": ACCEPT_AP, "User-Agent": self.user_agent}
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise FetchError(f"{url}: {exc}") from exc
        if not isinstance(data, dict):
            raise FetchError(f"{url}: kein JSON-Objekt")
        return data

    async def actor_for_key(self, key_id: str, refresh: bool = False) -> dict[str, Any]:
        """Lädt den Account, dem ein Schlüssel gehört (mit Cache)."""
        url = key_id.split("#")[0]
        if not refresh:
            cached = self.db.get_remote_actor(url, ACTOR_CACHE_S)
            if cached:
                return cached
        data = await self.fetch(url)
        # Manche Server (z. B. GoToSocial) liefern unter der keyId nur den Schlüssel.
        if "inbox" not in data and isinstance(data.get("owner"), str):
            data = await self.fetch(data["owner"])
        if "inbox" not in data or "id" not in data:
            raise FetchError(f"{url}: kein Actor")
        key = data.get("publicKey")
        keys = key if isinstance(key, list) else [key]
        if not any(isinstance(k, dict) and k.get("id") == key_id for k in keys):
            raise FetchError(f"{url}: Schlüssel {key_id} gehört nicht zu diesem Actor")
        self.db.put_remote_actor(url, data)
        return data

    @staticmethod
    def public_key_for(actor: dict[str, Any], key_id: str) -> str | None:
        key = actor.get("publicKey")
        for k in key if isinstance(key, list) else [key]:
            if isinstance(k, dict) and k.get("id") == key_id:
                return k.get("publicKeyPem")
        return None

    async def deliver(self, inbox: str, activity: dict[str, Any]) -> bool:
        body = json.dumps(activity).encode()
        headers = sign_request("POST", inbox, self.key, self.s.key_id, body)
        headers |= {"Content-Type": AP_MEDIA_TYPE, "User-Agent": self.user_agent}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(inbox, content=body, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("Zustellung an %s fehlgeschlagen: %s", inbox, exc)
            return False
        if resp.status_code >= 300:
            log.warning("Zustellung an %s: HTTP %s", inbox, resp.status_code)
            return False
        return True

    async def broadcast(self, activity: dict[str, Any]) -> None:
        inboxes = {f["shared_inbox"] or f["inbox"] for f in self.db.list_followers()}
        limit = asyncio.Semaphore(8)

        async def one(inbox: str) -> None:
            async with limit:
                await self.deliver(inbox, activity)

        await asyncio.gather(*(one(i) for i in inboxes))

    # Inbox ------------------------------------------------------------------

    @staticmethod
    def _actor_info(actor: dict[str, Any]) -> dict[str, Any]:
        host = urlsplit(actor["id"]).netloc
        username = actor.get("preferredUsername") or "?"
        return {
            "actor_url": actor["id"],
            "actor_name": html_to_text(actor.get("name") or "") or username,
            "actor_handle": f"@{username}@{host}",
            "actor_profile": actor.get("url") if isinstance(actor.get("url"), str) else actor["id"],
        }

    async def handle(self, activity: dict[str, Any], actor: dict[str, Any]) -> None:
        """Verarbeitet eine geprüfte Aktivität. `actor` ist der verifizierte Absender."""
        kind = activity.get("type")
        obj = activity.get("object")
        sender = actor["id"]

        if kind == "Follow":
            if object_id(obj) != self.s.actor_url:
                return
            endpoints = actor.get("endpoints") or {}
            self.db.add_follower(sender, actor["inbox"], endpoints.get("sharedInbox"))
            accept = {
                "@context": AS_CONTEXT,
                "id": f"{self.s.actor_url}#accepts/{uuid.uuid4().hex}",
                "type": "Accept",
                "actor": self.s.actor_url,
                "object": activity,
            }
            await self.deliver(actor["inbox"], accept)

        elif kind == "Undo":
            inner_type = obj.get("type") if isinstance(obj, dict) else None
            if inner_type == "Follow":
                self.db.remove_follower(sender)
            elif (inner_id := object_id(obj)) is not None:
                self.db.remove_interaction(inner_id, sender)

        elif kind in ("Like", "Announce"):
            local = self.local_activity_id(object_id(obj))
            ap_id = object_id(activity)
            if local and ap_id and self.db.get_activity(local):
                self.db.add_interaction({
                    "ap_id": ap_id,
                    "activity_id": local,
                    "kind": "like" if kind == "Like" else "announce",
                    **self._actor_info(actor),
                })

        elif kind == "Create" and isinstance(obj, dict):
            local = self.local_activity_id(object_id(obj.get("inReplyTo")))
            if not local or not self.db.get_activity(local) or not obj.get("id"):
                return
            if obj.get("attributedTo") not in (None, sender):
                return
            self.db.add_interaction({
                "ap_id": obj["id"],
                "activity_id": local,
                "kind": "reply",
                "content": html_to_text(obj.get("content") or "")[:5000],
                "url": obj.get("url") if isinstance(obj.get("url"), str) else obj["id"],
                **self._actor_info(actor),
            })

        elif kind == "Delete":
            target = object_id(obj)
            if target == sender:  # Account wurde gelöscht
                self.db.remove_follower(sender)
                self.db.remove_interactions_by_actor(sender)
            elif target:
                self.db.remove_interaction(target, sender)
