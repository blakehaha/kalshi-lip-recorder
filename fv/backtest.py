"""Historical test of the BTC fair-value model on settled Kalshi KXBTCD 5pm-ET markets.

For each settled daily event (KXBTCD-YYMONDD17) and each snapshot H hours
before the close:

  * Kalshi quote: the hourly candle's closing yes_bid / yes_ask at the snapshot
    (batch candlestick endpoint).
  * Spot: Coinbase BTC-USD hourly close at the snapshot (a BRTI constituent).
  * Vol: a smile built from real Deribit BTC option trades in the 2 hours before
    the snapshot (history.deribit.com), median trade IV per instrument,
    out-of-the-money strikes only, same VolSurface code as the live scanner.

Then the live scanner's rule decides trades: lift a side when model EV beats the
ask by --min-edge after the taker fee and --slippage, price in [0.05, 0.95],
first qualifying snapshot only per (ticker, side). P&L is charged the fee and
the slippage. Differences from live: no options-implied forward (spot is the
center) and hourly-close quotes, which may not have been fillable at size.

    python -m fv.backtest --days 120
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lip.client import KalshiRO

from . import sources
from .pricing import contract_fair_value, edges, effective_years, taker_fee
from .vol import VolSurface

MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
HIST = "https://history.deribit.com/api/v2/public"
UA = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (fv-backtest)"}


def _get(url: str, tries: int = 6) -> dict:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise
        except urllib.error.URLError:
            if i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise
    raise RuntimeError("unreachable")


class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, fn):
        p = self.root / (key.replace("/", "_") + ".json")
        if p.exists():
            return json.loads(p.read_text())
        v = fn()
        p.write_text(json.dumps(v))
        return v


def kalshi(client: KalshiRO, path: str, **params) -> dict:
    for i in range(8):
        try:
            return client.get(path, **params)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < 7:
                time.sleep(min(60, 3 * 2 ** i))
                continue
            raise
    raise RuntimeError("unreachable")


def event_ticker(d: datetime) -> str:
    return f"KXBTCD-{d:%y}{MON[d.month - 1]}{d:%d}17"


def load_event(client, cache, ev: str) -> list[dict]:
    return cache.get(f"mk_{ev}", lambda: kalshi(client, "/markets", event_ticker=ev, limit=1000).get("markets") or [])


def load_candles(client, cache, ev: str, tickers: list[str], start: int, end: int) -> dict:
    def fetch():
        out = {}
        for i in range(0, len(tickers), 50):
            d = kalshi(client, "/markets/candlesticks", market_tickers=",".join(tickers[i:i + 50]),
                       start_ts=start, end_ts=end, period_interval=60)
            for m in d.get("markets") or []:
                rows = {}
                for c in m.get("candlesticks") or []:
                    yb = sources.fnum((c.get("yes_bid") or {}).get("close_dollars"))
                    ya = sources.fnum((c.get("yes_ask") or {}).get("close_dollars"))
                    rows[str(c["end_period_ts"])] = [yb, ya]
                out[m["market_ticker"]] = rows
        return out
    return cache.get(f"cd_{ev}", fetch)


def load_spot(cache, start: int, end: int) -> dict[int, float]:
    """Coinbase hourly closes keyed by the hour the candle ENDS (unix s)."""
    out = {}
    t = start - start % 3600
    while t < end:
        t1 = min(end, t + 290 * 3600)
        key = f"cb_{t}_{t1}"
        rows = cache.get(key, lambda: _get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles?" + urllib.parse.urlencode({
                "granularity": 3600, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(t1, timezone.utc).isoformat()})))
        for r in rows:
            out[int(r[0]) + 3600] = float(r[4])
        t = t1
    return out


def load_option_trades(cache, snap: int, window_s: int = 7200) -> list[dict]:
    def fetch():
        trades, end = [], snap * 1000
        start = (snap - window_s) * 1000
        for _ in range(5):
            r = _get(f"{HIST}/get_last_trades_by_currency_and_time?" + urllib.parse.urlencode({
                "currency": "BTC", "kind": "option", "start_timestamp": start, "end_timestamp": end,
                "count": 1000, "sorting": "desc"}))["result"]
            tr = r.get("trades") or []
            trades += [{k: t.get(k) for k in ("timestamp", "iv", "index_price", "instrument_name")} for t in tr]
            if not r.get("has_more") or not tr:
                break
            end = min(t["timestamp"] for t in tr) - 1
        return trades
    return cache.get(f"dt_{snap}", fetch)


def surface_from_trades(trades: list[dict]) -> VolSurface | None:
    if not trades:
        return None
    last_index = max(trades, key=lambda t: t["timestamp"])["index_price"]
    by_inst = defaultdict(list)
    for t in trades:
        if t.get("iv"):
            by_inst[t["instrument_name"]].append(t["iv"])
    summaries = [{"instrument_name": k, "mark_iv": statistics.median(v), "underlying_price": last_index}
                 for k, v in by_inst.items()]
    try:
        return VolSurface.from_deribit(summaries, min_points=3)
    except ValueError:
        return None


def cluster_boot(rows: list[dict], key: str = "event", n: int = 4000, seed: int = 7) -> tuple[float, float]:
    by = defaultdict(list)
    for r in rows:
        by[r[key]].append(r["pnl"])
    groups = list(by.values())
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pick = [g for _ in groups for g in [rng.choice(groups)]]
        flat = [x for g in pick for x in g]
        means.append(sum(flat) / len(flat))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n)]


def summarize(name: str, trades: list[dict]) -> dict:
    if not trades:
        print(f"{name}: no trades")
        return {"n": 0}
    pnl = [t["pnl"] for t in trades]
    lo, hi = cluster_boot(trades)
    ev = len({t["event"] for t in trades})
    s = {"n": len(pnl), "events": ev, "mean": sum(pnl) / len(pnl), "lo": lo, "hi": hi,
         "exp": sum(t["edge"] for t in trades) / len(trades), "win": sum(t["won"] for t in trades) / len(trades)}
    print(f"{name}: {s['n']} trades / {ev} events | mean {s['mean']*100:+.2f}c per contract "
          f"(model expected {s['exp']*100:+.2f}c) | event-clustered 95% CI [{lo*100:+.2f}, {hi*100:+.2f}]c "
          f"| win rate {s['win']:.1%}")
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--offsets", type=float, nargs="+", default=[20, 12, 8, 4, 2], help="hours before close")
    ap.add_argument("--min-edge", type=float, default=0.01)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--cache", default="data/fv/bt_cache")
    ap.add_argument("--out", default="data/fv/backtest_rows.csv")
    a = ap.parse_args(argv)

    client = KalshiRO(min_interval_s=0.6)
    cache = Cache(Path(a.cache))
    today = datetime.now(timezone.utc).date()
    events = []
    for i in range(1, a.days + 1):
        d = today - timedelta(days=i)
        ev = event_ticker(datetime(d.year, d.month, d.day))
        mk = [m for m in load_event(client, cache, ev) if (m.get("result") or "") in ("yes", "no")]
        if mk:
            events.append((ev, mk))
    events.sort(key=lambda x: sources.ts(x[1][0]["close_time"]))
    print(f"{len(events)} settled 5pm events with results")
    if not events:
        return 1
    closes = [int(sources.ts(mk[0]["close_time"])) for _, mk in events]
    spot = load_spot(cache, min(closes) - 26 * 3600, max(closes) + 3600)

    rows = []
    for (ev, mk), close in zip(events, closes):
        cd = load_candles(client, cache, ev, [m["ticker"] for m in mk], close - 26 * 3600, close)
        for h in a.offsets:
            snap = int(close - h * 3600)
            s = spot.get(snap)
            surf = surface_from_trades(load_option_trades(cache, snap))
            if s is None or surf is None:
                continue
            yrs = effective_years(close - snap)
            for m in mk:
                q = cd.get(m["ticker"], {}).get(str(snap))
                if not q:
                    continue
                yb, ya = q
                if yb is None or ya is None or yb <= 0 or ya >= 1:
                    continue
                st = m["strike_type"]
                fl, cp = sources.fnum(m.get("floor_strike")), sources.fnum(m.get("cap_strike"))
                try:
                    fv = contract_fair_value(st, s, yrs, lambda k: surf.vol(k, close, snap), fl, cp)
                except (ValueError, TypeError):
                    continue
                e = edges(fv, yb, ya, slippage=a.slippage)
                rows.append({"event": ev, "close": close, "h": h, "ticker": m["ticker"], "strike": fl or cp,
                             "spot": s, "vol": surf.vol(fl or cp, close, snap), "fv": fv, "yes_bid": yb,
                             "yes_ask": ya, "mid": (yb + ya) / 2, "edge_yes": e["buy_yes"],
                             "edge_no": e["buy_no"], "y": 1 if m["result"] == "yes" else 0})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} priced quotes -> {a.out}")

    # calibration on live-ish quotes
    cal = [r for r in rows if 0.05 <= r["mid"] <= 0.95]
    bm = sum((r["mid"] - r["y"]) ** 2 for r in cal) / len(cal)
    bf = sum((r["fv"] - r["y"]) ** 2 for r in cal) / len(cal)
    print(f"Brier on {len(cal)} quotes with mid in [5,95]c: market mid {bm:.4f} | model {bf:.4f} "
          f"({'model better' if bf < bm else 'market better'})")

    # trades: first qualifying snapshot per (ticker, side), earliest snapshot first
    taken, trades = set(), []
    for r in sorted(rows, key=lambda r: (r["close"], -r["h"])):
        for side, edge, price in (("yes", r["edge_yes"], r["yes_ask"]), ("no", r["edge_no"], 1 - r["yes_bid"])):
            if edge is None or edge < a.min_edge or not (0.05 <= price <= 0.95) or (r["ticker"], side) in taken:
                continue
            taken.add((r["ticker"], side))
            won = (r["y"] == 1) == (side == "yes")
            trades.append({**r, "side": side, "price": price, "edge": edge, "won": won,
                           "pnl": (1.0 if won else 0.0) - price - taker_fee(price) - a.slippage})
    evs = sorted({t["event"] for t in rows}, key=lambda e: next(r["close"] for r in rows if r["event"] == e))
    cut = set(evs[: len(evs) // 2])
    dev = [t for t in trades if t["event"] in cut]
    oos = [t for t in trades if t["event"] not in cut]
    print(f"split: dev = first {len(cut)} events, OOS = last {len(evs) - len(cut)}")
    summarize("ALL", trades)
    summarize("DEV", dev)
    summarize("OOS", oos)
    for side in ("yes", "no"):
        summarize(f"OOS buy {side.upper()}", [t for t in oos if t["side"] == side])
    for h in a.offsets:
        summarize(f"OOS @{h:g}h", [t for t in oos if t["h"] == h])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
