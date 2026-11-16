import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from wegpunkt.app import create_app
from wegpunkt.config import Settings
from wegpunkt.httpsig import public_key_pem, sign_request

GPX = (Path(__file__).parent / "data" / "run.gpx").read_bytes()
AP = "application/activity+json"
REMOTE = "https://mastodon.example/users/alice"
ME = "http://testserver/users/philipp"


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        domain="testserver", username="philipp", display_name="Philipp", password="geheim",
        secret_key="x" * 32, data_dir=tmp_path, https=False, map_previews=False,
    )
    return Settings(**{**values, **overrides})


@pytest.fixture(scope="session")
def remote_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Env:
    """Eine Test-Instanz samt nachgestelltem Mastodon-Server."""

    def __init__(self, tmp_path, remote_key):
        self.app = create_app(make_settings(tmp_path))
        self.ctx = self.app.state.ctx
        self.fed = self.ctx.fed
        self.client = TestClient(self.app)
        self.key = remote_key
        self.sent: list[tuple[str, dict]] = []
        self.status = 202  # Antwort des fremden Servers auf Zustellungen
        self.actor = {
            "id": REMOTE, "type": "Person", "preferredUsername": "alice", "name": "Alice",
            "inbox": f"{REMOTE}/inbox", "endpoints": {"sharedInbox": "https://mastodon.example/inbox"},
            "url": "https://mastodon.example/@alice",
            "publicKey": {"id": f"{REMOTE}#main-key", "owner": REMOTE,
                          "publicKeyPem": public_key_pem(remote_key)},
        }

        async def fake_fetch(url):
            assert url == REMOTE
            return self.actor

        async def fake_post(inbox, body):
            if self.status < 300:
                self.sent.append((inbox, json.loads(body)))
            return self.status

        self.fed.fetch = fake_fetch
        self.fed.post = fake_post

    def inbox(self, activity, path="/users/philipp/inbox", key=None):
        body = json.dumps(activity).encode()
        headers = sign_request("POST", f"http://testserver{path}", key or self.key, f"{REMOTE}#main-key", body)
        headers["Content-Type"] = AP
        return self.client.post(path, content=body, headers=headers)

    def follow(self):
        r = self.inbox({"id": f"{REMOTE}#follow/1", "type": "Follow", "actor": REMOTE, "object": ME})
        assert r.status_code == 202
        return r

    def login(self):
        r = self.client.post("/login", data={"password": "geheim"}, follow_redirects=False)
        assert r.status_code == 303

    def upload(self, data=GPX, name="run.gpx", **fields):
        r = self.client.post("/upload", files={"file": (name, data)}, data={"sport": "auto", **fields},
                             follow_redirects=False)
        assert r.status_code == 303, r.text
        return r.headers["location"].rsplit("/", 1)[1]

    def workout(self, **fields):
        data = {
            "title": "Pull Day", "started": "2026-10-20T18:30", "duration_min": "45",
            "exercise_name": ["Klimmzüge", "Dips"], "exercise_sets": ["8 8 7 6", "3x12"],
            "exercise_unit": ["reps", "reps"], "exercise_weight": ["", "+5"], **fields,
        }
        r = self.client.post("/workout", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text
        return r.headers["location"].rsplit("/", 1)[1]

    def types(self):
        return [a["type"] for _, a in self.sent]


@pytest.fixture
def env(tmp_path, remote_key):
    return Env(tmp_path, remote_key)


def fit_bytes(sport="CYCLING", sub_sport="MOUNTAIN", points=60) -> bytes:
    import datetime as dt

    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer, Sport, SubSport

    t0 = dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.timezone.utc).timestamp() * 1000
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    head = FileIdMessage()
    head.type, head.manufacturer, head.product = FileType.ACTIVITY, Manufacturer.DEVELOPMENT.value, 0
    head.time_created, head.serial_number = round(t0), 1
    b.add(head)
    for i in range(points):
        r = RecordMessage()
        r.position_lat, r.position_long = 47.6 + i * 0.0005, 11.0
        r.altitude, r.heart_rate, r.timestamp = 700 + i * 2, 120 + i % 30, round(t0 + i * 10000)
        b.add(r)
    s = SessionMessage()
    s.sport, s.sub_sport = getattr(Sport, sport), getattr(SubSport, sub_sport)
    s.timestamp, s.start_time = round(t0 + points * 10000), round(t0)
    b.add(s)
    return b.build().to_bytes()
