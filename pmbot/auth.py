"""Polymarket US request signing.

Per the Polymarket US authentication docs (summarized; the page wasn't
reachable from the environment this was written in, so check the official
Python example there before going live):
  - Ed25519 over  timestamp_ms + METHOD + path  (no query string, no body).
  - The secret from polymarket.us/developer is base64, 64 bytes; the first 32
    are the Ed25519 private key seed.
  - Headers: access key id, timestamp (ms), base64 signature. Timestamp must
    be within 30 s of server time.
Header NAMES below are placeholders until confirmed against the docs.
"""
from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HDR_KEY, HDR_TS, HDR_SIG = "X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"  # UNVERIFIED names


class Signer:
    def __init__(self, key_id: str, secret_b64: str):
        raw = base64.b64decode(secret_b64)
        if len(raw) not in (32, 64):
            raise ValueError(f"secret decodes to {len(raw)} bytes, expected 32 or 64")
        self.key_id = key_id
        self._key = Ed25519PrivateKey.from_private_bytes(raw[:32])

    def message(self, ts_ms: str, method: str, path: str) -> bytes:
        return (ts_ms + method.upper() + path.split("?", 1)[0]).encode()

    def headers(self, method: str, path: str, now: float | None = None) -> dict[str, str]:
        ts = str(int((time.time() if now is None else now) * 1000))
        sig = self._key.sign(self.message(ts, method, path))
        return {HDR_KEY: self.key_id, HDR_TS: ts, HDR_SIG: base64.b64encode(sig).decode()}
