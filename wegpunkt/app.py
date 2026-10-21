"""Web-App: Seiten für Menschen und ActivityPub-Endpunkte für andere Server."""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, tracks
from .config import Settings, load_settings
from .db import Database, now_iso
from .federation import (
    AP_MEDIA_TYPE,
    AS_CONTEXT,
    Federation,
    FetchError,
    wants_activitypub,
)
from .httpsig import SignatureError, load_or_create_key, parse_signature_header, verify_request

log = logging.getLogger("wegpunkt")
HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


class APResponse(JSONResponse):
    media_type = AP_MEDIA_TYPE


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or load_settings()
    (s.data_dir / "media").mkdir(parents=True, exist_ok=True)
    (s.data_dir / "gpx").mkdir(parents=True, exist_ok=True)
    db = Database(s.data_dir / "wegpunkt.db")
    fed = Federation(s, db, load_or_create_key(s.data_dir / "private_key.pem"))
    tz = ZoneInfo(s.timezone)

    app = FastAPI(title="Wegpunkt", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=s.secret_key, https_only=s.https, same_site="lax"
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.settings, app.state.db, app.state.federation = s, db, fed

    templates = Jinja2Templates(directory=HERE / "templates")

    def local_time(value: str | None) -> str:
        if not value:
            return ""
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%d.%m.%Y, %H:%M Uhr")

    templates.env.filters.update(
        distance=tracks.format_distance, duration=tracks.format_duration, local_time=local_time
    )
    templates.env.globals.update(
        settings=s, sports=tracks.SPORTS, speed=tracks.format_speed
    )

    def is_owner(request: Request) -> bool:
        return request.session.get("owner") is True

    def require_owner(request: Request) -> None:
        if not is_owner(request):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"is_owner": is_owner(request), **ctx})

    def check_user(username: str) -> None:
        if username != s.username:
            raise HTTPException(status_code=404)

    # Seiten -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, sport: str | None = None):
        if sport not in tracks.SPORTS:
            sport = None
        return render(
            request,
            "index.html",
            activities=db.list_activities(limit=100, sport=sport),
            totals=db.activity_totals(),
            followers=db.count_followers(),
            sport=sport,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if not hmac.compare_digest(password.encode(), s.password.encode()):
            return render(request, "login.html", error="Falsches Passwort.")
        request.session["owner"] = True
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request):
        require_owner(request)
        return render(request, "upload.html", error=None)

    @app.post("/upload")
    async def upload(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        title: str = Form(""),
        description: str = Form(""),
        sport: str = Form("auto"),
    ):
        require_owner(request)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return render(request, "upload.html", error="Die Datei ist zu groß (max. 30 MB).")
        try:
            track = tracks.parse_gpx(data)
        except tracks.TrackError as exc:
            return render(request, "upload.html", error=str(exc))

        if sport not in tracks.SPORTS:
            sport = track.sport_hint or "run"
        activity_id = uuid.uuid4().hex[:16]
        visible = tracks.downsample(tracks.apply_privacy_zone(track.points, s.privacy_radius_m), 2000)
        has_image = tracks.render_route_png(visible, s.data_dir / "media" / f"{activity_id}.png")
        (s.data_dir / "gpx" / f"{activity_id}.gpx").write_bytes(data)

        default_title = f"{tracks.SPORTS[sport][0]} am " + (
            track.started_at.astimezone(tz).strftime("%d.%m.%Y") if track.started_at else "Tag X"
        )
        activity = {
            "id": activity_id,
            "title": (title.strip() or track.name or default_title)[:200],
            "description": description.strip()[:5000],
            "sport": sport,
            "started_at": track.started_at.strftime("%Y-%m-%dT%H:%M:%SZ") if track.started_at else None,
            "distance_m": track.distance_m,
            "duration_s": track.duration_s,
            "moving_s": track.moving_s,
            "elevation_gain_m": track.elevation_gain_m,
            "track": [list(p) for p in visible],
            "has_image": has_image,
            "created_at": now_iso(),
        }
        db.add_activity(activity)
        background.add_task(fed.broadcast, fed.create(activity))
        return RedirectResponse(f"/activities/{activity_id}", status_code=303)

    @app.get("/activities/{activity_id}")
    def activity_page(request: Request, activity_id: str):
        a = db.get_activity(activity_id)
        if a is None:
            raise HTTPException(status_code=404)
        if wants_activitypub(request.headers.get("accept")):
            return APResponse(fed.note(a))
        interactions = db.list_interactions(activity_id)
        return render(
            request,
            "activity.html",
            a=a,
            track_json=json.dumps([[p[0], p[1]] for p in a["track"]]),
            profile=tracks.elevation_profile([tuple(p) for p in a["track"]]),
            likes=[i for i in interactions if i["kind"] == "like"],
            boosts=[i for i in interactions if i["kind"] == "announce"],
            replies=[i for i in interactions if i["kind"] == "reply"],
        )

    @app.post("/activities/{activity_id}/delete")
    def delete_activity(request: Request, activity_id: str, background: BackgroundTasks):
        require_owner(request)
        if not db.delete_activity(activity_id):
            raise HTTPException(status_code=404)
        for path in (s.data_dir / "media" / f"{activity_id}.png", s.data_dir / "gpx" / f"{activity_id}.gpx"):
            path.unlink(missing_ok=True)
        background.add_task(fed.broadcast, fed.delete(activity_id))
        return RedirectResponse("/", status_code=303)

    @app.get("/activities/{activity_id}/gpx")
    def download_gpx(request: Request, activity_id: str):
        # Das Original enthält Start und Ziel, daher nur für dich.
        require_owner(request)
        path = s.data_dir / "gpx" / f"{activity_id}.gpx"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/gpx+xml", filename=f"{activity_id}.gpx")

    @app.get("/media/{activity_id}.png")
    def media(activity_id: str):
        path = s.data_dir / "media" / f"{activity_id}.png"
        if not activity_id.isalnum() or not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    # ActivityPub & Discovery ------------------------------------------------

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
                    {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html", "href": s.base_url + "/"},
                ],
            },
            media_type="application/jrd+json",
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
            "software": {"name": "wegpunkt", "version": __version__},
            "protocols": ["activitypub"],
            "services": {"inbound": [], "outbound": []},
            "openRegistrations": False,
            "usage": {"users": {"total": 1}, "localPosts": db.activity_totals()["count"]},
            "metadata": {},
        }

    @app.get("/users/{username}")
    def actor(request: Request, username: str):
        check_user(username)
        if wants_activitypub(request.headers.get("accept")):
            return APResponse(fed.actor())
        return RedirectResponse("/", status_code=302)

    @app.get("/users/{username}/outbox")
    def outbox(username: str):
        check_user(username)
        items = [fed.create(a) for a in db.list_activities(limit=20)]
        for item in items:
            item.pop("@context", None)
        return APResponse({
            "@context": AS_CONTEXT,
            "id": f"{s.actor_url}/outbox",
            "type": "OrderedCollection",
            "totalItems": db.activity_totals()["count"],
            "orderedItems": items,
        })

    @app.get("/users/{username}/followers")
    def followers(username: str):
        check_user(username)
        # Nur die Anzahl: wer dir folgt, bleibt privat.
        return APResponse({
            "@context": AS_CONTEXT,
            "id": f"{s.actor_url}/followers",
            "type": "OrderedCollection",
            "totalItems": db.count_followers(),
        })

    @app.get("/users/{username}/following")
    def following(username: str):
        check_user(username)
        return APResponse({
            "@context": AS_CONTEXT,
            "id": f"{s.actor_url}/following",
            "type": "OrderedCollection",
            "totalItems": 0,
            "orderedItems": [],
        })

    async def inbox(request: Request) -> Response:
        body = await request.body()
        try:
            activity = json.loads(body)
        except json.JSONDecodeError:
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

        await fed.handle(activity, sender)
        return Response(status_code=202)

    app.add_api_route("/inbox", inbox, methods=["POST"])

    @app.post("/users/{username}/inbox")
    async def user_inbox(request: Request, username: str):
        check_user(username)
        return await inbox(request)

    return app
