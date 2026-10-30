"""Read-only market data: Deribit (BTC index + option marks) and Kalshi BTC markets.

Both clients send GET only. Nothing in this package can place an order.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime

from lip.client import KalshiRO

DERIBIT = "https://www.deribit.com/api/v2/public"
BTC_SERIES = ("KXBTCD", "KXBTC", "KXBTC15M")


def _get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def deribit(method: str, **params) -> dict | list:
    url = f"{DERIBIT}/{method}?{urllib.parse.urlencode(params)}"
    return _get_json(url)["result"]


def btc_index() -> float:
    return float(deribit("get_index_price", index_name="btc_usd")["index_price"])


def btc_option_summaries() -> list[dict]:
    return deribit("get_book_summary_by_currency", currency="BTC", kind="option")


def coinbase_spot() -> float | None:
    try:
        return float(_get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"])
    except Exception:
        return None


def ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fnum(x) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def open_markets(client: KalshiRO, series: str, max_pages: int = 20) -> list[dict]:
    """All open markets in a series (follows the cursor)."""
    out, cursor = [], None
    for _ in range(max_pages):
        for attempt in range(3):
            try:
                d = client.get("/markets", series_ticker=series, status="open", limit=1000, cursor=cursor)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        out += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def market(client: KalshiRO, ticker: str) -> dict:
    return client.get(f"/markets/{urllib.parse.quote(ticker)}").get("market") or {}
