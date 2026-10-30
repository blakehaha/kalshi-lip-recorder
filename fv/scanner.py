"""Scan Kalshi BTC markets against a Deribit-implied fair value; paper-log the gaps.

    python -m fv.scanner scan                  # one pass, prints the best gaps
    python -m fv.scanner scan --loop 30        # every 30 s, appends paper entries
    python -m fv.scanner settle                # grade logged entries that have settled

A paper entry is logged when lifting one side is worth at least --min-edge per
contract after Kalshi's taker fee and --slippage, the market closes between
--min-ttl and --max-ttl from now, and the ask price sits inside [--pmin, --pmax].
Each (ticker, side) is logged once: the first time it qualifies.

KXBTC15M markets strike at the BRTI average 15 minutes before close; until that
window has passed the strike isn't known (floor_strike is blank), so they're
skipped.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from lip.client import KalshiRO

from . import sources
from .pricing import contract_fair_value, edges, effective_years, taker_fee
from .vol import VolSurface

LOG_COLS = ["ts_utc", "ticker", "series", "strike_type", "floor", "cap", "close_utc", "secs_to_close",
            "fwd", "vol", "fv", "yes_bid", "yes_ask", "side", "price", "fee", "edge", "size_at_ask"]
SETTLE_COLS = LOG_COLS + ["result", "pnl"]


def now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(client: KalshiRO, series: tuple[str, ...]) -> tuple[float, VolSurface, list[dict], float]:
    t = time.time()
    spot = sources.btc_index()
    cb = sources.coinbase_spot()
    if cb and abs(cb / spot - 1) > 0.003:
        raise RuntimeError(f"spot sources disagree: deribit {spot:.0f} vs coinbase {cb:.0f}")
    surf = VolSurface.from_deribit(sources.btc_option_summaries())
    mkts = []
    for s in series:
        mkts += sources.open_markets(client, s)
    return spot, surf, mkts, t


def price_markets(spot: float, surf: VolSurface, mkts: list[dict], now: float, min_ttl: float,
                  max_ttl: float, slippage: float) -> list[dict]:
    rows = []
    for m in mkts:
        close = sources.ts(m.get("close_time"))
        st = m.get("strike_type")
        floor, cap = sources.fnum(m.get("floor_strike")), sources.fnum(m.get("cap_strike"))
        if close is None or st is None:
            continue
        ttl = close - now
        if not (min_ttl <= ttl <= max_ttl):
            continue
        if st.startswith("greater") and floor is None or st.startswith("less") and cap is None:
            continue
        if st == "between" and (floor is None or cap is None):
            continue
        yrs = effective_years(ttl)
        vol_at = lambda k, c=close: surf.vol(k, c, now)
        fwd = surf.forward(close, now, spot)
        try:
            fv = contract_fair_value(st, fwd, yrs, vol_at, floor, cap)
        except ValueError:
            continue
        yb, ya = sources.fnum(m.get("yes_bid_dollars")), sources.fnum(m.get("yes_ask_dollars"))
        e = edges(fv, yb, ya, slippage=slippage)
        ref = floor if floor is not None else cap
        rows.append({
            "ticker": m["ticker"], "series": m["ticker"].split("-")[0], "strike_type": st,
            "floor": floor, "cap": cap, "close_utc": m.get("close_time"), "secs_to_close": round(ttl),
            "fwd": round(fwd, 2), "vol": round(vol_at(ref), 4), "fv": round(fv, 4),
            "yes_bid": yb, "yes_ask": ya, "edge_yes": e["buy_yes"], "edge_no": e["buy_no"],
            "yes_ask_size": sources.fnum(m.get("yes_ask_size_fp")),
            "yes_bid_size": sources.fnum(m.get("yes_bid_size_fp")),
        })
    return rows


def signals(rows: list[dict], min_edge: float, pmin: float, pmax: float) -> list[dict]:
    out = []
    for r in rows:
        for side, edge, price, size in (
                ("yes", r["edge_yes"], r["yes_ask"], r["yes_ask_size"]),
                ("no", r["edge_no"], None if r["yes_bid"] is None else round(1 - r["yes_bid"], 4),
                 r["yes_bid_size"])):
            if edge is None or price is None or edge < min_edge or not (pmin <= price <= pmax):
                continue
            out.append({**r, "side": side, "price": price, "fee": taker_fee(price), "edge": round(edge, 4),
                        "size_at_ask": size})
    return sorted(out, key=lambda x: -x["edge"])


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _append(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def log_signals(sigs: list[dict], path: Path, t: float) -> int:
    seen = {(r["ticker"], r["side"]) for r in _read(path)}
    fresh = [{**s, "ts_utc": now_iso(t)} for s in sigs if (s["ticker"], s["side"]) not in seen]
    if fresh:
        _append(path, LOG_COLS, fresh)
    return len(fresh)


def print_table(rows: list[dict], sigs: list[dict], spot: float, top: int) -> None:
    print(f"BTC {spot:,.0f} | {len(rows)} markets priced | {len(sigs)} signals")
    near = sorted(rows, key=lambda r: abs((r["fv"] or 0) - 0.5))[:top]
    print(f"{'ticker':34} {'ttl':>6} {'vol':>5} {'fv':>6} {'bid':>6} {'ask':>6} {'eYES':>7} {'eNO':>7}")
    for r in near:
        f = lambda x: "   -  " if x is None else f"{x:+.3f}"
        print(f"{r['ticker']:34} {r['secs_to_close']/60:5.0f}m {r['vol']:5.2f} {r['fv']:6.3f} "
              f"{r['yes_bid'] or 0:6.3f} {r['yes_ask'] or 0:6.3f} {f(r['edge_yes']):>7} {f(r['edge_no']):>7}")
    for s in sigs[:top]:
        print(f"SIGNAL {s['ticker']} buy {s['side'].upper()} @ {s['price']:.3f}  fv {s['fv']:.3f}  "
              f"edge {s['edge']*100:+.1f}c  size {s['size_at_ask']}")


def cmd_scan(a) -> int:
    client = KalshiRO()
    while True:
        try:
            spot, surf, mkts, t = snapshot(client, tuple(a.series))
            rows = price_markets(spot, surf, mkts, t, a.min_ttl, a.max_ttl, a.slippage)
            sigs = signals(rows, a.min_edge, a.pmin, a.pmax)
            n = log_signals(sigs, Path(a.log), t) if not a.dry else 0
            print_table(rows, sigs, spot, a.top)
            if not a.dry:
                print(f"logged {n} new paper entries -> {a.log}")
        except Exception as e:
            print(f"scan failed: {e}", file=sys.stderr)
        if not a.loop:
            return 0
        time.sleep(a.loop)


def cmd_settle(a) -> int:
    client = KalshiRO()
    log, done_path = Path(a.log), Path(a.settled)
    done = {(r["ticker"], r["side"]) for r in _read(done_path)}
    graded = []
    for r in _read(log):
        if (r["ticker"], r["side"]) in done:
            continue
        m = sources.market(client, r["ticker"])
        res = (m.get("result") or "").lower()
        if res not in ("yes", "no"):
            continue
        win = 1.0 if res == r["side"] else 0.0
        graded.append({**r, "result": res, "pnl": round(win - float(r["price"]) - float(r["fee"]), 4)})
    if graded:
        _append(done_path, SETTLE_COLS, graded)
    allr = _read(done_path)
    pnl = [float(r["pnl"]) for r in allr]
    print(f"graded {len(graded)} new; {len(pnl)} settled total")
    if pnl:
        mean = sum(pnl) / len(pnl)
        boots = sorted(sum(random.choices(pnl, k=len(pnl))) / len(pnl) for _ in range(2000))
        exp = sum(float(r["edge"]) for r in allr) / len(allr)
        print(f"mean P&L {mean*100:+.2f}c/contract (model expected {exp*100:+.2f}c), "
              f"95% CI [{boots[50]*100:+.2f}, {boots[1949]*100:+.2f}]c, total {sum(pnl):+.2f} per 1-lot")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi BTC fair-value scanner (paper only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--series", nargs="+", default=list(sources.BTC_SERIES))
    s.add_argument("--min-edge", type=float, default=0.01, help="dollars/contract after fee+slippage")
    s.add_argument("--slippage", type=float, default=0.01)
    s.add_argument("--min-ttl", type=float, default=300, help="seconds")
    s.add_argument("--max-ttl", type=float, default=36 * 3600)
    s.add_argument("--pmin", type=float, default=0.05)
    s.add_argument("--pmax", type=float, default=0.95)
    s.add_argument("--loop", type=float, default=0, help="seconds between scans; 0 = once")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--log", default="data/fv/paper_log.csv")
    s.add_argument("--dry", action="store_true", help="print only, don't log")
    g = sub.add_parser("settle")
    g.add_argument("--log", default="data/fv/paper_log.csv")
    g.add_argument("--settled", default="data/fv/paper_settled.csv")
    a = ap.parse_args(argv)
    return cmd_scan(a) if a.cmd == "scan" else cmd_settle(a)


if __name__ == "__main__":
    sys.exit(main())
