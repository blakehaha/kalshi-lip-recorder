"""Always-on LIP recorder.

Every sweep:
  1. Pull the list of active liquidity programs (one paginated GET).
  2. Pick `--markets` of them (the top payers by reward rate by default,
     one per ticker).
  3. GET each market's order book and score both sides under the filed LIP
     procedure, including a hypothetical quote of target size joining the
     best bid on each side.
  4. Write one Parquet file per sweep: <out>/date=YYYY-MM-DD/sweep-HHMMSS.parquet.

    python -m lip.recorder --out data/live --markets 150 --interval 300
    python -m lip.recorder --once          # single sweep, handy for testing

Units follow the column suffix: _e4 = dollars x 1e4, _e2 = contracts x 100,
_cents = cents, _bps = basis points.
"""
from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import KalshiRO, parse_orderbook
from .scoring import quote_share, score_side, snapshot_share

log = logging.getLogger("lip.recorder")

COLUMNS = [
    "ts_ns", "ticker", "series_ticker", "program_id", "description", "period_reward_cents", "program_seconds",
    "remaining_seconds", "target_size_e2", "discount_bps", "yes_best_e4", "no_best_e4", "yes_ref_e4", "no_ref_e4",
    "yes_ref_gap_ticks", "no_ref_gap_ticks", "implied_spread_e4", "yes_depth_total_e2", "no_depth_total_e2",
    "yes_depth_scoring_e2", "no_depth_scoring_e2", "yes_score_e2", "no_score_e2", "qualifies_total",
    "qualifies_scoring", "quote_size_e2", "share_bps", "share_avg_bps", "capital_cents", "subsidy_cents_per_hour",
    "subsidy_avg_cents_per_hour",
]


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def program_rate_cents_per_hour(p: dict) -> float:
    start, end = _ts(p.get("start_date")), _ts(p.get("end_date"))
    if not start or not end or end <= start:
        return 0.0
    return (float(p.get("period_reward") or 0) / 100) / ((end - start) / 3600)  # period_reward is centi-cents


def select_programs(programs: list[dict], n: int, mode: str = "top", rng: random.Random | None = None) -> list[dict]:
    best: dict[str, dict] = {}
    for p in programs:
        t = p.get("market_ticker")
        if not t or (p.get("incentive_type") not in (None, "liquidity")):
            continue
        if t not in best or program_rate_cents_per_hour(p) > program_rate_cents_per_hour(best[t]):
            best[t] = p
    ps = list(best.values())
    if mode == "random":
        (rng or random).shuffle(ps)
    else:
        ps.sort(key=program_rate_cents_per_hour, reverse=True)
    return ps[:n]


def build_row(p: dict, book: dict, now: float) -> dict:
    ticker = p["market_ticker"]
    yes, no = parse_orderbook(book)
    target = float(p.get("target_size_fp") or p.get("target_size") or 0)
    disc_bps = int(p.get("discount_factor_bps") or 0)
    disc = disc_bps / 10000
    ys, ns = score_side(yes, target, disc), score_side(no, target, disc)
    q = target  # hypothetical quote: target size, joining the best bid on each side
    sy = quote_share(yes, ys.best, q, target, disc) if ys.best is not None else 0.0
    sn = quote_share(no, ns.best, q, target, disc) if ns.best is not None else 0.0
    share_min, share_avg = min(sy, sn), snapshot_share(sy, sn)
    rate = program_rate_cents_per_hour(p)
    start, end = _ts(p.get("start_date")), _ts(p.get("end_date"))
    yb, nb = ys.best or 0.0, ns.best or 0.0
    e4 = lambda x: int(round((x or 0) * 10000))
    e2 = lambda x: int(round((x or 0) * 100))
    return {
        "ts_ns": int(now * 1e9), "ticker": ticker, "series_ticker": ticker.split("-")[0],
        "program_id": p.get("id", ""), "description": p.get("incentive_description") or "",
        "period_reward_cents": int(float(p.get("period_reward") or 0) // 100),
        "program_seconds": int((end - start) if start and end else 0),
        "remaining_seconds": int(max(0, (end - now)) if end else 0),
        "target_size_e2": e2(target), "discount_bps": disc_bps,
        "yes_best_e4": e4(yb), "no_best_e4": e4(nb), "yes_ref_e4": e4(ys.reference), "no_ref_e4": e4(ns.reference),
        "yes_ref_gap_ticks": ys.ref_gap_ticks, "no_ref_gap_ticks": ns.ref_gap_ticks,
        "implied_spread_e4": e4(1 - yb - nb) if (ys.best is not None and ns.best is not None) else 10000,
        "yes_depth_total_e2": e2(ys.depth_total), "no_depth_total_e2": e2(ns.depth_total),
        "yes_depth_scoring_e2": e2(ys.depth_qualifying), "no_depth_scoring_e2": e2(ns.depth_qualifying),
        "yes_score_e2": e2(ys.score_total), "no_score_e2": e2(ns.score_total),
        "qualifies_total": ys.depth_total >= target and ns.depth_total >= target,
        "qualifies_scoring": ys.qualifies and ns.qualifies,
        "quote_size_e2": e2(q), "share_bps": int(share_min * 10000), "share_avg_bps": int(share_avg * 10000),
        "capital_cents": int(round(q * (yb + nb) * 100)),
        "subsidy_cents_per_hour": int(rate * share_min), "subsidy_avg_cents_per_hour": int(rate * share_avg),
    }


def write_sweep(rows: list[dict], out: Path, now: float) -> Path | None:
    if not rows:
        return None
    import pyarrow as pa
    import pyarrow.parquet as pq
    dt = datetime.fromtimestamp(now, timezone.utc)
    d = out / f"date={dt:%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"sweep-{dt:%H%M%S}.parquet"
    tmp = path.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pylist(rows).select(COLUMNS), tmp, compression="zstd")
    tmp.replace(path)
    return path


def sweep(client: KalshiRO, out: Path, n: int, mode: str = "top") -> Path | None:
    programs = select_programs(client.liquidity_programs(), n, mode)
    rows, now = [], time.time()
    for p in programs:
        try:
            rows.append(build_row(p, client.orderbook(p["market_ticker"]), now))
        except Exception as e:  # one bad market never kills the sweep
            log.warning("skip %s: %s", p.get("market_ticker"), e)
    path = write_sweep(rows, out, now)
    log.info("sweep: %d/%d markets -> %s", len(rows), len(programs), path)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record Kalshi LIP order-book snapshots to Parquet.")
    ap.add_argument("--out", default="data/live")
    ap.add_argument("--markets", type=int, default=150)
    ap.add_argument("--interval", type=float, default=300, help="seconds between sweep starts")
    ap.add_argument("--select", choices=["top", "random"], default="top")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client, out = KalshiRO(), Path(a.out)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while True:
        t0 = time.monotonic()
        try:
            sweep(client, out, a.markets, a.select)
        except Exception as e:
            log.error("sweep failed: %s", e)
        if a.once or stop["now"]:
            return 0
        time.sleep(max(1.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
