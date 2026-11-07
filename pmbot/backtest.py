"""Replay recorded LIP snapshots through the strategy and charge it for fills.

    python -m pmbot.backtest                  # data/*.parquet (the Kalshi set in this repo)
    python -m pmbot.backtest data/live

The data is Kalshi, not Polymarket US: the two programs score almost the same
way, and this is the only order-book history we have. Treat it as a test of
the filters, not a forecast of Polymarket US pay.

Per market, between consecutive snapshots (gap < 30 min):
  quote     strategy.evaluate on the snapshot; if it passes, rest target size at
            the best YES bid and the best NO bid (back of the queue).
  subsidy   subsidy_per_hour * gap (half the gap if anything filled).
  fill      a bid counts as filled if the next snapshot's best bid on its side is
            BELOW our price: the whole level is gone, so the back of the queue
            went too. Levels pulled without trading also count as fills, which
            makes this pessimistic.
  exit      a filled YES is sold to the next snapshot's best YES bid (below our
            price by construction), a filled NO to the best NO bid. If both
            levels vanished we charge the worse side, never credit the pair.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.report import load  # noqa: E402
from pmbot.strategy import MarketState, Params, evaluate  # noqa: E402

MAX_GAP_MIN = 30


def state(r) -> MarketState:
    rate = r.period_reward_cents / 100 / (r.program_seconds / 3600) if r.program_seconds > 0 else 0.0
    yb, nb = r.yes_best_e4 / 1e4, r.no_best_e4 / 1e4
    return MarketState(
        market=r.ticker, yes_best=yb if r.yes_depth_total_e2 > 0 else None,
        no_best=nb if r.no_depth_total_e2 > 0 else None,
        yes_depth=r.yes_depth_scoring_e2 / 100, no_depth=r.no_depth_scoring_e2 / 100,
        target_size=r.target_size_e2 / 100, reward_per_hour=rate, share=r.share_bps / 1e4,
        hours_left=r.remaining_seconds / 3600, category=r.series_ticker)


def fill_pnl(y: float, n: float, q: float, yb2: float | None, nb2: float | None) -> tuple[bool, bool, float]:
    """(yes_filled, no_filled, dollars) for bids at y and n of size q, given next best bids.

    A held YES is sold to the best YES bidder (yb2), a held NO to the best NO
    bidder; an empty side is worth 0. When both levels vanished we don't credit
    the locked pair (that's usually a book being pulled, not two fills); we
    charge the worse single fill instead."""
    fy = yb2 is None or yb2 < y - 1e-9
    fn = nb2 is None or nb2 < n - 1e-9
    loss_y = q * ((yb2 or 0.0) - y)
    loss_n = q * ((nb2 or 0.0) - n)
    if fy and fn:
        return True, True, min(loss_y, loss_n)
    if fy:
        return True, False, loss_y
    if fn:
        return False, True, loss_n
    return False, False, 0.0


def run(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """One row per quoted interval: subsidy, fill pnl, capital-hours."""
    out = []
    for _, g in df.groupby("ticker", sort=False):
        rows = list(g.itertuples(index=False))
        for a, b in zip(rows, rows[1:]):
            gap_h = (b.ts_ns - a.ts_ns) / 3.6e12
            if gap_h * 60 > MAX_GAP_MIN:
                continue
            d = evaluate(state(a), p)
            if d.quote is None:
                continue
            q = d.quote
            nxt = state(b)
            fy, fn, pnl = fill_pnl(q.yes_price, q.no_price, q.size, nxt.yes_best, nxt.no_best)
            sub = q.subsidy_per_hour * gap_h * (0.5 if (fy or fn) else 1.0)
            out.append((a.ticker, a.t, q.yield_day_per_1k, sub, pnl, fy or fn, q.capital * gap_h))
    return pd.DataFrame(out, columns=["ticker", "t", "yield", "subsidy", "fill_pnl", "filled", "cap_h"])


def summarize(r: pd.DataFrame) -> dict:
    if r.empty:
        return dict(intervals=0, markets=0, subsidy=0.0, fill_pnl=0.0, net=0.0, fill_rate=np.nan, net_day_per_1k=np.nan)
    net = r.subsidy.sum() + r.fill_pnl.sum()
    cap_days = r.cap_h.sum() / 24
    return dict(intervals=len(r), markets=r.ticker.nunique(), subsidy=r.subsidy.sum(), fill_pnl=r.fill_pnl.sum(),
                net=net, fill_rate=r.filled.mean(), net_day_per_1k=net / cap_days * 1000 if cap_days else np.nan)


VARIANTS = {
    "everything two-sided": Params(price_floor=0, price_ceiling=1, min_hours_left=0, max_spread=1,
                                   require_organic=False, min_yield=0),
    "writeup filters": Params(),
    "writeup + yield cap 50": Params(max_yield=50),
    "writeup + yield cap 20": Params(max_yield=20),
    "tight (2c spread, 12h left)": Params(max_spread=0.02, min_hours_left=12),
    # The culture / politics slice of the Kalshi set (category = series ticker here).
    "culture+politics only": Params(categories=frozenset(
        {"kxrt", "kxtrumpapprove", "kxtrumpmention", "kxtrumpmentionb", "kxaprpotus", "kxactbluetop"})),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the rewards-MM filters on recorded snapshots.")
    ap.add_argument("src", nargs="?", default="data")
    a = ap.parse_args(argv)
    df = load(a.src)
    days = sorted(df.t.dt.date.unique())
    fmt = lambda s: (f"{s['intervals']:6d} {s['markets']:5d} {s['subsidy']:10.2f} {s['fill_pnl']:10.2f} "
                     f"{s['net']:10.2f} {s['fill_rate']:7.1%} {s['net_day_per_1k']:9.2f}")
    print(f"{'variant':32s} {'slice':10s} {'n':>6s} {'mkts':>5s} {'subsidy$':>10s} {'fills$':>10s} "
          f"{'net$':>10s} {'filled':>7s} {'$/d/1k':>9s}")
    for name, p in VARIANTS.items():
        r = run(df, p)
        print(f"{name:32s} {'all':10s} {fmt(summarize(r))}")
        for d in days:
            print(f"{'':32s} {str(d):10s} {fmt(summarize(r[r.t.dt.date == d]))}")
    r = run(df, VARIANTS["writeup filters"])
    if len(r) >= 25:
        r = r.assign(q=pd.qcut(r["yield"].rank(method="first"), 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"]))
        print("\nwriteup filters, by yield quintile:")
        print(r.groupby("q", observed=True).agg(n=("yield", "size"), med_yield=("yield", "median"),
                                                 subsidy=("subsidy", "sum"), fills=("fill_pnl", "sum"),
                                                 fill_rate=("filled", "mean")).round(3).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
