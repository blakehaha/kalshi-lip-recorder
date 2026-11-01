"""Paper-quote the LIP: subsidy earned minus what getting picked off costs.

For every pair of consecutive snapshots of the same market (gap <= 15 min),
if the rule says quote, we post target size at the best bid on both sides
at snapshot t and look at snapshot t+1:

  subsidy   rate * our share * gap.  Share is the v1 min-side share for the
            included data (conservative); v2 files use the filed average share.
  fill      a side's best bid fell below our price. We assume we were filled
            on that whole quote (pessimistic: a cancel looks the same).
  cost      'dump': sell the fill into the new best bid and pay the taker fee
                    ceil-free 0.07 * p * (1 - p) per contract (pessimistic)
            'mark': mark the fill to the new mid, no fee, never a gain (optimistic)
            Both bids fell = charged as the worse single-side fill. In a
            wide book that is almost always people pulling quotes, and
            crediting a YES+NO pair that redeems at $1 would book phantom
            spread capture.

Benign fills (someone sells into us and the price comes back) are ignored,
so spread capture is not counted. This understates good flow and assumes
every fill is adverse.

    python analysis/paper_quote.py                    # included data, train/test split
    python analysis/paper_quote.py data/live --rule '{"max_spread_c": 2, "min_hours": 8}'
    python analysis/paper_quote.py data/live --rule h1     # the pre-registered forward test
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.report import ROOT, load  # noqa: E402

MAX_GAP_MIN = 15
TAKER_FEE = 0.07


@dataclass(frozen=True)
class Rule:
    max_spread_c: float = 100.0     # quote only if implied spread <= this
    min_hours: float = 0.0          # program must have this long left
    min_price_c: float = 6.0        # both best bids inside (min, 100-min): skip decided books
    max_ref_gap: int = 99           # reference price within N ticks of best on both sides
    require_organic: bool = False   # both sides already at target depth without us
    max_yield_day_k: float = 1e12   # skip suspiciously fat yields
    series: tuple[str, ...] = ()    # only these series (empty = all)

    def mask(self, d: pd.DataFrame) -> pd.Series:
        lo, hi = self.min_price_c, 100 - self.min_price_c
        m = ((d.implied_spread_e4 / 100 <= self.max_spread_c)
             & (d.remaining_seconds >= self.min_hours * 3600)
             & d.yes_c.between(lo, hi) & d.no_c.between(lo, hi)
             & (d.yes_ref_gap_ticks <= self.max_ref_gap) & (d.no_ref_gap_ticks <= self.max_ref_gap)
             & (d.yield_day_k <= self.max_yield_day_k)
             & ~d.one_sided)
        if self.require_organic:
            m &= d.organic
        if self.series:
            m &= d.series_ticker.isin(self.series)
        return m


# Pre-registered 2026-11-01 from the included data, BEFORE seeing any data
# recorded after 2026-09-25. Judge it only on data/live from after that date.
H1_SLOW = Rule(max_spread_c=1, min_hours=2, max_ref_gap=2, require_organic=True,
               series=("KXMLBSEASONGAMES", "KXHORMUZMAX", "KXHORMUZWEEKLY",
                       "KXSUEZWEEKLY", "KXBABELMANDEBWEEKLY"))


def pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the next snapshot of the same market to each row."""
    d = df.sort_values(["ticker", "ts_ns"]).copy()
    g = d.groupby("ticker")
    for c in ("yes_best_e4", "no_best_e4", "ts_ns"):
        d[c + "_n"] = g[c].shift(-1)
    d = d.dropna(subset=["ts_ns_n"])
    d["gap_h"] = (d.ts_ns_n - d.ts_ns) / 3.6e12
    return d[d.gap_h * 60 <= MAX_GAP_MIN]


def simulate(d: pd.DataFrame) -> pd.DataFrame:
    """Per-interval paper P&L in dollars for a target-size quote on both sides."""
    q = d.quote_size_e2 / 100
    y0, n0 = d.yes_best_e4 / 1e4, d.no_best_e4 / 1e4
    y1, n1 = d.yes_best_e4_n / 1e4, d.no_best_e4_n / 1e4
    # YES value at t+1: the mid when both sides have bids; with one side
    # empty, the only price left is that side's bid (YES bid, or 1 - NO bid).
    mid1_yes = np.where((y1 > 0) & (n1 > 0), (y1 + 1 - n1) / 2,
                        np.where(y1 > 0, y1, np.where(n1 > 0, 1 - n1, np.nan)))
    mid1_yes = pd.Series(mid1_yes, index=d.index).fillna(y0)   # both empty: no info, no mark
    rate_col = "subsidy_avg_cents_per_hour" if "subsidy_avg_cents_per_hour" in d else "subsidy_cents_per_hour"
    subsidy = d[rate_col].fillna(d.subsidy_cents_per_hour) / 100 * d.gap_h

    fy, fn = y1 < y0 - 1e-9, n1 < n0 - 1e-9
    both = fy & fn
    fee = lambda p: TAKER_FEE * p * (1 - p)
    dump_y = q * (y0 - y1 + fee(y1))
    dump_n = q * (n0 - n1 + fee(n1))
    # long YES at y0 marked to the new mid, floored at zero cost: a fill we
    # inferred from a falling bid is adverse by construction, and "buy the bid
    # in a 60c book, mark to mid" is a gain nobody can realize.
    mark_y = q * (y0 - mid1_yes).clip(lower=0)
    mark_n = q * (n0 - (1 - mid1_yes)).clip(lower=0)
    one_dump = np.where(fy, dump_y, 0) + np.where(fn, dump_n, 0)
    one_mark = np.where(fy, mark_y, 0) + np.where(fn, mark_n, 0)
    out = pd.DataFrame({
        "ticker": d.ticker, "event": d.event, "series": d.series_ticker, "t": d.t,
        "next_one_sided": (y1 <= 0) | (n1 <= 0),
        "capital": d.capital_cents / 100, "gap_h": d.gap_h, "subsidy": subsidy,
        "fill_yes": fy, "fill_no": fn,
        "cost_dump": np.where(both, np.maximum(dump_y, dump_n), one_dump),
        "cost_mark": np.where(both, np.maximum(mark_y, mark_n), one_mark),
    }, index=d.index)
    out["net_dump"] = out.subsidy - out.cost_dump
    out["net_mark"] = out.subsidy - out.cost_mark
    return out


def summarize(s: pd.DataFrame) -> dict:
    if s.empty:
        return {"intervals": 0}
    cap_days = (s.capital * s.gap_h).sum() / 24
    per_k = lambda x: x.sum() / cap_days * 1000 if cap_days else 0.0
    by_event = s.groupby("event").net_dump.sum().sort_values()
    return {
        "intervals": len(s), "markets": s.ticker.nunique(), "events": s.event.nunique(),
        "quote_hours": round(s.gap_h.sum(), 1),
        "fill_rate": round(float((s.fill_yes | s.fill_no).mean()), 3),
        "subsidy_$": round(s.subsidy.sum(), 2),
        "cost_dump_$": round(s.cost_dump.sum(), 2), "cost_mark_$": round(s.cost_mark.sum(), 2),
        "net_dump_$": round(s.net_dump.sum(), 2), "net_mark_$": round(s.net_mark.sum(), 2),
        "net_dump_day_per_1k": round(per_k(s.net_dump), 2),
        "net_mark_day_per_1k": round(per_k(s.net_mark), 2),
        "subsidy_day_per_1k": round(per_k(s.subsidy), 2),
        "peak_capital_$": round(s.groupby("t").capital.sum().max(), 0),
        "worst_event_$": round(by_event.iloc[0], 2), "best_event_$": round(by_event.iloc[-1], 2),
        "share_of_net_from_top_event": round(by_event.iloc[-1] / s.net_dump.sum(), 2) if s.net_dump.sum() > 0 else None,
    }


GRID = {
    "max_spread_c": [1, 2, 3, 5, 100],
    "min_hours": [0, 2, 4, 8],
    "max_ref_gap": [2, 99],
    "require_organic": [False, True],
}


def grid_search(train: pd.DataFrame, min_intervals: int = 200) -> list[tuple[Rule, dict]]:
    res = []
    for vals in itertools.product(*GRID.values()):
        r = Rule(**dict(zip(GRID, vals)))
        s = summarize(simulate(train[r.mask(train)]))
        if s["intervals"] >= min_intervals:
            res.append((r, s))
    return sorted(res, key=lambda x: -x[1]["net_dump_day_per_1k"])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(ROOT / "data"))
    ap.add_argument("--rule", help="JSON Rule fields; skips the grid search")
    ap.add_argument("--split", default="2026-09-25", help="test = snapshots on/after this UTC date")
    a = ap.parse_args(argv)
    pd.set_option("display.width", 200)

    d = pairs(load(a.src))
    show = lambda name, s: print(f"\n## {name}\n" + "\n".join(f"  {k:30s} {v}" for k, v in s.items()))

    if a.rule == "h1":
        show(f"H1 slow-series rule {asdict(H1_SLOW)}", summarize(simulate(d[H1_SLOW.mask(d)])))
        return
    if a.rule:
        r = Rule(**{k: tuple(v) if k == "series" else v for k, v in json.loads(a.rule).items()})
        show(f"rule {asdict(r)}", summarize(simulate(d[r.mask(d)])))
        return

    split = pd.Timestamp(a.split, tz="UTC")
    train, test = d[d.t < split], d[d.t >= split]
    print(f"train {train.t.min():%m-%d %H:%M}..{train.t.max():%m-%d %H:%M} ({len(train):,} intervals) | "
          f"test {test.t.min():%m-%d %H:%M}..{test.t.max():%m-%d %H:%M} ({len(test):,})")

    naive = Rule()
    show("naive yield-chaser (quote every live two-sided book), TRAIN", summarize(simulate(train[naive.mask(train)])))
    show("naive yield-chaser, TEST", summarize(simulate(test[naive.mask(test)])))

    ranked = grid_search(train)
    print(f"\n## grid: {len(ranked)} rules with >=200 train intervals; top 5 by TRAIN net_dump $/day per $1k")
    for r, s in ranked[:5]:
        print(f"  {asdict(r)} -> {s['net_dump_day_per_1k']:+.2f}  (n={s['intervals']}, events={s['events']})")
    best = ranked[0][0]
    show(f"BEST-ON-TRAIN rule {asdict(best)}, TRAIN", ranked[0][1])
    show("same rule, TEST (out of sample)", summarize(simulate(test[best.mask(test)])))


if __name__ == "__main__":
    main()
