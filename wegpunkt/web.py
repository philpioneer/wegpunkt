"""Seiten für Menschen: Übersicht, Aktivitäten, Eintragen, Bearbeiten, Profil."""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import fmt, render, stats, tracks, workouts
from .ap import APResponse
from .federation import wants_activitypub
from .sports import GPS_SPORTS, SPORTS, sport

if TYPE_CHECKING:
    from .app import Context

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_AVATAR_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 20


def register(app: FastAPI, ctx: "Context") -> None:
    s, db, fed = ctx.settings, ctx.db, ctx.fed

    # Übersicht ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, sport: str | None = None, page: int = 1):
        owner = ctx.is_owner(request)
        current = sport if sport in SPORTS else None
        page = max(page, 1)
        light = ctx.visible_light(request)
        total = db.count_activities(sport=current, include_private=owner)
        return ctx.render(
            request, "index.html",
            activities=db.list_activities(
                limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, sport=current, include_private=owner
            ),
            totals={
                "count": len(light),
                "distance_m": sum(a["distance_m"] or 0 for a in light),
                "seconds": sum(stats.seconds(a) for a in light),
            },
            week=stats.this_week(light, ctx.tz),
            chart=stats.weekly_chart(light, ctx.tz),
            calendar=stats.calendar(light, ctx.tz),
            records=stats.records(light),
            followers=db.count_followers(),
            current_sport=current,
            page=page,
            has_more=page * PAGE_SIZE < total,
        )

    # Anmeldung ----------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if ctx.is_owner(request):
            return RedirectResponse("/", status_code=303)
        return ctx.render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if ctx.throttle.blocked(ip):
            return ctx.render(request, "login.html", status_code=429,
                              error="Zu viele Fehlversuche. Bitte warte 15 Minuten.")
        if not hmac.compare_digest(password.encode(), s.password.encode()):
            ctx.throttle.fail(ip)
            return ctx.render(request, "login.html", status_code=401, error="Falsches Passwort.")
        ctx.throttle.reset(ip)
        request.session["owner"] = True
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # Eintragen ----------------------------------------------------------------

    def upload_page(request: Request, mode: str, status_code: int = 200, **extra: Any) -> HTMLResponse:
        recent = [a for a in db.list_activities(limit=40, sport="calisthenics", include_private=True)]
        seen: set[str] = set()
        templates_ = []
        for a in recent:  # je Titel nur das neueste Workout als Vorlage anbieten
            if a["title"] not in seen and len(templates_) < 4:
                seen.add(a["title"])
                templates_.append(a)
        page = {
            "mode": mode, "error": None, "values": {}, "rows": [{}, {}, {}],
            "now_local": datetime.now(ctx.tz).strftime("%Y-%m-%dT%H:%M"),
            "workout_templates": templates_, "editing": None,
        }
        return ctx.render(request, "upload.html", status_code=status_code, **{**page, **extra})

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request, type: str = "gps", source: str | None = None):
        ctx.require_owner(request)
        if type != "workout":
            return upload_page(request, "gps")
        extra: dict[str, Any] = {}
        template = db.get_activity(source) if source else None
        if template and template.get("workout"):
            extra = {"rows": workouts.form_rows(template["workout"]),
                     "values": {"title": template["title"],
                                "duration_min": _minutes(template["duration_s"])}}
        return upload_page(request, "workout", **extra)

    def publish(a: dict[str, Any], background: BackgroundTasks) -> RedirectResponse:
        db.add_activity(a)
        a = db.get_activity(a["id"])  # einheitliche Form samt Standardwerten
        if a["visibility"] == "public":
            fed.broadcast(fed.create(a))
            background.add_task(fed.process_queue)
        return RedirectResponse(f"/activities/{a['id']}", status_code=303)

    @app.post("/upload")
    async def upload(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        title: str = Form(""),
        description: str = Form(""),
        sport: str = Form("auto"),
        private: str = Form(""),
    ):
        ctx.require_owner(request)
        values = {"title": title, "description": description, "sport": sport, "private": private}

        def fail(message: str) -> HTMLResponse:
            return upload_page(request, "gps", status_code=400, values=values, error=message)

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return fail("Die Datei ist zu groß (max. 30 MB).")
        try:
            track = tracks.parse(data)
        except tracks.TrackError as exc:
            return fail(str(exc))

        key = sport if sport in GPS_SPORTS else (track.sport_hint or "run")
        sp = SPORTS[key]
        when = track.started_at.astimezone(ctx.tz).strftime("%d.%m.%Y") if track.started_at else None
        a: dict[str, Any] = {
            "id": ctx.new_id(),
            "kind": "gps",
            "title": (title.strip() or track.name or (f"{sp.name} am {when}" if when else sp.name))[:200],
            "description": description.strip()[:5000],
            "sport": key,
            "visibility": "private" if private else "public",
            "created_at": ctx.now(),
        }
        ctx.apply_track(a, track)
        suffix = (".fit" if track.source == "fit" else ".gpx") + (".gz" if data[:2] == b"\x1f\x8b" else "")
        (ctx.original_dir / f"{a['id']}{suffix}").write_bytes(data)
        return publish(a, background)

    def read_workout_form(
        title: str, description: str, started: str, duration_min: str,
        names: list[str], sets: list[str], units: list[str], weights: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, str]]:
        """Gibt (Felder der Aktivität, Formularzeilen, Formularwerte) zurück oder wirft WorkoutError."""
        rows = [
            {"name": n, "sets": sets[i] if i < len(sets) else "",
             "unit": units[i] if i < len(units) else "reps",
             "weight": weights[i] if i < len(weights) else ""}
            for i, n in enumerate(names)
        ]
        values = {"title": title, "description": description, "started": started,
                  "duration_min": duration_min}
        try:
            exercises = workouts.build_workout(names, sets, units, weights)
            try:
                started_dt = (datetime.strptime(started, "%Y-%m-%dT%H:%M").replace(tzinfo=ctx.tz)
                              if started else datetime.now(ctx.tz))
            except ValueError:
                raise workouts.WorkoutError("Datum und Uhrzeit verstehe ich nicht.")
            try:
                minutes = float(duration_min.replace(",", ".")) if duration_min.strip() else None
            except ValueError:
                raise workouts.WorkoutError("Die Dauer muss eine Zahl in Minuten sein.")
            if minutes is not None and not 0 < minutes <= 24 * 60:
                raise workouts.WorkoutError("Die Dauer muss zwischen 1 Minute und 24 Stunden liegen.")
        except workouts.WorkoutError as exc:
            exc.rows, exc.values = rows or [{}], values  # type: ignore[attr-defined]
            raise
        fields = {
            "title": (title.strip() or "Calisthenics")[:200],
            "description": description.strip()[:5000],
            "started_at": started_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": minutes * 60 if minutes else None,
            "workout": exercises,
        }
        return fields, rows, values

    @app.post("/workout")
    def add_workout(
        request: Request,
        background: BackgroundTasks,
        title: str = Form(""),
        description: str = Form(""),
        started: str = Form(""),
        duration_min: str = Form(""),
        private: str = Form(""),
        exercise_name: list[str] = Form([]),
        exercise_sets: list[str] = Form([]),
        exercise_unit: list[str] = Form([]),
        exercise_weight: list[str] = Form([]),
    ):
        ctx.require_owner(request)
        try:
            fields, _, _ = read_workout_form(title, description, started, duration_min,
                                             exercise_name, exercise_sets, exercise_unit, exercise_weight)
        except workouts.WorkoutError as exc:
            values = {**exc.values, "private": private}  # type: ignore[attr-defined]
            return upload_page(request, "workout", status_code=400, values=values,
                               rows=exc.rows, error=str(exc))  # type: ignore[attr-defined]
        return publish({
            "id": ctx.new_id(), "kind": "workout", "sport": "calisthenics",
            "visibility": "private" if private else "public",
            "distance_m": 0.0, "track": [], "has_image": False, "created_at": ctx.now(),
            **fields,
        }, background)

    # Aktivität ----------------------------------------------------------------

    def load(request: Request, activity_id: str) -> dict[str, Any]:
        a = db.get_activity(activity_id) if activity_id.isalnum() else None
        if a is None or (a["visibility"] != "public" and not ctx.is_owner(request)):
            raise HTTPException(status_code=404)
        return a

    @app.get("/activities/{activity_id}")
    def activity_page(request: Request, activity_id: str):
        a = load(request, activity_id)
        if wants_activitypub(request.headers.get("accept")):
            if a["visibility"] != "public":
                raise HTTPException(status_code=404)
            return APResponse(fed.note(a))

        sp = sport(a["sport"])
        interactions = db.list_interactions(activity_id)
        page: dict[str, Any] = {
            "a": a, "sp": sp,
            "likes": [i for i in interactions if i["kind"] == "like"],
            "boosts": [i for i in interactions if i["kind"] == "announce"],
            "replies": [i for i in interactions if i["kind"] == "reply"],
            "has_original": ctx.original_path(activity_id) is not None,
        }
        light = ctx.visible_light(request)
        if a["kind"] == "workout":
            page["summary"] = workouts.summary(a.get("workout"))
            page["history"] = stats.exercise_history(light, a, ctx.tz)
        else:
            profile = tracks.profile(a["track"])
            page.update(
                profile_json=json.dumps(profile, separators=(",", ":")),
                has_elevation=sum(1 for p in profile if p[3] is not None) > 1,
                has_time=sum(1 for p in profile if p[4] is not None) > 1,
                has_hr=sum(1 for p in profile if p[5]) > 10,
                splits=_split_rows(a) if sp.metric == "pace" else [],
                efforts=_effort_rows(a, light) if sp.metric == "pace" else [],
                max_speed=fmt.de(a["extra"]["max_speed_ms"] * 3.6, 1) if a["extra"].get("max_speed_ms") else None,
            )
        return ctx.render(request, "activity.html", **page)

    @app.get("/activities/{activity_id}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, activity_id: str):
        ctx.require_owner(request)
        a = load(request, activity_id)
        return edit_page(request, a)

    def edit_page(request: Request, a: dict[str, Any], status_code: int = 200, **extra: Any) -> HTMLResponse:
        started = ctx.local(a["started_at"])
        page = {
            "a": a, "error": None,
            "values": {
                "title": a["title"], "description": a["description"], "sport": a["sport"],
                "private": a["visibility"] != "public",
                "started": started.strftime("%Y-%m-%dT%H:%M") if started else "",
                "duration_min": _minutes(a["duration_s"]),
            },
            "rows": workouts.form_rows(a.get("workout")) or [{}],
        }
        return ctx.render(request, "edit.html", status_code=status_code, **{**page, **extra})

    @app.post("/activities/{activity_id}/edit")
    def edit(
        request: Request,
        activity_id: str,
        background: BackgroundTasks,
        title: str = Form(""),
        description: str = Form(""),
        sport: str = Form(""),
        private: str = Form(""),
        started: str = Form(""),
        duration_min: str = Form(""),
        exercise_name: list[str] = Form([]),
        exercise_sets: list[str] = Form([]),
        exercise_unit: list[str] = Form([]),
        exercise_weight: list[str] = Form([]),
    ):
        ctx.require_owner(request)
        a = load(request, activity_id)
        was_public = a["visibility"] == "public"

        if a["kind"] == "workout":
            try:
                fields, _, _ = read_workout_form(title, description, started, duration_min,
                                                 exercise_name, exercise_sets, exercise_unit, exercise_weight)
            except workouts.WorkoutError as exc:
                values = {**exc.values, "private": bool(private), "sport": a["sport"]}  # type: ignore[attr-defined]
                return edit_page(request, a, status_code=400, values=values,
                                 rows=exc.rows, error=str(exc))  # type: ignore[attr-defined]
            a.update(fields)
        else:
            if not title.strip():
                return edit_page(request, a, status_code=400, error="Bitte gib einen Titel an.")
            a["title"] = title.strip()[:200]
            a["description"] = description.strip()[:5000]
            if sport in GPS_SPORTS and sport != a["sport"]:
                a["sport"] = sport
                ctx.draw_route(a)  # Farbe des Vorschaubilds passt zur Sportart

        a["visibility"] = "private" if private else "public"
        a["updated_at"] = ctx.now()
        db.update_activity(a)

        is_public = a["visibility"] == "public"
        if was_public and is_public:
            fed.broadcast(fed.update(a))
        elif was_public:
            fed.broadcast(fed.delete(a["id"]))
        elif is_public:
            fed.broadcast(fed.create(a))
        background.add_task(fed.process_queue)
        return RedirectResponse(f"/activities/{activity_id}", status_code=303)

    @app.post("/activities/{activity_id}/delete")
    def delete_activity(request: Request, activity_id: str, background: BackgroundTasks):
        ctx.require_owner(request)
        a = load(request, activity_id)
        db.delete_activity(activity_id)
        (ctx.media_dir / f"{activity_id}.png").unlink(missing_ok=True)
        if original := ctx.original_path(activity_id):
            original.unlink(missing_ok=True)
        if a["visibility"] == "public":
            fed.broadcast(fed.delete(activity_id))
            background.add_task(fed.process_queue)
        return RedirectResponse("/", status_code=303)

    @app.get("/activities/{activity_id}/original")
    def download_original(request: Request, activity_id: str):
        # Das Original enthält Start und Ziel, daher nur für dich.
        ctx.require_owner(request)
        path = ctx.original_path(activity_id)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.get("/media/{name}.png")
    def media(request: Request, name: str):
        if not name.isalnum():
            raise HTTPException(status_code=404)
        if name != "avatar":
            load(request, name)  # private Vorschaubilder nur für dich
        path = ctx.media_dir / f"{name}.png"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600"})

    # Profil -------------------------------------------------------------------

    def settings_page(request: Request, status_code: int = 200, **extra: Any) -> HTMLResponse:
        page = {"error": None, "saved": False, "followers": db.count_followers(),
                "pending": db.count_deliveries()}
        return ctx.render(request, "settings.html", status_code=status_code, **{**page, **extra})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(request: Request, saved: int = 0):
        ctx.require_owner(request)
        return settings_page(request, saved=bool(saved))

    @app.post("/settings")
    async def save_settings(
        request: Request,
        background: BackgroundTasks,
        display_name: str = Form(""),
        summary: str = Form(""),
        remove_avatar: str = Form(""),
        avatar: UploadFile | None = File(None),
    ):
        ctx.require_owner(request)
        if not display_name.strip():
            return settings_page(request, status_code=400, error="Bitte gib einen Namen an.")
        changes = {"display_name": display_name.strip()[:100], "summary": summary.strip()[:500]}

        data = await avatar.read(MAX_AVATAR_BYTES + 1) if avatar and avatar.filename else b""
        if len(data) > MAX_AVATAR_BYTES:
            return settings_page(request, status_code=400, error="Das Bild ist zu groß (max. 8 MB).")
        if data:
            try:
                render.save_avatar(data, ctx.avatar_path)
            except render.AvatarError as exc:
                return settings_page(request, status_code=400, error=str(exc))
            changes["custom_avatar"] = "1"
        elif remove_avatar:
            render.default_avatar(ctx.avatar_path)
            changes["custom_avatar"] = "0"

        db.set_profile(changes)
        fed.broadcast(fed.update_actor())
        background.add_task(fed.process_queue)
        return RedirectResponse("/settings?saved=1", status_code=303)


def _minutes(seconds: float | None) -> str:
    return f"{seconds / 60:g}".replace(".", ",") if seconds else ""


def _split_rows(a: dict[str, Any]) -> list[dict[str, Any]]:
    """Kilometer-Abschnitte mit Tempo; der Balken ist umso länger, je schneller."""
    splits = [x for x in a["extra"].get("splits", []) if x["m"] > 0 and x["s"] > 0]
    if len(splits) < 2:
        return []
    paces = [x["s"] / (x["m"] / 1000) for x in splits]
    fastest, slowest = min(paces), max(paces)
    span = (slowest - fastest) or 1.0
    full = [p for x, p in zip(splits, paces) if x["m"] >= 950]
    best = min(full) if full else None
    rows = []
    for i, (x, p) in enumerate(zip(splits, paces), start=1):
        partial = x["m"] < 950
        rows.append({
            "label": fmt.de(x["m"] / 1000, 2) if partial else str(i),
            "partial": partial,
            "pace": fmt.pace(p),
            "width": round(100 - (p - fastest) / span * 55),
            "ele": round(x["ele"]) if "ele" in x else None,
            "fastest": not partial and p == best,
        })
    return rows


def _effort_rows(a: dict[str, Any], light: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Schnellste Abschnitte dieses Laufs; markiert, wenn es die eigene Bestzeit ist."""
    rows = []
    for label, seconds in a["extra"].get("efforts", {}).items():
        others = [
            o["extra"]["efforts"][label] for o in light
            if o["sport"] == a["sport"] and o["id"] != a["id"] and label in o["extra"].get("efforts", {})
        ]
        rows.append({
            "label": label,
            "time": fmt.duration(seconds),
            "pace": fmt.pace(seconds / (tracks.EFFORT_DISTANCES[label] / 1000)),
            "record": bool(others) and seconds < min(others),
        })
    return rows
