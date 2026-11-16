import sqlite3

from arriba import stats, tracks
from arriba.app import create_app
from arriba.config import load_settings, read_env_file
from arriba.db import Database
from arriba.workouts import WorkoutError, build_workout, parse_sets, summary

import pytest
from fastapi.testclient import TestClient
from zoneinfo import ZoneInfo

from .conftest import GPX, fit_bytes, make_settings


def test_pages_need_login(env):
    c = env.client
    for path in ("/upload", "/settings"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.post("/upload", files={"file": ("run.gpx", GPX)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.post("/workout", data={}, follow_redirects=False).status_code == 303


def test_login_is_throttled(env):
    c = env.client
    for _ in range(5):
        assert "Falsches Passwort" in c.post("/login", data={"password": "nein"}).text
    assert "Zu viele Fehlversuche" in c.post("/login", data={"password": "geheim"}).text


def test_forms_from_other_sites_are_rejected(env):
    env.login()
    r = env.client.post("/workout", data={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    env.workout()  # ohne oder mit eigener Herkunft klappt es
    r = env.client.post("/settings", data={"display_name": "P"}, headers={"Origin": "http://testserver"},
                        follow_redirects=False)
    assert r.status_code == 303


def test_security_headers_and_error_page(env):
    r = env.client.get("/")
    csp = r.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in r.text and "frame-ancestors 'none'" in csp
    assert r.text.count("<script") == r.text.count("<script nonce=") + r.text.count("<script src=")
    assert r.headers["x-content-type-options"] == "nosniff"

    missing = env.client.get("/activities/gibtsnicht", headers={"Accept": "text/html"})
    assert missing.status_code == 404 and "Nicht gefunden" in missing.text
    assert env.client.get("/activities/gibtsnicht").json() == {"detail": "Not Found"}


def test_gps_activity_page(env):
    env.login()
    activity_id = env.upload()
    page = env.client.get(f"/activities/{activity_id}").text
    for text in ("Morgenrunde", "3,92", "Kilometer", "Schnellste Abschnitte", "1 km", 'id="track-data"',
                 'data-series="ele"', 'data-series="speed"', "Höchster Punkt"):
        assert text in page, text
    assert 'data-series="hr"' not in page
    home = env.client.get("/").text
    assert "Morgenrunde" in home and "Bestwerte" in home and "Längster Lauf" in home

    original = env.client.get(f"/activities/{activity_id}/original")
    assert original.content == GPX
    env.client.cookies.clear()
    assert env.client.get(f"/activities/{activity_id}/original", follow_redirects=False).status_code == 303


def test_fit_upload_shows_heart_rate(env):
    env.login()
    activity_id = env.upload(fit_bytes("RUNNING", "GENERIC"), "lauf.fit")
    page = env.client.get(f"/activities/{activity_id}").text
    assert "Joggen" in page and 'data-series="hr"' in page and "Max. Puls" in page
    assert env.ctx.original_path(activity_id).suffix == ".fit"


def test_bad_upload_keeps_form_values(env):
    env.login()
    r = env.client.post("/upload", files={"file": ("x.gpx", b"quatsch")}, data={"title": "Mein Titel"})
    assert r.status_code == 400 and "weder eine gültige GPX" in r.text and 'value="Mein Titel"' in r.text


def test_workout_flow(env):
    c = env.client
    env.follow()
    env.login()
    first = env.workout(started="2026-10-13T18:30", exercise_sets=["7 7 6 5", "3x10"])
    second = env.workout()
    note = env.sent[-1][1]["object"]
    assert "Klimmzüge: 8 · 8 · 7 · 6 Wdh." in note["content"]
    assert "Dips (+5 kg): 12 · 12 · 12 Wdh." in note["content"] and "attachment" not in note

    page = c.get(f"/activities/{second}").text
    assert "Zuletzt · 13. Okt" in page and "7 · 7 · 6 · 5" in page and "+4" in page and "Bestwert" in page
    assert "Zum ersten Mal eingetragen" in c.get(f"/activities/{first}").text

    bad = c.post("/workout", data={"exercise_name": ["Klimmzüge"], "exercise_sets": ["viele"]})
    assert bad.status_code == 400 and "verstehe ich nicht" in bad.text and 'value="Klimmzüge"' in bad.text

    template = c.get(f"/upload?type=workout&source={second}").text
    assert 'value="8 8 7 6"' in template and 'value="5"' in template and "Als Vorlage nehmen" in template

    r = c.post(f"/activities/{second}/edit", data={
        "title": "Pull Day", "started": "2026-10-20T18:30", "duration_min": "50",
        "exercise_name": ["Klimmzüge"], "exercise_sets": ["9 8 8"], "exercise_unit": ["reps"],
        "exercise_weight": [""]}, follow_redirects=False)
    assert r.status_code == 303 and "Klimmzüge: 9 · 8 · 8 Wdh." in env.sent[-1][1]["object"]["content"]
    assert "50:00" in c.get(f"/activities/{second}").text


def test_pagination(env):
    env.login()
    for i in range(21):
        env.workout(title=f"Einheit {i:02d}", started=f"2026-09-{i + 1:02d}T18:00")
    card = 'class="act-title">Einheit {:02d}<'.format
    first = env.client.get("/").text
    assert card(20) in first and card(0) not in first and "Ältere" in first
    second = env.client.get("/?page=2").text
    assert card(0) in second and "Neuere" in second and "Ältere" not in second


def test_parse_sets_and_workouts():
    assert parse_sets("8 8 7 6") == [8, 8, 7, 6]
    assert parse_sets("4x8") == [8, 8, 8, 8] and parse_sets("10, 9;8") == [10, 9, 8]
    for bad in ("acht", "99x5", "1 2 x"):
        with pytest.raises(WorkoutError):
            parse_sets(bad)
    w = build_workout(["Klimmzüge", "", "L-Sit"], ["4x8", "", "20 20"], ["reps", "reps", "sec"], ["10", "", ""])
    assert [e["name"] for e in w] == ["Klimmzüge", "L-Sit"] and w[0]["weight_kg"] == 10
    assert summary(w) == {"exercises": 2, "sets": 6, "reps": 32, "hold_s": 40}
    with pytest.raises(WorkoutError):
        build_workout(["", ""], ["", ""], [], [])


def test_old_database_is_migrated_and_reanalyzed(tmp_path):
    """Eine Datenbank der ersten Version wird beim Start umgestellt und neu ausgewertet."""
    conn = sqlite3.connect(tmp_path / "wegpunkt.db")   # so hieß die Datei vor der Umbenennung
    conn.executescript("""CREATE TABLE activities (id TEXT PRIMARY KEY, title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', sport TEXT NOT NULL, started_at TEXT,
        distance_m REAL NOT NULL, duration_s REAL, moving_s REAL, elevation_gain_m REAL,
        track_json TEXT NOT NULL, has_image INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO activities VALUES ('aaaa','A','', 'ride', NULL, 1000, 60, 60, 0, '[[52.5,13.4,40]]', 0, '2026-01-01T00:00:00Z');
        INSERT INTO activities VALUES ('bbbb','B','', 'hike', NULL, 1000, 60, 60, 0, '[]', 0, '2026-01-01T00:00:00Z');""")
    conn.commit()
    conn.close()
    (tmp_path / "gpx").mkdir()
    (tmp_path / "gpx" / "aaaa.gpx").write_bytes(GPX)

    app = create_app(make_settings(tmp_path))
    db = app.state.ctx.db
    a, b = db.get_activity("aaaa"), db.get_activity("bbbb")
    assert (tmp_path / "arriba.db").exists() and not (tmp_path / "wegpunkt.db").exists()
    assert a["sport"] == "gravel" and b["sport"] == "run" and a["visibility"] == "public"
    assert a["extra"]["version"] == tracks.ANALYSIS_VERSION and 3800 < a["distance_m"] < 4000
    assert len(a["track"][0]) == 5 and a["has_image"] and b["extra"] == {}
    page = TestClient(app).get("/activities/aaaa")
    assert page.status_code == 200 and TestClient(app).get("/activities/bbbb").status_code == 200


def test_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# Kommentar\nARRIBA_DOMAIN=sport.example\nexport ARRIBA_USERNAME="phil"\n'
                        "ARRIBA_PASSWORD='ge=heim'\nARRIBA_DATA_DIR=" + str(tmp_path / "d") + "\n")
    assert read_env_file(env_file)["ARRIBA_PASSWORD"] == "ge=heim"
    for key in ("ARRIBA_DOMAIN", "ARRIBA_USERNAME", "ARRIBA_PASSWORD", "ARRIBA_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARRIBA_USERNAME", "vorrang")
    s = load_settings(env_file)
    assert (s.domain, s.username, s.password, s.https) == ("sport.example", "vorrang", "ge=heim", True)
    assert s.handle == "@vorrang@sport.example"


def test_settings_with_the_old_prefix_still_work(tmp_path, monkeypatch):
    for key in ("ARRIBA_DOMAIN", "ARRIBA_USERNAME", "ARRIBA_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARRIBA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEGPUNKT_DOMAIN", "alt.example")
    monkeypatch.setenv("WEGPUNKT_PASSWORD", "alt")
    monkeypatch.setenv("WEGPUNKT_USERNAME", "alt")
    monkeypatch.setenv("ARRIBA_USERNAME", "neu")
    s = load_settings(tmp_path / "gibtsnicht.env")
    assert (s.domain, s.password, s.username) == ("alt.example", "alt", "neu")


def test_default_avatar_follows_the_logo_but_keeps_custom_pictures(tmp_path):
    ctx = create_app(make_settings(tmp_path)).state.ctx
    ctx.avatar_path.write_bytes(b"altes Standardbild")
    ctx.db.set_profile({"avatar_design": "alt"})
    assert create_app(make_settings(tmp_path)).state.ctx.avatar_path.read_bytes()[:4] == b"\x89PNG"
    ctx.avatar_path.write_bytes(b"eigenes Bild")
    ctx.db.set_profile({"avatar_design": "alt", "custom_avatar": "1"})
    assert create_app(make_settings(tmp_path)).state.ctx.avatar_path.read_bytes() == b"eigenes Bild"


def _activity(i, sport, started, **extra):
    base = {"id": f"a{i}", "title": f"T{i}", "sport": sport, "kind": "gps", "started_at": started,
            "created_at": started, "distance_m": 0.0, "duration_s": 3600.0, "moving_s": None,
            "elevation_gain_m": None, "elevation_loss_m": None, "workout": None, "extra": {}}
    return {**base, **extra}


def test_statistics():
    from datetime import datetime
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 21, 12, 0, tzinfo=tz)   # ein Mittwoch
    pull = lambda sets: [{"name": "Klimmzüge", "sets": sets, "unit": "reps", "weight_kg": None}]
    items = [
        _activity(1, "gravel", "2026-10-19T08:00:00Z", distance_m=80_000.0, elevation_gain_m=900.0),
        _activity(2, "run", "2026-10-13T08:00:00Z", distance_m=10_500.0, extra={"efforts": {"5 km": 1500.0, "10 km": 3100.0}}),
        _activity(3, "run", "2026-10-20T22:30:00Z", distance_m=5_200.0, duration_s=1800.0, extra={"efforts": {"5 km": 1440.0}}),
        _activity(4, "calisthenics", "2026-10-06T17:00:00Z", kind="workout", workout=pull([7, 6, 5])),
        _activity(5, "calisthenics", "2026-10-20T17:00:00Z", kind="workout", workout=pull([9, 8, 6])),
        _activity(6, "enduro", "2025-06-01T08:00:00Z", elevation_loss_m=1200.0),
    ]
    week = stats.this_week(items, tz, now)
    assert week["count"] == 3 and week["reps"] == 23 and week["previous_seconds"] == 3600

    chart = stats.weekly_chart(items, tz, now=now)
    assert len(chart["bars"]) == 12 and chart["bars"][-1]["current"] and not chart["empty"]
    assert {s["sport"] for s in chart["bars"][-1]["segments"]} == {"gravel", "run", "calisthenics"}

    cal = stats.calendar(items, tz, now=now)
    assert cal["active_days"] == 5                    # der Lauf um 22:30 UTC zählt zum 21. Oktober
    assert sum(1 for c in cal["cells"] if c["sport"]) == 5 and len(cal["cells"]) == 52 * 7 + 3

    groups = stats.records(items)
    assert [g["sport"] for g in groups] == ["gravel", "enduro", "run", "calisthenics"]
    records = {(r["sport"], r["label"]): r for g in groups for r in g["items"]}
    assert records[("run", "5 km")]["value"] == "24:00" and records[("run", "5 km")]["id"] == "a3"
    assert records[("gravel", "Längste Tour")]["value"] == "80,00"
    assert records[("enduro", "Längste Abfahrt")]["value"] == "1.200"
    assert records[("calisthenics", "Klimmzüge")]["value"] == "9"

    history = stats.exercise_history(items, items[4], tz)["Klimmzüge"]
    assert history["record"] and history["last"]["delta"] == 5 and history["totals"] == [18, 23]
    assert stats.exercise_history(items, items[3], tz)["Klimmzüge"]["first"]
