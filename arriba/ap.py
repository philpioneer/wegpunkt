"""ActivityPub-Endpunkte: Auffindbarkeit, Actor, Outbox und Inbox."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import __version__
from .federation import AP_MEDIA_TYPE, AS_CONTEXT, FetchError, wants_activitypub
from .httpsig import SignatureError, parse_signature_header, verify_request

if TYPE_CHECKING:
    from .app import Context

log = logging.getLogger("arriba.ap")
MAX_INBOX_BYTES = 1024 * 1024
OUTBOX_PAGE_SIZE = 20


class APResponse(JSONResponse):
    media_type = AP_MEDIA_TYPE


def register(app: FastAPI, ctx: "Context") -> None:
    s, db, fed = ctx.settings, ctx.db, ctx.fed

    def check_user(username: str) -> None:
        if username != s.username:
            raise HTTPException(status_code=404)

    @app.get("/.well-known/webfinger")
    def webfinger(resource: str = ""):
        accepted = {f"acct:{s.username}@{s.domain}", s.actor_url, s.base_url + "/"}
        if resource not in accepted:
            raise HTTPException(status_code=404)
        return JSONResponse(
            {
                "subject": f"acct:{s.username}@{s.domain}",
                "aliases": [s.actor_url, s.base_url + "/"],
                "links": [
                    {"rel": "self", "type": AP_MEDIA_TYPE, "href": s.actor_url},
                    {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html",
                     "href": s.base_url + "/"},
                ],
            },
            media_type="application/jrd+json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.get("/.well-known/nodeinfo")
    def nodeinfo_index():
        return {"links": [{
            "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
            "href": f"{s.base_url}/nodeinfo/2.0",
        }]}

    @app.get("/nodeinfo/2.0")
    def nodeinfo():
        return {
            "version": "2.0",
            "software": {"name": "arriba", "version": __version__},
            "protocols": ["activitypub"],
            "services": {"inbound": [], "outbound": []},
            "openRegistrations": False,
            "usage": {"users": {"total": 1, "activeMonth": 1, "activeHalfyear": 1},
                      "localPosts": db.count_activities()},
            "metadata": {},
        }

    @app.get("/users/{username}")
    def actor(request: Request, username: str):
        check_user(username)
        if wants_activitypub(request.headers.get("accept")):
            return APResponse(fed.actor())
        return RedirectResponse("/", status_code=302)

    @app.get("/users/{username}/outbox")
    def outbox(username: str, page: int | None = None):
        check_user(username)
        url = f"{s.actor_url}/outbox"
        total = db.count_activities()
        if page is None:
            return APResponse({
                "@context": AS_CONTEXT, "id": url, "type": "OrderedCollection",
                "totalItems": total, "first": f"{url}?page=1",
            })
        page = max(page, 1)
        offset = (page - 1) * OUTBOX_PAGE_SIZE
        items = [fed.create(a) for a in db.list_activities(limit=OUTBOX_PAGE_SIZE, offset=offset)]
        for item in items:
            item.pop("@context", None)
        body = {
            "@context": AS_CONTEXT, "id": f"{url}?page={page}", "type": "OrderedCollectionPage",
            "partOf": url, "orderedItems": items,
        }
        if offset + OUTBOX_PAGE_SIZE < total:
            body["next"] = f"{url}?page={page + 1}"
        return APResponse(body)

    @app.get("/users/{username}/followers")
    def followers(username: str):
        check_user(username)
        # Nur die Anzahl: wer dir folgt, bleibt privat.
        return APResponse({
            "@context": AS_CONTEXT, "id": f"{s.actor_url}/followers",
            "type": "OrderedCollection", "totalItems": db.count_followers(),
        })

    @app.get("/users/{username}/following")
    def following(username: str):
        check_user(username)
        return APResponse({
            "@context": AS_CONTEXT, "id": f"{s.actor_url}/following",
            "type": "OrderedCollection", "totalItems": 0, "orderedItems": [],
        })

    async def inbox(request: Request, background: BackgroundTasks) -> Response:
        declared = request.headers.get("content-length", "0")
        if declared.isdigit() and int(declared) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        body = await request.body()
        if len(body) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        try:
            activity = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Kein gültiges JSON")
        if not isinstance(activity, dict):
            raise HTTPException(status_code=400)

        key_id = parse_signature_header(request.headers.get("signature", "")).get("keyId")
        if not key_id:
            raise HTTPException(status_code=401, detail="Signatur fehlt")

        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        try:
            sender = await fed.actor_for_key(key_id)
        except FetchError as exc:
            if activity.get("type") == "Delete":
                # Gelöschte Accounts sind oft nicht mehr abrufbar. Ignorieren.
                return Response(status_code=202)
            log.info("Absender nicht abrufbar: %s", exc)
            raise HTTPException(status_code=401, detail="Absender nicht abrufbar")

        for attempt in range(2):
            pem = fed.public_key_for(sender, key_id) or ""
            try:
                verify_request(request.method, target, request.headers, body, pem)
                break
            except SignatureError as exc:
                if attempt == 0:
                    try:  # Vielleicht wurde der Schlüssel gewechselt
                        sender = await fed.actor_for_key(key_id, refresh=True)
                        continue
                    except FetchError:
                        pass
                log.info("Signatur abgelehnt (%s): %s", key_id, exc)
                raise HTTPException(status_code=401, detail="Signatur ungültig")

        if activity.get("actor") != sender["id"]:
            raise HTTPException(status_code=401, detail="Actor passt nicht zur Signatur")

        fed.handle(activity, sender)
        background.add_task(fed.process_queue)  # z. B. die Bestätigung eines Follows
        return Response(status_code=202)

    app.add_api_route("/inbox", inbox, methods=["POST"])

    @app.post("/users/{username}/inbox")
    async def user_inbox(request: Request, username: str, background: BackgroundTasks):
        check_user(username)
        return await inbox(request, background)
