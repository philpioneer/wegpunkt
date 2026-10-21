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
    assert "♥ 1 Likes" in page and "💬 1 Antworten" in page
    assert "Stark!" in page and "<script>alert" not in page

    undo = {"id": f"{REMOTE}#undo/1", "type": "Undo", "actor": REMOTE, "object": like}
    post_inbox(client, remote_key, undo)
    assert "♥ 0 Likes" in client.get(f"/activities/{activity_id}").text

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
