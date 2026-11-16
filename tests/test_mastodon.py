import json

import httpx
import pytest
from fastapi.testclient import TestClient

from arriba import mastodon
from arriba.app import create_app

from .conftest import AP, GPX, REMOTE, make_settings


class FakeMastodon:
    """Stellt die Mastodon-Schnittstelle nach und merkt sich, was ankommt."""

    def __init__(self):
        self.statuses: dict[str, dict] = {}
        self.media: list[dict] = []
        self.calls: list[str] = []
        self.fail_with: int | None = None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if request.headers.get("authorization") != "Bearer geheimes-token":
            return httpx.Response(401, json={"error": "The access token is invalid"})
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": "kaputt"})
        if path == "/api/v1/accounts/verify_credentials":
            return httpx.Response(200, json={"id": "1", "acct": "philipp"})
        if path == "/api/v2/media":
            body = request.read()
            assert b"\x89PNG" in body and b"Karte der Route" in body
            self.media.append({"id": str(100 + len(self.media))})
            return httpx.Response(200, json={"id": self.media[-1]["id"], "url": "https://cdn.example/x.png"})
        if path == "/api/v1/statuses" and method == "POST":
            data = json.loads(request.content)
            status_id = str(1000 + len(self.statuses))
            assert request.headers["idempotency-key"].startswith("arriba-")
            self.statuses[status_id] = data
            return httpx.Response(200, json={"id": status_id, "url": f"https://mastodon.example/@philipp/{status_id}"})
        status_id = path.rsplit("/", 1)[1]
        if method == "PUT":
            self.statuses[status_id] = {**self.statuses[status_id], **json.loads(request.content)}
            return httpx.Response(200, json={"id": status_id, "url": f"https://mastodon.example/@philipp/{status_id}"})
        if method == "DELETE":
            del self.statuses[status_id]
            return httpx.Response(200, json={})
        if method == "GET" and status_id in self.statuses:
            return httpx.Response(200, json={"id": status_id, "favourites_count": 4, "reblogs_count": 1, "replies_count": 2})
        return httpx.Response(404, json={"error": "Record not found"})


@pytest.fixture
def masto(env):
    fake = FakeMastodon()
    env.ctx.mastodon_transport = fake.transport
    env.login()
    r = env.client.post("/settings/mastodon", data={"url": "mastodon.example/@philipp", "token": "geheimes-token",
                                                    "visibility": "unlisted"}, follow_redirects=False)
    assert r.status_code == 303
    return fake


def test_connect_checks_the_token(env):
    fake = FakeMastodon()
    env.ctx.mastodon_transport = fake.transport
    env.login()
    c = env.client
    bad = c.post("/settings/mastodon", data={"url": "https://mastodon.example", "token": "falsch"})
    assert bad.status_code == 400 and "lehnt das Zugangs-Token ab" in bad.text
    assert c.post("/settings/mastodon", data={"url": "kein server", "token": "x"}).status_code == 400
    assert c.post("/settings/mastodon", data={"url": "https://mastodon.example", "token": ""}).status_code == 400
    assert env.ctx.mastodon_config() is None

    c.post("/settings/mastodon", data={"url": "https://mastodon.example", "token": "geheimes-token"})
    config = env.ctx.mastodon_config()
    assert config["url"] == "https://mastodon.example" and config["acct"] == "@philipp@mastodon.example"
    page = c.get("/settings").text
    assert "Verbunden als @philipp@mastodon.example" in page and "geheimes-token" not in page

    c.post("/settings/mastodon", data={"action": "disconnect"})
    assert env.ctx.mastodon_config() is None


def test_tour_is_posted_edited_and_deleted(env, masto):
    c = env.client
    activity_id = env.upload(mastodon="1", description="Schön kühl heute.")
    (status_id, status), = masto.statuses.items()
    assert status["visibility"] == "unlisted" and status["media_ids"] == ["100"] and status["language"] == "de"
    text = status["status"]
    assert text.startswith("Morgenrunde\n\n🏃 Joggen · 3,92 km")
    assert "Schön kühl heute." in text and text.endswith(f"http://testserver/activities/{activity_id}\n#Joggen")

    page = c.get(f"/activities/{activity_id}").text
    assert f"https://mastodon.example/@philipp/{status_id}" in page and "Beitrag auf Mastodon ansehen" in page
    assert env.ctx.db.get_activity(activity_id)["extra"]["mastodon"]["counts"]["favourites"] == 4  # nachgeladen

    c.post(f"/activities/{activity_id}/edit", data={"title": "Abendrunde", "sport": "run"})
    assert masto.statuses[status_id]["status"].startswith("Abendrunde")
    assert masto.statuses[status_id]["media_ids"] == ["100"] and len(masto.media) == 1   # Bild bleibt

    c.post(f"/activities/{activity_id}/edit", data={"title": "Abendrunde", "sport": "gravel"})
    assert masto.statuses[status_id]["media_ids"] == ["101"]                             # neu gezeichnet
    assert "#Gravel" in masto.statuses[status_id]["status"]

    c.post(f"/activities/{activity_id}/edit", data={"title": "Abendrunde", "sport": "gravel", "private": "1"})
    assert masto.statuses == {} and "mastodon" not in env.ctx.db.get_activity(activity_id)["extra"]

    other = env.upload(mastodon="1")
    assert len(masto.statuses) == 1
    c.post(f"/activities/{other}/delete")
    assert masto.statuses == {}


def test_posting_is_optional_and_can_be_done_later(env, masto):
    c = env.client
    quiet = env.upload()                                   # Schalter aus
    hidden = env.workout(private="1", mastodon="1")        # privat geht nie raus
    assert masto.statuses == {}
    assert "Noch nicht auf Mastodon gepostet" in c.get(f"/activities/{quiet}").text
    assert 'id="mastodon"' not in c.get(f"/activities/{hidden}").text

    r = c.post(f"/activities/{quiet}/mastodon", follow_redirects=False)
    assert r.status_code == 303 and len(masto.statuses) == 1

    workout = env.workout(mastodon="1")
    text = list(masto.statuses.values())[-1]["status"]
    assert "Klimmzüge: 8 · 8 · 7 · 6 Wdh." in text and "#Calisthenics" in text
    assert list(masto.statuses.values())[-1]["media_ids"] == []
    assert workout


def test_failed_post_is_shown_and_can_be_retried(env, masto):
    c = env.client
    masto.fail_with = 503
    activity_id = env.upload(mastodon="1")
    page = c.get(f"/activities/{activity_id}").text
    assert "Mastodon meldet einen Fehler (503)" in page and "Erneut versuchen" in page
    masto.fail_with = None
    c.post(f"/activities/{activity_id}/mastodon")
    page = c.get(f"/activities/{activity_id}").text
    assert len(masto.statuses) == 1 and "Mastodon meldet einen Fehler" not in page


def test_status_text_always_fits():
    base = {"title": "Pull Day", "sport": "calisthenics", "kind": "workout", "moving_s": None,
            "duration_s": 3300, "distance_m": 0, "elevation_gain_m": None, "extra": {}}
    many = [{"name": f"Übung Nummer {i}", "sets": [10, 10, 10, 10], "unit": "reps", "weight_kg": 5} for i in range(30)]
    text = mastodon.compose_status({**base, "workout": many, "description": "x" * 900})
    assert len(text) <= 500 and "… und" in text and text.endswith("#Calisthenics")

    long_note = mastodon.compose_status({**base, "workout": many[:2], "description": "Wort " * 300},
                                        link="https://sport.example.de/activities/0123456789abcdef")
    counted = len(long_note) - len("https://sport.example.de/activities/0123456789abcdef") + 23
    assert counted <= 500 and "…" in long_note and "https://sport.example.de/activities/" in long_note

    short = mastodon.compose_status({**base, "workout": many[:1], "description": ""})
    assert short == "Pull Day\n\n🤸 Calisthenics · 55 min · 4 Sätze · 40 Wdh.\n\nÜbung Nummer 0 (+5 kg): 10 · 10 · 10 · 10 Wdh.\n\n#Calisthenics"


@pytest.mark.parametrize("value,expected", [
    ("mastodon.social", "https://mastodon.social"), ("https://chaos.social/@ich", "https://chaos.social"),
    (" https://MASTODON.social/ ", "https://MASTODON.social"),
])
def test_instance_address_is_tidied(value, expected):
    assert mastodon.normalize_url(value) == expected


@pytest.mark.parametrize("value", ["", "http://mastodon.social", "localhost", "ftp://x.y"])
def test_bad_instance_addresses(value):
    with pytest.raises(mastodon.MastodonError):
        mastodon.normalize_url(value)


def test_private_mode_hides_everything(tmp_path):
    app = create_app(make_settings(tmp_path, private=True))
    c = TestClient(app)
    html = {"Accept": "text/html"}
    for path in ("/", "/upload", "/settings", "/activities/abc"):
        r = c.get(path, headers=html, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    for path in ("/users/philipp", "/.well-known/webfinger?resource=acct:philipp@testserver", "/nodeinfo/2.0",
                 "/users/philipp/outbox", "/media/avatar.png"):
        assert c.get(path, headers={"Accept": AP}).status_code == 404, path
    assert c.post("/inbox", json={"type": "Follow", "actor": REMOTE}).status_code == 404
    assert "Disallow: /" in c.get("/robots.txt").text
    assert c.get("/static/style.css").status_code == 200

    login = c.get("/login")
    assert login.status_code == 200 and login.headers["x-robots-tag"] == "noindex, nofollow"
    c.post("/login", data={"password": "geheim"})
    r = c.post("/upload", files={"file": ("run.gpx", GPX)}, data={"sport": "auto"}, follow_redirects=False)
    page = c.get(r.headers["location"]).text
    assert "Morgenrunde" in page and "Aus dem Fediverse" not in page
    home = c.get("/").text
    assert "Private Instanz" in home and "Folgen im Fediverse" not in home and "Follower" not in home


def test_private_mode_posts_without_link(tmp_path):
    app = create_app(make_settings(tmp_path, private=True))
    ctx = app.state.ctx
    fake = FakeMastodon()
    ctx.mastodon_transport = fake.transport
    c = TestClient(app)
    c.post("/login", data={"password": "geheim"})
    c.post("/settings/mastodon", data={"url": "mastodon.example", "token": "geheimes-token"})
    c.post("/upload", files={"file": ("run.gpx", GPX)}, data={"sport": "auto", "mastodon": "1"})
    (status,) = fake.statuses.values()
    assert "testserver" not in status["status"] and status["status"].endswith("#Joggen")
    assert status["visibility"] == "public" and status["media_ids"] == ["100"]
