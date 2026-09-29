"""Read-only Kalshi REST client.

The only HTTP method this client can send is GET; `_request` raises on
anything else, and there are no order endpoints. Authentication is optional.
Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH to sign requests (RSA-PSS
SHA-256 over timestamp + method + path). Some endpoints, the order book
included, may require it.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiRO:
    def __init__(self, base: str | None = None, key_id: str | None = None, key_path: str | None = None,
                 min_interval_s: float = 0.06, timeout_s: float = 15):
        self.base = (base or os.environ.get("KALSHI_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.key_id = key_id or os.environ.get("KALSHI_KEY_ID") or None
        key_path = key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        self._key = None
        if self.key_id and key_path:
            from cryptography.hazmat.primitives import serialization
            with open(key_path, "rb") as f:
                self._key = serialization.load_pem_private_key(f.read(), password=None)
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._last = 0.0

    def _sign(self, method: str, path: str) -> dict:
        if not self._key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        sig = self._key.sign((ts + method + path).encode(),
                             padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                             hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        if method != "GET":
            raise PermissionError("read-only client: GET only")
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = self.base + path
        q = {k: v for k, v in (params or {}).items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
        sign_path = urllib.parse.urlparse(self.base + path).path  # no query string in the signature
        req = urllib.request.Request(url, headers={"Accept": "application/json", **self._sign("GET", sign_path)})
        self._last = time.monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params)

    def liquidity_programs(self) -> list[dict]:
        """All active liquidity incentive programs (follows pagination)."""
        out, cursor = [], None
        for _ in range(500):
            d = self.get("/incentive_programs", status="active", type="liquidity", limit=1000, cursor=cursor)
            out += d.get("incentive_programs") or []
            cursor = d.get("next_cursor") or d.get("cursor")
            if not cursor:
                break
        return out

    def orderbook(self, ticker: str) -> dict:
        return self.get(f"/markets/{urllib.parse.quote(ticker)}/orderbook", depth=0)


def parse_orderbook(d: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Return (yes_bids, no_bids) as [(price_dollars, contracts)].

    Handles the fixed-point shape ({"orderbook_fp": {"yes_dollars": [["0.15", "100.00"], ...]}})
    and the legacy integer-cents shape ({"orderbook": {"yes": [[15, 100], ...]}})."""
    fp = d.get("orderbook_fp")
    if fp:
        conv = lambda rows: [(float(p), float(s)) for p, s in (rows or [])]
        return conv(fp.get("yes_dollars")), conv(fp.get("no_dollars"))
    ob = d.get("orderbook") or {}
    if ob.get("yes_dollars") is not None or ob.get("no_dollars") is not None:
        conv = lambda rows: [(float(p), float(s)) for p, s in (rows or [])]
        return conv(ob.get("yes_dollars")), conv(ob.get("no_dollars"))
    conv = lambda rows: [(int(p) / 100, float(s)) for p, s in (rows or [])]
    return conv(ob.get("yes")), conv(ob.get("no"))
