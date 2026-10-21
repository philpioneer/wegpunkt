from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from wegpunkt.app import create_app
from wegpunkt.config import Settings
from wegpunkt.httpsig import SignatureError, public_key_pem, sign_request, verify_request
from wegpunkt.tracks import apply_privacy_zone, haversine_m, parse_gpx

GPX = (Path(__file__).parent / "data" / "run.gpx").read_bytes()
AP = "application/activity+json"
REMOTE = "https://mastodon.example/users/alice"


@pytest.fixture
def remote_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def env(tmp_path, remote_key):
    settings = Settings(
        domain="testserver", username="philipp", display_name="Philipp",
        password="geheim", secret_key="x" * 32, data_dir=tmp_path, https=False,
    )
    app = create_app(settings)
    fed = app.state.federation
    delivered: list[tuple[str, dict]] = []
    remote_actor = {
        "id": REMOTE, "type": "Person", "preferredUsername": "alice", "name": "Alice",
        "inbox": f"{REMOTE}/inbox", "endpoints": {"sharedInbox": "https://mastodon.example/inbox"},
        "url": "https://mastodon.example/@alice",
        "publicKey": {"id": f"{REMOTE}#main-key", "owner": REMOTE,
                      "publicKeyPem": public_key_pem(remote_key)},
    }

    async def fake_fetch(url):
        assert url == REMOTE
        return remote_actor

    async def fake_deliver(inbox, activity):
        delivered.append((inbox, activity))
        return True

    fed.fetch = fake_fetch
    fed.deliver = fake_deliver
    return TestClient(app), delivered


def post_inbox(client, key, activity, path="/users/philipp/inbox"):
    import json
    body = json.dumps(activity).encode()
    headers = sign_request("POST", f"http://testserver{path}", key, f"{REMOTE}#main-key", body)
    headers["Content-Type"] = AP
    return client.post(path, content=body, headers=headers)


def reaction_count(page, label):
    import re
    m = re.search(rf'title="{label}">.*?</svg>\s*(\d+)', page, re.S)
    return int(m.group(1))


def login_and_upload(client):
    client.post("/login", data={"password": "geheim"})
    r = client.post("/upload", files={"file": ("run.gpx", GPX)}, data={"sport": "auto"},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


# GPX ----------------------------------------------------------------------

def test_parse_gpx():
    t = parse_gpx(GPX)
    assert t.name == "Morgenrunde"
    assert t.sport_hint == "run"
    assert 3800 < t.distance_m < 4000
    assert t.duration_s == 1170
    assert t.elevation_gain_m and t.elevation_gain_m > 40


def test_privacy_zone_hides_start_and_end():
    pts = parse_gpx(GPX).points
    visible = apply_privacy_zone(pts, 200)
    assert haversine_m(visible[0], pts[0]) >= 200
    assert haversine_m(visible[-1], pts[-1]) >= 200


# Signaturen ---------------------------------------------------------------

def test_signature_roundtrip(remote_key):
    body = b'{"a": 1}'
    h = sign_request("POST", "https://x.example/inbox", remote_key, "k", body)
    pem = public_key_pem(remote_key)
    assert verify_request("POST", "/inbox", h, body, pem) == "k"
    with pytest.raises(SignatureError):
        verify_request("POST", "/inbox", h, b'{"a": 2}', pem)
    with pytest.raises(SignatureError):
        verify_request("POST", "/other", h, body, pem)


# Discovery ----------------------------------------------------------------

def test_webfinger_and_actor(env):
    client, _ = env
    r = client.get("/.well-known/webfinger", params={"resource": "acct:philipp@testserver"})
    assert r.status_code == 200
    href = r.json()["links"][0]["href"]
    assert href == "http://testserver/users/philipp"

    actor = client.get("/users/philipp", headers={"Accept": AP}).json()
    assert actor["type"] == "Person"
    assert "BEGIN PUBLIC KEY" in actor["publicKey"]["publicKeyPem"]
    assert client.get("/.well-known/webfinger", params={"resource": "acct:x@y"}).status_code == 404


# Ablauf -------------------------------------------------------------------

def test_upload_requires_login(env):
    client, _ = env
    r = client.post("/upload", files={"file": ("run.gpx", GPX)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_follow_upload_and_reactions(env, remote_key):
    client, delivered = env

    follow = {"id": f"{REMOTE}#follow/1", "type": "Follow", "actor": REMOTE,
              "object": "http://testserver/users/philipp"}
    assert post_inbox(client, remote_key, follow).status_code == 202
    assert delivered[-1][0] == f"{REMOTE}/inbox"
    assert delivered[-1][1]["type"] == "Accept"

    activity_id = login_and_upload(client)
    inbox, create = delivered[-1]
    assert inbox == "https://mastodon.example/inbox"  # geteilte Inbox
    assert create["type"] == "Create" and create["object"]["type"] == "Note"
    assert "3,92 km" in create["object"]["content"]
    assert create["object"]["attachment"][0]["mediaType"] == "image/png"

    note_url = f"http://testserver/activities/{activity_id}"
    note = client.get(f"/activities/{activity_id}", headers={"Accept": AP}).json()
    assert note["id"] == note_url

    like = {"id": f"{REMOTE}#likes/1", "type": "Like", "actor": REMOTE, "object": note_url}
    reply = {"id": f"{REMOTE}#create/2", "type": "Create", "actor": REMOTE, "object": {
        "id": f"{REMOTE}/statuses/2", "type": "Note", "attributedTo": REMOTE,
        "inReplyTo": note_url, "content": "<p>Stark! <script>alert(1)</script></p>"}}
    assert post_inbox(client, remote_key, like, "/inbox").status_code == 202
    assert post_inbox(client, remote_key, reply).status_code == 202

    page = client.get(f"/activities/{activity_id}").text
    assert reaction_count(page, "Likes") == 1 and reaction_count(page, "Antworten") == 1
    assert "Stark!" in page and "<script>alert" not in page

    undo = {"id": f"{REMOTE}#undo/1", "type": "Undo", "actor": REMOTE, "object": like}
    post_inbox(client, remote_key, undo)
    assert reaction_count(client.get(f"/activities/{activity_id}").text, "Likes") == 0

    r = client.post(f"/activities/{activity_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert delivered[-1][1]["type"] == "Delete"
    assert client.get(f"/activities/{activity_id}").status_code == 404


def test_inbox_rejects_bad_signatures(env, remote_key):
    client, _ = env
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    follow = {"id": "x", "type": "Follow", "actor": REMOTE, "object": "http://testserver/users/philipp"}
    assert post_inbox(client, other_key, follow).status_code == 401

    spoofed = {**follow, "actor": "https://evil.example/users/bob"}
    assert post_inbox(client, remote_key, spoofed).status_code == 401

    assert client.post("/inbox", json=follow).status_code == 401


# Workouts, Anmeldung, Umstellung -------------------------------------------

from wegpunkt.workouts import WorkoutError, build_workout, parse_sets, summary


def test_parse_sets():
    assert parse_sets("8 8 7 6") == [8, 8, 7, 6]
    assert parse_sets("4x8") == [8, 8, 8, 8]
    assert parse_sets("10, 9;8") == [10, 9, 8]
    with pytest.raises(WorkoutError):
        parse_sets("acht")


def test_build_workout_skips_empty_rows():
    w = build_workout(["Klimmzüge", "", "L-Sit"], ["4x8", "", "20 20"], ["reps", "reps", "sec"], ["10", "", ""])
    assert [e["name"] for e in w] == ["Klimmzüge", "L-Sit"]
    assert w[0]["weight_kg"] == 10
    assert summary(w) == {"exercises": 2, "sets": 6, "reps": 32, "hold_s": 40}
    with pytest.raises(WorkoutError):
        build_workout(["", ""], ["", ""], [], [])


def test_workout_is_published(env, remote_key):
    client, delivered = env
    follow = {"id": f"{REMOTE}#follow/1", "type": "Follow", "actor": REMOTE,
              "object": "http://testserver/users/philipp"}
    post_inbox(client, remote_key, follow)
    client.post("/login", data={"password": "geheim"})
    r = client.post("/workout", data={
        "title": "Pull Day", "started": "2026-10-20T18:30", "duration_min": "45",
        "exercise_name": ["Klimmzüge", "Dips"], "exercise_sets": ["8 8 7 6", "3x12"],
        "exercise_unit": ["reps", "reps"], "exercise_weight": ["", "+5"],
    }, follow_redirects=False)
    assert r.status_code == 303
    note = delivered[-1][1]["object"]
    assert "Klimmzüge: 8 · 8 · 7 · 6 Wdh." in note["content"]
    assert "Dips (+5 kg): 12 · 12 · 12 Wdh." in note["content"]
    assert "#Calisthenics" == note["tag"][0]["name"]
    page = client.get(r.headers["location"]).text
    assert "Pull Day" in page and "65" in page  # 29 + 36 Wiederholungen

    bad = client.post("/workout", data={"exercise_name": ["Klimmzüge"], "exercise_sets": ["viele"]})
    assert "verstehe ich nicht" in bad.text and 'value="Klimmzüge"' in bad.text


def test_login_is_throttled(env):
    client, _ = env
    for _ in range(5):
        assert "Falsches Passwort" in client.post("/login", data={"password": "nein"}).text
    blocked = client.post("/login", data={"password": "geheim"})
    assert "Zu viele Fehlversuche" in blocked.text


def test_old_sports_are_migrated(tmp_path):
    import sqlite3
    from wegpunkt.db import Database
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""CREATE TABLE activities (id TEXT PRIMARY KEY, title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', sport TEXT NOT NULL, started_at TEXT,
        distance_m REAL NOT NULL, duration_s REAL, moving_s REAL, elevation_gain_m REAL,
        track_json TEXT NOT NULL, has_image INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO activities VALUES ('a','A','', 'ride', NULL, 1000, 60, 60, 0, '[]', 0, '2026-01-01T00:00:00Z');
        INSERT INTO activities VALUES ('b','B','', 'hike', NULL, 1000, 60, 60, 0, '[]', 0, '2026-01-01T00:00:00Z');""")
    conn.commit(); conn.close()
    db = Database(db_path)
    assert db.get_activity("a")["sport"] == "gravel"
    assert db.get_activity("b")["sport"] == "run"
    assert db.get_activity("a")["kind"] == "gps"
