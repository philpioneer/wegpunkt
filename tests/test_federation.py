import asyncio
import re

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from wegpunkt.app import create_app
from wegpunkt.federation import RETRY_DELAYS_S, FetchError, html_to_text
from wegpunkt.httpsig import SignatureError, public_key_pem, sign_request, verify_request

from .conftest import AP, ME, REMOTE, fit_bytes, make_settings


def reactions(page: str, label: str) -> int:
    return int(re.search(rf'title="{label}">.*?</svg>\s*(\d+)', page, re.S).group(1))


def test_signature_roundtrip(remote_key):
    body = b'{"a": 1}'
    h = sign_request("POST", "https://x.example/inbox", remote_key, "k", body)
    pem = public_key_pem(remote_key)
    assert verify_request("POST", "/inbox", h, body, pem) == "k"
    for bad in (("POST", "/inbox", b'{"a": 2}'), ("POST", "/other", body)):
        with pytest.raises(SignatureError):
            verify_request(bad[0], bad[1], h, bad[2], pem)


def test_discovery(env):
    c = env.client
    r = c.get("/.well-known/webfinger", params={"resource": "acct:philipp@testserver"})
    assert r.json()["links"][0]["href"] == ME
    assert c.get("/.well-known/webfinger", params={"resource": "acct:x@y"}).status_code == 404

    actor = c.get("/users/philipp", headers={"Accept": AP}).json()
    assert actor["type"] == "Person" and actor["name"] == "Philipp"
    assert "BEGIN PUBLIC KEY" in actor["publicKey"]["publicKeyPem"]
    assert c.get(actor["icon"]["url"]).headers["content-type"] == "image/png"
    assert c.get("/users/philipp", follow_redirects=False).status_code == 302
    assert c.get("/users/niemand", headers={"Accept": AP}).status_code == 404
    assert c.get("/nodeinfo/2.0").json()["openRegistrations"] is False


def test_follow_publish_react_delete(env):
    c = env.client
    env.follow()
    assert env.sent[-1][0] == f"{REMOTE}/inbox" and env.sent[-1][1]["type"] == "Accept"

    env.login()
    activity_id = env.upload()
    inbox, create = env.sent[-1]
    assert inbox == "https://mastodon.example/inbox"  # geteilte Inbox
    note = create["object"]
    assert create["type"] == "Create" and note["type"] == "Note"
    assert "3,92 km" in note["content"] and "#Joggen" == note["tag"][0]["name"]
    assert c.get(note["attachment"][0]["url"]).headers["content-type"] == "image/png"

    url = f"http://testserver/activities/{activity_id}"
    assert c.get(url, headers={"Accept": AP}).json()["id"] == url

    like = {"id": f"{REMOTE}#likes/1", "type": "Like", "actor": REMOTE, "object": url}
    reply = {"id": f"{REMOTE}#create/2", "type": "Create", "actor": REMOTE, "object": {
        "id": f"{REMOTE}/statuses/2", "type": "Note", "attributedTo": REMOTE, "inReplyTo": url,
        "content": "<p>Stark! <script>alert(1)</script></p>"}}
    assert env.inbox(like, "/inbox").status_code == 202
    assert env.inbox({**like, "id": f"{REMOTE}#likes/2"}).status_code == 202  # zählt nicht doppelt
    assert env.inbox(reply).status_code == 202

    page = c.get(url).text
    assert reactions(page, "Likes") == 1 and reactions(page, "Antworten") == 1
    assert "Stark!" in page and "alert(1)" not in page

    edited = {"id": f"{REMOTE}#update/3", "type": "Update", "actor": REMOTE,
              "object": {**reply["object"], "content": "<p>Sehr stark!</p>"}}
    env.inbox(edited)
    assert "Sehr stark!" in c.get(url).text

    # Undo mit anderer ID als das ursprüngliche Like: das Ziel entscheidet
    env.inbox({"id": f"{REMOTE}#undo/1", "type": "Undo", "actor": REMOTE,
               "object": {**like, "id": f"{REMOTE}#likes/unbekannt"}})
    assert reactions(c.get(url).text, "Likes") == 0

    env.inbox({"id": f"{REMOTE}#delete/1", "type": "Delete", "actor": REMOTE,
               "object": {"id": f"{REMOTE}/statuses/2", "type": "Tombstone"}})
    assert reactions(c.get(url).text, "Antworten") == 0

    assert c.post(f"/activities/{activity_id}/edit", data={"title": "x"}, follow_redirects=False).status_code == 303
    assert c.post(f"/activities/{activity_id}/delete", follow_redirects=False).status_code == 303
    assert env.types()[-2:] == ["Update", "Delete"]
    assert c.get(url).status_code == 404

    env.inbox({"id": f"{REMOTE}#undo/2", "type": "Undo", "actor": REMOTE,
               "object": {"id": f"{REMOTE}#follow/1", "type": "Follow", "actor": REMOTE, "object": ME}})
    assert env.ctx.db.count_followers() == 0


def test_inbox_rejects_bad_requests(env):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    follow = {"id": "x", "type": "Follow", "actor": REMOTE, "object": ME}
    assert env.inbox(follow, key=other_key).status_code == 401
    assert env.inbox({**follow, "actor": "https://evil.example/users/bob"}).status_code == 401
    assert env.client.post("/inbox", json=follow).status_code == 401
    assert env.client.post("/inbox", content=b"kein json", headers={"Signature": 'keyId="a"'}).status_code == 400
    assert env.client.post("/inbox", content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert env.ctx.db.count_followers() == 0


def test_reactions_to_unknown_or_private_activities_are_ignored(env):
    env.login()
    private_id = env.upload(private="1")
    for target in (f"http://testserver/activities/{private_id}", "http://testserver/activities/gibtsnicht",
                   "https://woanders.example/1"):
        env.inbox({"id": f"{REMOTE}#l/{target}", "type": "Like", "actor": REMOTE, "object": target})
    assert env.ctx.db.list_interactions(private_id) == []


def test_failed_deliveries_are_retried(env):
    db = env.ctx.db
    env.follow()
    env.login()
    env.status = 503
    env.upload()
    assert db.count_deliveries() == 1 and db.due_deliveries() == []     # wartet auf den nächsten Versuch
    with db._conn() as conn:
        row = conn.execute("SELECT attempts, last_error FROM deliveries").fetchone()
        assert tuple(row) == (1, "HTTP 503")
        conn.execute("UPDATE deliveries SET next_at = 0")

    env.status = 202
    assert asyncio.run(env.fed.process_queue()) == 1
    assert db.count_deliveries() == 0 and env.types() == ["Accept", "Create"]


def test_deliveries_give_up(env):
    db = env.ctx.db
    env.follow()
    env.status = 410                                                    # Inbox gibt es nicht mehr
    env.fed.broadcast({"type": "Create"})
    asyncio.run(env.fed.process_queue())
    assert db.count_deliveries() == 0

    env.status = 500
    env.fed.broadcast({"type": "Create"})
    for _ in range(len(RETRY_DELAYS_S)):
        asyncio.run(env.fed.process_queue())
        with db._conn() as conn:
            conn.execute("UPDATE deliveries SET next_at = 0")
    assert db.count_deliveries() == 1
    asyncio.run(env.fed.process_queue())
    assert db.count_deliveries() == 0                                   # nach dem letzten Versuch verworfen


def test_edit_and_visibility_changes_are_federated(env):
    c = env.client
    env.follow()
    env.login()
    activity_id = env.upload(fit_bytes(), "tour.fit")
    url = f"/activities/{activity_id}"
    assert "Enduro MTB" in c.get(url).text and env.sent[-1][1]["object"]["tag"][0]["name"] == "#EnduroMTB"

    c.post(f"{url}/edit", data={"title": "Hausrunde", "description": "Schön", "sport": "gravel"})
    update = env.sent[-1][1]
    assert update["type"] == "Update" and "Hausrunde" in update["object"]["content"]
    assert update["object"]["tag"][0]["name"] == "#Gravel" and "updated" in update["object"]

    c.post(f"{url}/edit", data={"title": "Hausrunde", "private": "1"})
    assert env.sent[-1][1]["type"] == "Delete"
    c.cookies.clear()
    assert c.get(url).status_code == 404
    assert c.get(url, headers={"Accept": AP}).status_code == 404
    assert c.get(f"/media/{activity_id}.png").status_code == 404
    assert "Hausrunde" not in c.get("/").text
    assert c.get("/users/philipp/outbox?page=1", headers={"Accept": AP}).json()["orderedItems"] == []

    env.login()
    assert "Hausrunde" in c.get("/").text
    c.post(f"{url}/edit", data={"title": "Hausrunde"})
    assert env.sent[-1][1]["type"] == "Create"
    assert c.post(f"{url}/edit", data={"title": " "}).status_code == 400


def test_private_upload_is_not_federated(env):
    env.follow()
    env.login()
    env.upload(private="1")
    env.workout(private="1")
    assert env.types() == ["Accept"] and env.ctx.db.count_deliveries() == 0


def test_profile_changes_are_federated(env):
    c = env.client
    env.follow()
    assert c.get("/settings", follow_redirects=False).status_code == 303
    env.login()

    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (900, 500), (200, 30, 30)).save(buf, "JPEG")
    r = c.post("/settings", data={"display_name": "Philipp H.", "summary": "Zeile 1\nZeile 2 <b>"},
               files={"avatar": ("ich.jpg", buf.getvalue())}, follow_redirects=False)
    assert r.status_code == 303
    actor = env.sent[-1][1]["object"]
    assert env.sent[-1][1]["type"] == "Update" and actor["type"] == "Person"
    assert actor["name"] == "Philipp H." and actor["summary"] == "<p>Zeile 1<br>Zeile 2 &lt;b&gt;</p>"
    avatar = Image.open(io.BytesIO(c.get("/media/avatar.png").content))
    assert avatar.size == (400, 400) and avatar.getpixel((200, 200))[0] > 150
    assert "Philipp H." in c.get("/").text

    assert c.post("/settings", data={"display_name": ""}).status_code == 400
    assert c.post("/settings", data={"display_name": "P"}, files={"avatar": ("x.png", b"kein bild")}).status_code == 400


def test_outbox_is_paged(env):
    env.login()
    for i in range(3):
        env.workout(title=f"W{i}", started=f"2026-10-0{i + 1}T18:00")
    c = env.client
    top = c.get("/users/philipp/outbox", headers={"Accept": AP}).json()
    assert top["totalItems"] == 3 and top["first"].endswith("?page=1")
    page = c.get("/users/philipp/outbox?page=1", headers={"Accept": AP}).json()
    assert [i["type"] for i in page["orderedItems"]] == ["Create"] * 3 and "next" not in page
    assert "W2" in page["orderedItems"][0]["object"]["content"]


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/inbox", "https://10.0.0.5/x", "https://[::1]/x", "https://169.254.169.254/latest",
    "http://93.184.216.34/x", "ftp://example.org/x", "https:///x",
])
def test_internal_addresses_are_blocked(tmp_path, url):
    fed = create_app(make_settings(tmp_path, https=True, domain="sport.example")).state.ctx.fed
    with pytest.raises(FetchError):
        asyncio.run(fed.guard(url))


def test_public_addresses_are_allowed(tmp_path):
    fed = create_app(make_settings(tmp_path, https=True, domain="sport.example")).state.ctx.fed
    asyncio.run(fed.guard("https://93.184.216.34/inbox"))


def test_foreign_html_becomes_plain_text():
    assert html_to_text("<p>Hallo <b>du</b></p><p>Zeile<br>2</p><style>x{}</style>") == "Hallo du\nZeile\n2"
