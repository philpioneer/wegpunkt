"""Einstellungen aus Umgebungsvariablen oder einer .env-Datei."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

OLD_PREFIX = "WEGPUNKT_"


@dataclass(frozen=True)
class Settings:
    domain: str
    username: str
    display_name: str
    password: str
    secret_key: str
    data_dir: Path
    https: bool = True
    summary: str = ""
    timezone: str = "Europe/Berlin"
    # Punkte in diesem Radius um Start und Ziel werden nicht öffentlich gezeigt.
    privacy_radius_m: float = 200.0
    # Vorschaubilder für Fediverse-Beiträge mit Kartenhintergrund von OpenStreetMap
    map_previews: bool = True

    @property
    def base_url(self) -> str:
        return f"{'https' if self.https else 'http'}://{self.domain}"

    @property
    def actor_url(self) -> str:
        return f"{self.base_url}/users/{self.username}"

    @property
    def key_id(self) -> str:
        return f"{self.actor_url}#main-key"

    @property
    def handle(self) -> str:
        return f"@{self.username}@{self.domain}"


def read_env_file(path: Path) -> dict[str, str]:
    """Liest einfache KEY=WERT-Zeilen. Anführungszeichen um den Wert sind erlaubt."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().removeprefix("export ").strip()] = value
    return values


def merged_env(env_file: Path | None = None) -> dict[str, str]:
    """Umgebung plus .env-Datei. Echte Umgebungsvariablen haben Vorrang.

    Einstellungen mit dem früheren Präfix WEGPUNKT_ gelten weiter, solange es
    keine gleichnamige mit ARRIBA_ gibt.
    """
    env = {**read_env_file(env_file or Path(".env")), **os.environ}
    for key, value in list(env.items()):
        if key.startswith(OLD_PREFIX):
            env.setdefault("ARRIBA_" + key[len(OLD_PREFIX):], value)
    return env


def _persistent_secret(data_dir: Path) -> str:
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(32)
    path.write_text(value)
    path.chmod(0o600)
    return value


def load_settings(env_file: Path | None = None) -> Settings:
    env = merged_env(env_file)
    domain = env.get("ARRIBA_DOMAIN", "localhost:8000")
    data_dir = Path(env.get("ARRIBA_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    local = domain.startswith(("localhost", "127.0.0.1"))
    https = env.get("ARRIBA_HTTPS", "0" if local else "1").lower() not in ("0", "false", "no")

    password = env.get("ARRIBA_PASSWORD", "")
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"ARRIBA_PASSWORD ist nicht gesetzt. Passwort für diesen Start: {password}")

    return Settings(
        domain=domain,
        username=env.get("ARRIBA_USERNAME", "me"),
        display_name=env.get("ARRIBA_DISPLAY_NAME", "Arriba"),
        password=password,
        secret_key=env.get("ARRIBA_SECRET_KEY") or _persistent_secret(data_dir),
        data_dir=data_dir,
        https=https,
        summary=env.get("ARRIBA_SUMMARY", ""),
        timezone=env.get("ARRIBA_TIMEZONE", "Europe/Berlin"),
        privacy_radius_m=float(env.get("ARRIBA_PRIVACY_RADIUS_M", "200")),
        map_previews=env.get("ARRIBA_MAP_PREVIEWS", "1").lower() not in ("0", "false", "no"),
    )
