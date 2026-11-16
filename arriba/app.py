"""Baut die Anwendung zusammen: Einstellungen, Datenbank, Föderation, Routen."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, ap, fmt, render, sports, stats, tracks, web, workouts
from .config import Settings, load_settings
from .db import Database, now_iso
from .federation import Federation
from .httpsig import load_or_create_key

log = logging.getLogger("arriba")
HERE = Path(__file__).parent
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 15 * 60
SESSION_MAX_AGE_S = 30 * 24 * 3600
PREVIEW_WITH_MAP = "map-1"
AVATAR_DESIGN = "arriba-1"  # ändert sich, wenn das Standard-Profilbild neu gestaltet wird
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez."]
ERROR_TITLES = {403: "Kein Zugriff", 404: "Nicht gefunden", 413: "Zu groß", 500: "Etwas ist schiefgegangen"}


class LoginThrottle:
    """Bremst das Durchprobieren von Passwörtern: max. 5 Fehlversuche in 15 Minuten."""

    def __init__(self) -> None:
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, ip: str) -> deque[float]:
        q = self.failures[ip]
        while q and time.monotonic() - q[0] > LOGIN_WINDOW_S:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= LOGIN_MAX_FAILURES

    def fail(self, ip: str) -> None:
        self._recent(ip).append(time.monotonic())

    def reset(self, ip: str) -> None:
        self.failures.pop(ip, None)


class Context:
    """Alles, was die Routen brauchen."""

    def __init__(self, settings: Settings):
        s = self.settings = settings
        self.media_dir = s.data_dir / "media"
        self.original_dir = s.data_dir / "original"
        self.legacy_gpx_dir = s.data_dir / "gpx"
        for d in (self.media_dir, self.original_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.avatar_path = self.media_dir / "avatar.png"

        self.tiles = render.TileSource(
            s.data_dir / "cache" / "tiles", f"Arriba/{__version__} (+{s.base_url})"
        ) if s.map_previews else None

        db_path = s.data_dir / "arriba.db"
        if not db_path.exists():  # Datenbank aus der Zeit vor der Umbenennung übernehmen
            for suffix in ("", "-wal", "-shm"):
                old = s.data_dir / f"wegpunkt.db{suffix}"
                if old.exists():
                    old.rename(s.data_dir / f"arriba.db{suffix}")
        self.db = Database(db_path)
        stored = self.db.get_profile()
        outdated = stored.get("custom_avatar") != "1" and stored.get("avatar_design") != AVATAR_DESIGN
        if outdated or not self.avatar_path.exists():
            render.default_avatar(self.avatar_path)
            self.db.set_profile({"avatar_design": AVATAR_DESIGN})
        self.fed = Federation(s, self.db, load_or_create_key(s.data_dir / "private_key.pem"), self.profile)
        self.tz = ZoneInfo(s.timezone)
        self.throttle = LoginThrottle()
        self.templates = Jinja2Templates(directory=HERE / "templates")
        self._setup_templates()

    # Profil ---------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        """Name und Text aus der Oberfläche, sonst aus den Einstellungen."""
        stored = self.db.get_profile()
        try:
            version = int(self.avatar_path.stat().st_mtime)
        except OSError:
            version = 0
        return {
            "display_name": stored.get("display_name") or self.settings.display_name,
            "summary": stored.get("summary", self.settings.summary),
            "avatar_version": version,
            "custom_avatar": stored.get("custom_avatar") == "1",
        }

    # Anmeldung ------------------------------------------------------------

    @staticmethod
    def is_owner(request: Request) -> bool:
        return request.session.get("owner") is True

    def require_owner(self, request: Request) -> None:
        if not self.is_owner(request):
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        # Formulare dürfen nur von dieser Seite selbst kommen.
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="Anfrage von fremder Seite abgelehnt")

    # Seiten ---------------------------------------------------------------

    def render(self, request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request, name,
            {"is_owner": self.is_owner(request), "profile": self.profile(),
             "nonce": getattr(request.state, "nonce", ""), **ctx},
            status_code=status_code,
        )

    def local(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(self.tz)

    def _setup_templates(self) -> None:
        def local_time(value: str | None) -> str:
            dt = self.local(value)
            return dt.strftime("%d.%m.%Y, %H:%M Uhr") if dt else ""

        def nice_date(value: str | None) -> str:
            """'Sa, 18. Okt. 2026 · 09:00'"""
            dt = self.local(value)
            if not dt:
                return ""
            return f"{WEEKDAYS[dt.weekday()]}, {dt.day}. {MONTHS[dt.month - 1]} {dt.year} · {dt:%H:%M}"

        def initials(name: str) -> str:
            parts = [p for p in name.replace("@", " ").split() if p]
            return "".join(p[0] for p in parts[:2]).upper() or "?"

        env = self.templates.env
        env.filters.update(
            distance=fmt.distance, duration=fmt.duration, hours=fmt.hours, meters=fmt.meters,
            local_time=local_time, nice_date=nice_date, initials=initials,
            weight=workouts.format_weight,
        )
        env.globals.update(
            settings=self.settings, sports=sports.SPORTS, gps_sports=sports.GPS_SPORTS,
            sport=sports.sport, speed_parts=fmt.speed_parts, distance_parts=fmt.distance_parts,
            route_path=render.route_path, workout_summary=workouts.summary,
            units=workouts.UNITS, suggestions=workouts.SUGGESTIONS, version=__version__,
        )

    # Aufzeichnungen -------------------------------------------------------

    def original_path(self, activity_id: str) -> Path | None:
        if not activity_id.isalnum():
            return None
        found = sorted(self.original_dir.glob(f"{activity_id}.*"))
        if found:
            return found[0]
        legacy = self.legacy_gpx_dir / f"{activity_id}.gpx"
        return legacy if legacy.exists() else None

    def apply_track(self, a: dict[str, Any], track: tracks.Track) -> None:
        """Überträgt die Auswertung auf die Aktivität und zeichnet das Vorschaubild."""
        visible = tracks.visible_points(track, self.settings.privacy_radius_m)
        a.update(
            started_at=track.started_at.strftime("%Y-%m-%dT%H:%M:%SZ") if track.started_at else None,
            distance_m=track.distance_m,
            duration_s=track.duration_s,
            moving_s=track.moving_s,
            elevation_gain_m=track.elevation_gain_m,
            elevation_loss_m=track.elevation_loss_m,
            track=visible,
            extra=track.extra,
        )
        self.draw_route(a)

    def draw_route(self, a: dict[str, Any]) -> None:
        """Vorschaubild mit Karte; ohne Netz oder bei Fehlern nur die Linie."""
        path = self.media_dir / f"{a['id']}.png"
        color = sports.sport(a["sport"]).color
        extra = a.setdefault("extra", {})
        if self.tiles is not None:
            try:
                a["has_image"] = render.route_map_png(a["track"], path, color, self.tiles)
                extra["preview"] = PREVIEW_WITH_MAP
                return
            except Exception as exc:  # Kartenserver nicht erreichbar o. Ä.
                log.warning("Kartenbild für %s nicht möglich (%s), zeichne ohne Karte", a["id"], exc)
            finally:
                self.tiles.close()
        a["has_image"] = render.route_png(a["track"], path, color)
        extra["preview"] = "plain"

    def refresh_previews(self, pause_s: float = 1.0) -> int:
        """Zeichnet ältere Vorschaubilder mit Kartenhintergrund neu, eines nach dem anderen."""
        if self.tiles is None:
            return 0
        count = 0
        for activity_id in self.db.gps_ids():
            a = self.db.get_activity(activity_id)
            if not a or not a["track"] or a["extra"].get("preview") == PREVIEW_WITH_MAP:
                continue
            self.draw_route(a)
            if a["extra"]["preview"] != PREVIEW_WITH_MAP:
                break  # Kartenserver nicht erreichbar: beim nächsten Start weiter
            fresh = self.db.get_activity(activity_id)  # zwischenzeitliche Änderungen behalten
            if fresh:
                fresh["has_image"] = a["has_image"]
                fresh["extra"] = {**fresh["extra"], "preview": PREVIEW_WITH_MAP}
                self.db.update_activity(fresh)
                count += 1
            time.sleep(pause_s)  # den Kartenserver schonen
        return count

    def reprocess_outdated(self) -> int:
        """Wertet ältere Aufzeichnungen mit der aktuellen Auswertung neu aus."""
        count = 0
        for activity_id in self.db.gps_ids():
            a = self.db.get_activity(activity_id)
            path = self.original_path(activity_id)
            if not a or not path or a["extra"].get("version") == tracks.ANALYSIS_VERSION:
                continue
            try:
                self.apply_track(a, tracks.parse(path.read_bytes()))
                self.db.update_activity(a)
                count += 1
            except Exception:  # eine kaputte Datei darf den Start nicht verhindern
                log.exception("Aktivität %s ließ sich nicht neu auswerten", activity_id)
        return count

    def visible_light(self, request: Request) -> list[dict[str, Any]]:
        return self.db.all_light(include_private=self.is_owner(request))

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(8)

    @staticmethod
    def now() -> str:
        return now_iso()


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or load_settings()
    ctx = Context(s)
    updated = ctx.reprocess_outdated()
    if updated:
        log.info("%s Aktivitäten neu ausgewertet", updated)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = asyncio.create_task(ctx.fed.run_queue_forever())
        previews = asyncio.create_task(asyncio.to_thread(ctx.refresh_previews))
        try:
            yield
        finally:
            worker.cancel()
            previews.cancel()

    app = FastAPI(title="Arriba", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(16)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{request.state.nonce}'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.opentopomap.org; "
                "connect-src 'self'; object-src 'none'; base-uri 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
        return response

    # Nach der eigenen Middleware eintragen, damit die Sitzung dort schon bereitsteht.
    app.add_middleware(
        SessionMiddleware, secret_key=s.secret_key, https_only=s.https, same_site="lax",
        max_age=SESSION_MAX_AGE_S,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        location = (exc.headers or {}).get("Location")
        if exc.status_code in (302, 303) and location:
            return RedirectResponse(location, status_code=exc.status_code)
        if "text/html" in request.headers.get("accept", ""):
            return ctx.render(
                request, "error.html", status_code=exc.status_code, status=exc.status_code,
                title=ERROR_TITLES.get(exc.status_code, "Fehler"),
                detail=exc.detail if exc.status_code not in (404, 500) else None,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    web.register(app, ctx)
    ap.register(app, ctx)
    return app
