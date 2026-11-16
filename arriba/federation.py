"""ActivityPub: eigene Objekte bauen, fremde Accounts laden, zustellen, Inbox verarbeiten."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import socket
import uuid
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from . import __version__
from .config import Settings
from .db import Database
from .httpsig import public_key_pem, sign_request
from .mastodon import stats_line
from .sports import sport
from .workouts import format_exercise

log = logging.getLogger("arriba.federation")

AS_CONTEXT = "https://www.w3.org/ns/activitystreams"
SECURITY_CONTEXT = "https://w3id.org/security/v1"
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
AP_MEDIA_TYPE = "application/activity+json"
ACCEPT_AP = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
ACTOR_CACHE_S = 24 * 3600
ACTOR_REFRESH_MIN_AGE_S = 300   # nach abgelehnter Signatur höchstens alle 5 Minuten neu laden
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
# Wartezeit vor dem nächsten Zustellversuch; danach wird aufgegeben (nach gut drei Tagen).
RETRY_DELAYS_S = (60, 300, 1800, 7200, 21600, 43200, 86400, 86400)


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
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p") and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Fremdes HTML nie direkt anzeigen: nur Text übernehmen."""
    parser = _TextExtractor()
    parser.feed(value or "")
    return "".join(parser.parts).strip()


def text_to_html(value: str) -> str:
    paragraphs = [p for p in value.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p.strip()).replace(chr(10), '<br>')}</p>" for p in paragraphs)


class Federation:
    def __init__(
        self, settings: Settings, db: Database, key: rsa.RSAPrivateKey,
        profile: Callable[[], dict[str, Any]],
    ):
        self.s = settings
        self.db = db
        self.key = key
        self.profile = profile
        self.user_agent = f"Arriba/{__version__} (+{settings.base_url})"
        self._lock = asyncio.Lock()

    # Eigene Objekte ---------------------------------------------------------

    def activity_url(self, activity_id: str) -> str:
        return f"{self.s.base_url}/activities/{activity_id}"

    def local_activity_id(self, url: str | None) -> str | None:
        prefix = f"{self.s.base_url}/activities/"
        if not url or not url.startswith(prefix):
            return None
        return url[len(prefix):].split("#")[0].split("/")[0].split("?")[0] or None

    def actor(self) -> dict[str, Any]:
        s, p = self.s, self.profile()
        actor: dict[str, Any] = {
            "@context": [AS_CONTEXT, SECURITY_CONTEXT],
            "id": s.actor_url,
            "type": "Person",
            "preferredUsername": s.username,
            "name": p["display_name"],
            "summary": text_to_html(p["summary"]),
            "url": s.base_url + "/",
            "inbox": f"{s.actor_url}/inbox",
            "outbox": f"{s.actor_url}/outbox",
            "followers": f"{s.actor_url}/followers",
            "following": f"{s.actor_url}/following",
            "endpoints": {"sharedInbox": f"{s.base_url}/inbox"},
            "manuallyApprovesFollowers": False,
            "discoverable": True,
            "indexable": True,
            "icon": {
                "type": "Image",
                "mediaType": "image/png",
                "url": f"{s.base_url}/media/avatar.png?v={p['avatar_version']}",
            },
            "publicKey": {
                "id": s.key_id,
                "owner": s.actor_url,
                "publicKeyPem": public_key_pem(self.key),
            },
        }
        return actor

    def note(self, a: dict[str, Any]) -> dict[str, Any]:
        url = self.activity_url(a["id"])
        sp = sport(a["sport"])
        tag_url = f"{self.s.base_url}/?sport={a['sport']}"

        content = f"<p><strong>{html.escape(a['title'])}</strong></p>"
        content += f"<p>{html.escape(' · '.join(stats_line(a)))}</p>"
        if a.get("kind") == "workout" and a.get("workout"):
            lines = [html.escape(format_exercise(e)) for e in a["workout"]]
            content += "<p>" + "<br>".join(lines) + "</p>"
        if a["description"]:
            content += text_to_html(a["description"])
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
        if a.get("updated_at"):
            note["updated"] = a["updated_at"]
        if a["has_image"]:
            version = (a.get("updated_at") or a["created_at"]).replace(":", "")
            note["attachment"] = [{
                "type": "Document",
                "mediaType": "image/png",
                "url": f"{self.s.base_url}/media/{a['id']}.png?v={version}",
                "name": f"Karte der Route: {a['title']}",
            }]
        return note

    def _wrap(self, kind: str, obj: Any, suffix: str, activity_id: str | None = None) -> dict[str, Any]:
        base = self.activity_url(activity_id) if activity_id else self.s.actor_url
        return {
            "@context": AS_CONTEXT,
            "id": f"{base}#{suffix}",
            "type": kind,
            "actor": self.s.actor_url,
            "to": [AS_PUBLIC],
            "cc": [f"{self.s.actor_url}/followers"],
            "object": obj,
        }

    def create(self, a: dict[str, Any]) -> dict[str, Any]:
        note = {k: v for k, v in self.note(a).items() if k != "@context"}
        return {**self._wrap("Create", note, "create", a["id"]), "published": note["published"]}

    def update(self, a: dict[str, Any]) -> dict[str, Any]:
        note = {k: v for k, v in self.note(a).items() if k != "@context"}
        return self._wrap("Update", note, f"updates/{uuid.uuid4().hex}", a["id"])

    def delete(self, activity_id: str) -> dict[str, Any]:
        tombstone = {"id": self.activity_url(activity_id), "type": "Tombstone"}
        return self._wrap("Delete", tombstone, "delete", activity_id)

    def update_actor(self) -> dict[str, Any]:
        actor = {k: v for k, v in self.actor().items() if k != "@context"}
        wrapped = self._wrap("Update", actor, f"updates/{uuid.uuid4().hex}")
        wrapped["@context"] = [AS_CONTEXT, SECURITY_CONTEXT]
        return wrapped

    # Netzwerk ---------------------------------------------------------------

    async def guard(self, url: str) -> None:
        """Verhindert, dass fremde Server uns auf interne Adressen schicken."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"Ungültige Adresse: {url}")
        if not self.s.https:
            return  # lokale Entwicklung: alles erlaubt
        if parts.scheme != "https":
            raise FetchError(f"Nur HTTPS ist erlaubt: {url}")
        try:
            addresses = [ipaddress.ip_address(parts.hostname)]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
                )
            except OSError as exc:
                raise FetchError(f"{parts.hostname} ist nicht auflösbar") from exc
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        if not addresses or not all(ip.is_global for ip in addresses):
            raise FetchError(f"{parts.hostname} zeigt auf eine interne Adresse")

    async def fetch(self, url: str) -> dict[str, Any]:
        """Lädt ein ActivityPub-Objekt, signiert, mit geprüften Weiterleitungen."""
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    await self.guard(url)
                    headers = sign_request("GET", url, self.key, self.s.key_id)
                    headers |= {"Accept": ACCEPT_AP, "User-Agent": self.user_agent}
                    resp = await client.get(url, headers=headers)
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    break
                else:
                    raise FetchError(f"{url}: zu viele Weiterleitungen")
            resp.raise_for_status()
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise FetchError(f"{url}: Antwort zu groß")
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError(f"{url}: {exc}") from exc
        if not isinstance(data, dict):
            raise FetchError(f"{url}: kein JSON-Objekt")
        return data

    async def post(self, inbox: str, body: bytes) -> int:
        """Stellt eine signierte Aktivität zu und gibt den HTTP-Status zurück."""
        await self.guard(inbox)
        headers = sign_request("POST", inbox, self.key, self.s.key_id, body)
        headers |= {"Content-Type": AP_MEDIA_TYPE, "User-Agent": self.user_agent}
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resp = await client.post(inbox, content=body, headers=headers)
        return resp.status_code

    async def actor_for_key(self, key_id: str, refresh: bool = False) -> dict[str, Any]:
        """Lädt den Account, dem ein Schlüssel gehört (mit Cache)."""
        url = key_id.split("#")[0]
        cached = self.db.get_remote_actor(url)
        if cached:
            data, age = cached
            if not refresh and age < ACTOR_CACHE_S:
                return data
            if refresh and age < ACTOR_REFRESH_MIN_AGE_S:
                return data
        data = await self.fetch(url)
        # Manche Server (z. B. GoToSocial) liefern unter der keyId nur den Schlüssel.
        if "inbox" not in data and isinstance(data.get("owner"), str):
            data = await self.fetch(data["owner"])
        if not isinstance(data.get("inbox"), str) or not isinstance(data.get("id"), str):
            raise FetchError(f"{url}: kein Actor")
        if self.public_key_for(data, key_id) is None:
            raise FetchError(f"{url}: Schlüssel {key_id} gehört nicht zu diesem Actor")
        self.db.put_remote_actor(url, data)
        return data

    @staticmethod
    def public_key_for(actor: dict[str, Any], key_id: str) -> str | None:
        key = actor.get("publicKey")
        for k in key if isinstance(key, list) else [key]:
            if isinstance(k, dict) and k.get("id") == key_id and isinstance(k.get("publicKeyPem"), str):
                return k["publicKeyPem"]
        return None

    # Zustellen --------------------------------------------------------------

    def follower_inboxes(self) -> list[str]:
        return sorted({f["shared_inbox"] or f["inbox"] for f in self.db.list_followers()})

    def broadcast(self, activity: dict[str, Any]) -> None:
        """Legt die Aktivität für alle Follower in die Warteschlange."""
        self.db.enqueue_deliveries(self.follower_inboxes(), json.dumps(activity))

    def send_to(self, inbox: str, activity: dict[str, Any]) -> None:
        self.db.enqueue_deliveries([inbox], json.dumps(activity))

    async def process_queue(self) -> int:
        """Stellt alles Fällige zu. Fehlschläge werden später erneut versucht."""
        async with self._lock:
            due = self.db.due_deliveries()
            limit = asyncio.Semaphore(8)

            async def one(d: dict[str, Any]) -> None:
                async with limit:
                    error, permanent = await self._attempt(d["inbox"], d["body"].encode())
                if error is None:
                    self.db.delivery_done(d["id"])
                    return
                attempts = d["attempts"]
                give_up = permanent or attempts >= len(RETRY_DELAYS_S)
                log.warning("Zustellung an %s fehlgeschlagen (%s)%s", d["inbox"], error,
                            ", gebe auf" if give_up else "")
                self.db.delivery_failed(d["id"], error, None if give_up else RETRY_DELAYS_S[attempts])

            await asyncio.gather(*(one(d) for d in due))
            return len(due)

    async def _attempt(self, inbox: str, body: bytes) -> tuple[str | None, bool]:
        """Gibt (Fehler oder None, endgültig?) zurück."""
        try:
            status = await self.post(inbox, body)
        except FetchError as exc:
            return str(exc), True
        except httpx.HTTPError as exc:
            return f"{type(exc).__name__}: {exc}", False
        if status < 300:
            return None, False
        # 404/410: die Inbox gibt es nicht mehr. Andere 4xx sind meist dauerhaft,
        # außer Zeitüberschreitung und Drosselung.
        permanent = 400 <= status < 500 and status not in (408, 429)
        return f"HTTP {status}", permanent

    async def run_queue_forever(self, interval_s: float = 30) -> None:
        while True:
            try:
                await self.process_queue()
            except Exception:  # der Hintergrunddienst darf nie sterben
                log.exception("Fehler in der Zustell-Warteschlange")
            await asyncio.sleep(interval_s)

    # Inbox ------------------------------------------------------------------

    @staticmethod
    def _actor_info(actor: dict[str, Any]) -> dict[str, Any]:
        host = urlsplit(actor["id"]).netloc
        username = str(actor.get("preferredUsername") or "?")[:100]
        profile = actor.get("url") if isinstance(actor.get("url"), str) else actor["id"]
        if urlsplit(profile).scheme not in ("http", "https"):
            profile = actor["id"]
        return {
            "actor_url": actor["id"],
            "actor_name": (html_to_text(str(actor.get("name") or "")) or username)[:100],
            "actor_handle": f"@{username}@{host}",
            "actor_profile": profile,
        }

    def _public_activity(self, url: str | None) -> str | None:
        """Lokale ID, wenn die Adresse eine öffentliche Aktivität von uns meint."""
        local = self.local_activity_id(url)
        if not local:
            return None
        a = self.db.get_activity(local)
        return local if a and a["visibility"] == "public" else None

    def handle(self, activity: dict[str, Any], actor: dict[str, Any]) -> None:
        """Verarbeitet eine geprüfte Aktivität. `actor` ist der verifizierte Absender."""
        kind = activity.get("type")
        obj = activity.get("object")
        sender = actor["id"]

        if kind == "Follow":
            if object_id(obj) != self.s.actor_url:
                return
            endpoints = actor.get("endpoints") if isinstance(actor.get("endpoints"), dict) else {}
            shared = endpoints.get("sharedInbox")
            self.db.add_follower(sender, actor["inbox"], shared if isinstance(shared, str) else None)
            self.send_to(actor["inbox"], {
                "@context": AS_CONTEXT,
                "id": f"{self.s.actor_url}#accepts/{uuid.uuid4().hex}",
                "type": "Accept",
                "actor": self.s.actor_url,
                "object": activity,
            })

        elif kind == "Undo":
            inner_type = obj.get("type") if isinstance(obj, dict) else None
            if inner_type == "Follow":
                self.db.remove_follower(sender)
                return
            if (inner_id := object_id(obj)) is not None:
                self.db.remove_interaction(inner_id, sender)
            if inner_type in ("Like", "Announce") and isinstance(obj, dict):
                # Manche Server schicken das Undo mit neuer ID; dann zählt das Ziel.
                local = self.local_activity_id(object_id(obj.get("object")))
                if local:
                    self.db.remove_interaction_on(
                        "like" if inner_type == "Like" else "announce", local, sender
                    )

        elif kind in ("Like", "Announce"):
            local = self._public_activity(object_id(obj))
            ap_id = object_id(activity)
            if local and ap_id:
                name = "like" if kind == "Like" else "announce"
                self.db.remove_interaction_on(name, local, sender)  # nie doppelt zählen
                self.db.add_interaction({
                    "ap_id": ap_id, "activity_id": local, "kind": name, **self._actor_info(actor),
                })

        elif kind == "Create" and isinstance(obj, dict):
            local = self._public_activity(object_id(obj.get("inReplyTo")))
            if not local or not isinstance(obj.get("id"), str):
                return
            if obj.get("attributedTo") not in (None, sender):
                return
            url = obj.get("url") if isinstance(obj.get("url"), str) else obj["id"]
            if urlsplit(url).scheme not in ("http", "https"):
                url = obj["id"]
            self.db.add_interaction({
                "ap_id": obj["id"],
                "activity_id": local,
                "kind": "reply",
                "content": html_to_text(str(obj.get("content") or ""))[:5000],
                "url": url,
                **self._actor_info(actor),
            })

        elif kind == "Update" and isinstance(obj, dict) and isinstance(obj.get("id"), str):
            if obj.get("type") == "Note":
                self.db.update_reply(obj["id"], sender, html_to_text(str(obj.get("content") or ""))[:5000])
            elif obj["id"] == sender:
                self.db.put_remote_actor(sender, actor)

        elif kind == "Delete":
            target = object_id(obj)
            if target == sender:  # Account wurde gelöscht
                self.db.remove_follower(sender)
                self.db.remove_interactions_by_actor(sender)
            elif target:
                self.db.remove_interaction(target, sender)
