"""Live-test ledger: what a quote earned in LIP rewards vs. what it lost to fills.

    python -m lip.ledger --since 2026-09-27 --rewards rewards.csv        # fetch, cache, report
    python -m lip.ledger --offline --rewards rewards.csv                 # re-report from the cache

Each run pulls your fills (live and historical) and settlements, marks open
positions at the current mid, and measures every fill against 1-minute
candlesticks:

  edge     qty * (mid just before the fill - fill price)   what the quote captured
  adverse  qty * (mid `--markout-min` later - mid before)  what informed flow took back
  markout  edge + adverse                                  P&L of the fill at the horizon

The last column repeats Kalshi's own fees_paid / realized_pnl from
/portfolio/positions. If the fees disagree, trust Kalshi and find out why.

Kalshi's API has no per-account reward endpoint, so LIP payouts come from a CSV
you copy out of the app: date,ticker,amount_dollars[,note]. A blank ticker counts
toward the total only.

Everything is in YES terms. Buying NO at q is selling YES at 1 - q, which has the
same P&L, so qty > 0 means long YES. Fees come from each fill's fee_cost.
Portfolio endpoints need a signed client (KALSHI_KEY_ID + KALSHI_PRIVATE_KEY_PATH);
the client stays GET-only.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import sys
import time
import urllib.error
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .client import KalshiRO, parse_orderbook

log = logging.getLogger("lip.ledger")

CANDLE_CHUNK_S = 3 * 86400  # 4,320 one-minute candles per request
MAX_STALE_S = 15 * 60       # a candle older than this doesn't count as "the mid at t"


@dataclass
class Fill:
    fill_id: str
    ticker: str
    ts: float
    qty: float        # contracts, + bought YES (or sold NO), - sold YES (or bought NO)
    yes_price: float  # dollars
    fee: float        # dollars
    is_taker: bool


def _num(d: dict, *keys, scale: float = 1.0) -> float | None:
    for k in keys:
        if d.get(k) is not None:
            return float(d[k]) * (scale if not k.endswith(("_dollars", "_fp")) else 1.0)
    return None


def normalize_fill(d: dict) -> Fill:
    """One fill from GET /portfolio/fills or /historical/fills, in any of the shapes Kalshi has shipped."""
    book_side = d.get("book_side")
    if book_side is None:  # deprecated side/action: buy YES or sell NO hits the YES bid side
        buy = d.get("action") == "buy"
        book_side = "bid" if buy == (d.get("side") == "yes") else "ask"
    count = _num(d, "count_fp", "count") or 0.0
    price = _num(d, "yes_price_dollars", "yes_price", scale=0.01)
    if price is None:
        raise ValueError(f"fill {d.get('fill_id')} has no yes price")
    created = d.get("created_time")
    ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else float(d.get("ts") or 0)
    return Fill(fill_id=str(d.get("fill_id") or d.get("trade_id")), ticker=d.get("ticker") or d.get("market_ticker"),
                ts=ts, qty=count if book_side == "bid" else -count, yes_price=price,
                fee=float(d.get("fee_cost") or 0), is_taker=bool(d.get("is_taker")))


def settlement_value(s: dict) -> float | None:
    """Dollars paid per YES contract when the market settled."""
    r = s.get("market_result")
    if r == "yes":
        return 1.0
    if r == "no":
        return 0.0
    if r == "scalar" and s.get("value") is not None:
        return int(s["value"]) / 100
    return None


def book_mid(book: dict) -> float | None:
    yes, no = parse_orderbook(book)
    if not yes or not no:
        return None
    return (max(p for p, _ in yes) + 1 - max(p for p, _ in no)) / 2


def _close(dist: dict | None) -> float | None:
    if not dist:
        return None
    v = dist.get("close_dollars", dist.get("close"))
    return float(v) if v is not None else None


def candle_mids(candles: list[dict]) -> tuple[list[float], list[float]]:
    """(end_ts, mid) for each candle whose YES bid and ask both closed with a nonzero price."""
    pts = []
    for c in candles:
        b, a = _close(c.get("yes_bid")), _close(c.get("yes_ask"))
        if b and a:  # a 0 close means that side had no quote
            pts.append((float(c["end_period_ts"]), (b + a) / 2))
    pts.sort()
    return [t for t, _ in pts], [m for _, m in pts]


def mid_at(series: tuple[list[float], list[float]], t: float, max_stale_s: float = MAX_STALE_S) -> float | None:
    """The latest candle mid at or before t, if it is recent enough."""
    ts, mids = series
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > max_stale_s:
        return None
    return mids[i]


def markouts(fills: list[Fill], candles: dict[str, list[dict]], horizon_s: float) -> dict[str, tuple[float, float]]:
    """fill_id -> (mid before the fill, mid horizon_s later). Fills without both are left out."""
    series = {t: candle_mids(c) for t, c in candles.items()}
    out = {}
    for f in fills:
        s = series.get(f.ticker)
        if not s:
            continue
        m0, mh = mid_at(s, f.ts), mid_at(s, f.ts + horizon_s)
        if m0 is not None and mh is not None:
            out[f.fill_id] = (m0, mh)
    return out


def load_rewards(path: str | Path | None) -> dict[str, float]:
    """ticker -> dollars of LIP rewards; '' holds rows without a ticker."""
    out: dict[str, float] = defaultdict(float)
    if not path:
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("amount_dollars") or "").strip():
                out[(row.get("ticker") or "").strip()] += float(row["amount_dollars"])
    return out


def ledger(fills: list[Fill], settled: dict[str, float], marks: dict[str, float],
           rewards: dict[str, float], marks_at: dict[str, tuple[float, float]],
           settle_fees: dict[str, float] | None = None) -> list[dict]:
    """One row per market. P&L is None when a position is open and has no mark."""
    settle_fees = settle_fees or {}
    by: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by[f.ticker].append(f)
    rows = []
    for t in sorted(set(by) | {k for k in rewards if k}):
        fs = by.get(t, [])
        pos = sum(f.qty for f in fs)
        fees = sum(f.fee for f in fs) + settle_fees.get(t, 0.0)
        cash = sum(-f.qty * f.yes_price for f in fs) - fees
        mark = settled.get(t, marks.get(t))
        trading = cash + pos * mark if mark is not None else (cash if abs(pos) < 1e-9 else None)
        mo = [(f, marks_at[f.fill_id]) for f in fs if f.fill_id in marks_at]
        reward = rewards.get(t, 0.0)
        rows.append({
            "ticker": t, "fills": len(fs), "taker_fills": sum(f.is_taker for f in fs),
            "contracts": sum(abs(f.qty) for f in fs), "position": pos,
            "fees": fees, "taker_fees": sum(f.fee for f in fs if f.is_taker),
            "mark": mark, "status": "settled" if t in settled else ("open" if abs(pos) > 1e-9 else "flat"),
            "trading_pnl": trading, "rewards": reward,
            "net_pnl": trading + reward if trading is not None else None,
            "marked_fills": len(mo),
            "edge": sum(f.qty * (m0 - f.yes_price) for f, (m0, _) in mo),
            "adverse": sum(f.qty * (mh - m0) for f, (m0, mh) in mo),
        })
    return rows


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def report(rows: list[dict], unattributed: float, horizon_min: float, since: str | None) -> str:
    known = [r for r in rows if r["net_pnl"] is not None]
    trading = sum(r["trading_pnl"] for r in known)
    rewards = sum(r["rewards"] for r in rows) + unattributed
    fees = sum(r["fees"] for r in rows)
    taker = sum(r["taker_fills"] for r in rows)
    edge, adverse = sum(r["edge"] for r in rows), sum(r["adverse"] for r in rows)
    lines = [
        f"# LIP live-test ledger{f' since {since}' if since else ''}",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Markout horizon: {horizon_min:g} min.",
        "",
        "| | |", "|---|---|",
        f"| Trading P&L (fees included) | {_money(trading)} |",
        f"| LIP rewards | {_money(rewards)} |",
        f"| **Net** | **{_money(trading + rewards)}** |",
        f"| Fees paid | {_money(fees)} |",
        f"| Taker fills (crossed the spread) | {taker} |",
        f"| Edge captured at fill | {_money(edge)} |",
        f"| Mid move {horizon_min:g} min after fill (adverse; negative = picked off) | {_money(adverse)} |",
        "",
    ]
    missing = [r["ticker"] for r in rows if r["net_pnl"] is None]
    if missing:
        lines += [f"Left out of the totals (open, no mark): {', '.join(missing)}", ""]
    if unattributed:
        lines += [f"Rewards with no ticker: {_money(unattributed)}", ""]
    lines += ["| Market | Status | Fills (taker) | Contracts | Position | Fees | Trading P&L | Rewards | Net "
              "| Edge | Adverse | Marked fills | Kalshi fees / realized |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['ticker']} | {r['status']} | {r['fills']} ({r['taker_fills']}) | {r['contracts']:g} "
                     f"| {r['position']:+g} | {_money(r['fees'])} | {_money(r['trading_pnl'])} "
                     f"| {_money(r['rewards'])} | {_money(r['net_pnl'])} | {_money(r['edge'])} "
                     f"| {_money(r['adverse'])} | {r['marked_fills']}/{r['fills']} "
                     f"| {_money(r.get('kalshi_fees'))} / {_money(r.get('kalshi_realized'))} |")
    return "\n".join(lines) + "\n"


# --- fetching -----------------------------------------------------------------------------------------

def _paged(client: KalshiRO, path: str, key: str, **params) -> list[dict]:
    out, cursor = [], None
    for _ in range(1000):
        d = client.get(path, limit=500, cursor=cursor, **params)
        out += d.get(key) or []
        cursor = d.get("cursor") or d.get("next_cursor")
        if not cursor:
            break
    return out


def _try(f, default):
    try:
        return f()
    except urllib.error.HTTPError as e:
        log.warning("%s: HTTP %s", getattr(e, "url", ""), e.code)
        return default


def fetch_candles(client: KalshiRO, ticker: str, start: int, end: int) -> list[dict]:
    series = ticker.split("-")[0]
    out: list[dict] = []
    for a in range(start, end, CANDLE_CHUNK_S):
        b = min(end, a + CANDLE_CHUNK_S)
        d = _try(lambda: client.get(f"/series/{series}/markets/{ticker}/candlesticks",
                                    start_ts=a, end_ts=b, period_interval=1), {})
        cs = d.get("candlesticks") or []
        if not cs:  # markets settled before the historical cutoff live under /historical
            cs = _try(lambda: client.get(f"/historical/markets/{ticker}/candlesticks",
                                         start_ts=a, end_ts=b, period_interval=1), {}).get("candlesticks") or []
        out += cs
    return out


def fetch(client: KalshiRO, cache: Path, min_ts: int | None, horizon_s: float, tickers: list[str] | None) -> None:
    if not client._key:
        raise SystemExit("portfolio endpoints need a signed client: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    cache.mkdir(parents=True, exist_ok=True)
    tk = ",".join(tickers) if tickers else None
    raw: dict[str, dict] = {}
    # fills before the historical cutoff only exist under /historical; a failure on the live list is fatal
    old = _try(lambda: _paged(client, "/historical/fills", "fills", min_ts=min_ts, ticker=tk), [])
    for d in old + _paged(client, "/portfolio/fills", "fills", min_ts=min_ts, ticker=tk):
        raw[str(d.get("fill_id") or d.get("trade_id"))] = d
    one = tickers[0] if tickers and len(tickers) == 1 else None  # settlements filter takes one ticker
    settlements = _paged(client, "/portfolio/settlements", "settlements", min_ts=min_ts, ticker=one)
    # Kalshi's own per-market fees and realized P&L, to check this ledger against
    positions = _try(lambda: _paged(client, "/historical/positions", "market_positions"), [])
    positions += _paged(client, "/portfolio/positions", "market_positions", settlement_status="all")
    fills = [normalize_fill(d) for d in raw.values()]
    settled = {s["ticker"] for s in settlements if settlement_value(s) is not None}
    marks = {}
    for t in sorted({f.ticker for f in fills} - settled):
        m = _try(lambda: book_mid(client.orderbook(t)), None)
        if m is not None:
            marks[t] = m
    candles = {}
    for t in sorted({f.ticker for f in fills}):
        ts = [f.ts for f in fills if f.ticker == t]
        candles[t] = fetch_candles(client, t, int(min(ts) - MAX_STALE_S), int(min(max(ts) + horizon_s + 60, time.time())))
    (cache / "fills.json").write_text(json.dumps(list(raw.values()), indent=1))
    (cache / "settlements.json").write_text(json.dumps(settlements, indent=1))
    (cache / "marks.json").write_text(json.dumps(marks, indent=1))
    (cache / "positions.json").write_text(json.dumps(positions, indent=1))
    (cache / "candles.json").write_text(json.dumps(candles))
    log.info("cached %d fills, %d settlements, %d marks, candles for %d markets in %s",
             len(raw), len(settlements), len(marks), len(candles), cache)


def build(cache: Path, rewards_path: str | None, horizon_s: float, tickers: list[str] | None = None,
          min_ts: float | None = None) -> tuple[list[dict], float, list[Fill], dict]:
    fills = [normalize_fill(d) for d in json.loads((cache / "fills.json").read_text())]
    fills = [f for f in fills if (not tickers or f.ticker in tickers) and (min_ts is None or f.ts >= min_ts)]
    sts = json.loads((cache / "settlements.json").read_text())
    settled = {s["ticker"]: v for s in sts if (v := settlement_value(s)) is not None}
    settle_fees = {s["ticker"]: float(s.get("fee_cost") or 0) for s in sts}
    marks = json.loads((cache / "marks.json").read_text())
    candles = json.loads((cache / "candles.json").read_text()) if (cache / "candles.json").exists() else {}
    rewards = load_rewards(rewards_path)
    if tickers:
        rewards = defaultdict(float, {k: v for k, v in rewards.items() if k in tickers or k == ""})
    mk = markouts(fills, candles, horizon_s)
    unattributed = rewards.pop("", 0.0)
    rows = ledger(fills, settled, marks, rewards, mk, settle_fees)
    pos_file = cache / "positions.json"
    kalshi = {q["ticker"]: q for q in (json.loads(pos_file.read_text()) if pos_file.exists() else [])}
    for r in rows:
        q = kalshi.get(r["ticker"], {})
        r["kalshi_fees"] = float(q["fees_paid_dollars"]) if q.get("fees_paid_dollars") is not None else None
        r["kalshi_realized"] = float(q["realized_pnl_dollars"]) if q.get("realized_pnl_dollars") is not None else None
    return rows, unattributed, fills, mk


def write_fills_csv(path: Path, fills: list[Fill], mk: dict[str, tuple[float, float]]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([*asdict(fills[0]).keys(), "utc", "mid_before", "mid_after"] if fills else ["fill_id"])
        for x in sorted(fills, key=lambda x: x.ts):
            m0, mh = mk.get(x.fill_id, (None, None))
            w.writerow([*asdict(x).values(), f"{datetime.fromtimestamp(x.ts, timezone.utc):%Y-%m-%d %H:%M:%S}", m0, mh])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P&L ledger for a live LIP quote: rewards vs. fills.")
    ap.add_argument("--cache", default="data/ledger", help="where raw API responses are cached (gitignored)")
    ap.add_argument("--since", help="YYYY-MM-DD; only fills and settlements from this date")
    ap.add_argument("--ticker", action="append", help="limit to these markets (repeatable)")
    ap.add_argument("--rewards", help="CSV of LIP payouts: date,ticker,amount_dollars[,note]")
    ap.add_argument("--markout-min", type=float, default=5, help="markout horizon in minutes")
    ap.add_argument("--offline", action="store_true", help="skip the API and report from the cache")
    ap.add_argument("--out", help="also write the Markdown report here")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cache, horizon_s = Path(a.cache), a.markout_min * 60
    min_ts = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()) if a.since else None
    if not a.offline:
        fetch(KalshiRO(), cache, min_ts, horizon_s, a.ticker)
    rows, unattributed, fills, mk = build(cache, a.rewards, horizon_s, a.ticker, min_ts)
    write_fills_csv(cache / "fills_marked.csv", fills, mk)
    md = report(rows, unattributed, a.markout_min, a.since)
    if a.out:
        Path(a.out).write_text(md)
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
