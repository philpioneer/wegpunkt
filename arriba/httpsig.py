"""HTTP-Signaturen (draft-cavage), wie Mastodon sie für ActivityPub nutzt."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import re
import time
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

MAX_CLOCK_SKEW_S = 3600


class SignatureError(Exception):
    pass


def load_or_create_key(path: Path) -> rsa.RSAPrivateKey:
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def public_key_pem(key: rsa.RSAPrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def body_digest(body: bytes) -> str:
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()


def sign_request(
    method: str, url: str, key: rsa.RSAPrivateKey, key_id: str, body: bytes | None = None
) -> dict[str, str]:
    """Gibt die Header zurück, die eine ausgehende Anfrage signieren."""
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {"Host": parts.netloc, "Date": email.utils.formatdate(usegmt=True)}
    names = ["(request-target)", "host", "date"]
    if body is not None:
        headers["Digest"] = body_digest(body)
        names.append("digest")
    lines = [f"(request-target): {method.lower()} {target}"]
    lines += [f"{n}: {headers[n.title()]}" for n in names[1:]]
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    headers["Signature"] = (
        f'keyId="{key_id}",algorithm="rsa-sha256",headers="{" ".join(names)}",'
        f'signature="{base64.b64encode(signature).decode()}"'
    )
    return headers


def parse_signature_header(value: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', value))


def verify_request(
    method: str,
    target: str,
    headers: Mapping[str, str],
    body: bytes,
    public_key_pem_text: str,
) -> str:
    """Prüft eine eingehende Signatur. Gibt die keyId zurück oder wirft SignatureError."""
    h = {k.lower(): v for k, v in headers.items()}
    if "signature" not in h:
        raise SignatureError("Signatur fehlt")
    sig = parse_signature_header(h["signature"])
    if "keyId" not in sig or "signature" not in sig:
        raise SignatureError("Signatur unvollständig")

    names = sig.get("headers", "date").lower().split()
    required = {"(request-target)", "host", "date"}
    if method.upper() == "POST":
        required.add("digest")
    if not required.issubset(names):
        raise SignatureError("Signatur deckt nicht alle nötigen Header ab")

    try:
        sent = email.utils.parsedate_to_datetime(h.get("date", "")).timestamp()
    except (TypeError, ValueError) as exc:
        raise SignatureError("Ungültiger Date-Header") from exc
    if abs(time.time() - sent) > MAX_CLOCK_SKEW_S:
        raise SignatureError("Date-Header zu alt oder in der Zukunft")

    if "digest" in names and h.get("digest") != body_digest(body):
        raise SignatureError("Digest passt nicht zum Inhalt")

    lines = []
    for name in names:
        if name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {target}")
        elif name in h:
            lines.append(f"{name}: {h[name]}")
        else:
            raise SignatureError(f"Signierter Header fehlt: {name}")

    try:
        public_key = serialization.load_pem_public_key(public_key_pem_text.encode())
        assert isinstance(public_key, rsa.RSAPublicKey)
        public_key.verify(
            base64.b64decode(sig["signature"]),
            "\n".join(lines).encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError, AssertionError) as exc:
        raise SignatureError("Signatur ungültig") from exc
    return sig["keyId"]
