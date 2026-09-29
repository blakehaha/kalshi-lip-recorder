"""Reproduce every number and figure in WRITEUP.md from the Parquet snapshots.

    python analysis/report.py                 # reads data/*.parquet (and data/**/ sweep files)
    python analysis/report.py data/live --no-figs

Definitions (all per snapshot):
  decided    either side's best bid is <=5c or >=95c: the outcome is basically known
  one_sided  a side has no resting bids at all
  organic    both sides already carry >= target size of qualifying depth without you
  survivor   organic, not decided, >4h left on the program, and the reference price
             within 2 ticks of the best bid on both sides (a book you could quote into)
  yield      hypothetical $/day per $1,000 of capital from quoting target size at the
             best bid on both sides (the recorder's subsidy_cents_per_hour / capital)
  move       |change in mid| at the same market's next snapshot (~5 min later), in cents
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def load(src: str) -> pd.DataFrame:
    files = sorted(set(glob.glob(f"{src}/*.parquet") + glob.glob(f"{src}/**/*.parquet", recursive=True)))
    if not files:
        sys.exit(f"no parquet files under {src}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True).drop_duplicates(["ts_ns", "ticker"])
    df["t"] = pd.to_datetime(df.ts_ns, utc=True)
    df["event"] = df.ticker.str.rsplit("-", n=1).str[0]
    df["yes_c"], df["no_c"] = df.yes_best_e4 / 100, df.no_best_e4 / 100
    tgt = df.target_size_e2
    df["organic"] = (df.yes_depth_scoring_e2 >= tgt) & (df.no_depth_scoring_e2 >= tgt)
    df["one_sided"] = (df.yes_depth_total_e2 == 0) | (df.no_depth_total_e2 == 0)
    df["decided"] = (df.yes_c <= 5) | (df.yes_c >= 95) | (df.no_c <= 5) | (df.no_c >= 95)
    df["survivor"] = (df.organic & ~df.one_sided & ~df.decided & (df.remaining_seconds > 4 * 3600)
                      & (df.yes_ref_gap_ticks <= 2) & (df.no_ref_gap_ticks <= 2))
    df["yield_day_k"] = df.subsidy_cents_per_hour / 100 * 24 * (100000 / df.capital_cents.clip(lower=1))
    df["mid"] = (df.yes_c + (100 - df.no_c)) / 2
    df = df.sort_values(["ticker", "ts_ns"])
    g = df.groupby("ticker")
    df["move"] = (g.mid.shift(-1) - df.mid).abs()
    df["gap_min"] = (g.ts_ns.shift(-1) - df.ts_ns) / 6e10
    return df


def pct(x) -> str:
    return f"{100 * float(np.mean(x)):.1f}%"


def report(df: pd.DataFrame) -> dict:
    out = {}
    span_h = (df.t.max() - df.t.min()).total_seconds() / 3600
    sweeps = df.ts_ns.nunique()
    cad = pd.Series(sorted(df.ts_ns.unique())).diff().median() / 6e10
    print(f"## Coverage\n{df.t.min():%Y-%m-%d %H:%M} -> {df.t.max():%Y-%m-%d %H:%M} UTC ({span_h:.1f} h) | "
          f"{sweeps} sweeps, median every {cad:.1f} min | {len(df):,} snapshots | {df.ticker.nunique():,} markets | "
          f"{df.event.nunique():,} events | {df.series_ticker.nunique()} series")
    out.update(span_h=span_h, sweeps=sweeps, rows=len(df), markets=df.ticker.nunique())

    print("\n## Book state (share of snapshots)")
    for c in ["qualifies_scoring", "organic", "one_sided", "decided", "survivor"]:
        print(f"  {c:18s} {pct(df[c])}")
    s = df[df.survivor]
    print(f"  survivors span {s.ticker.nunique()} markets / {s.event.nunique()} events / {s.series_ticker.nunique()} series")

    # yield vs. what happens next
    m = df.dropna(subset=["move"])
    m = m[(m.gap_min < 30) & ~m.decided & ~m.one_sided]
    m = m.assign(q=pd.qcut(m.yield_day_k.rank(method="first"), 5, labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"]))
    tab = m.groupby("q", observed=True).agg(
        snapshots=("mid", "size"),
        median_yield_day_per_1k=("yield_day_k", "median"),
        median_move_c=("move", "median"),
        p_move_ge_3c=("move", lambda x: (x >= 3).mean()),
        median_hours_left=("remaining_seconds", lambda x: x.median() / 3600),
        median_spread_c=("implied_spread_e4", lambda x: x.median() / 100),
        survivor_share=("survivor", "mean"))
    rho = m[["yield_day_k", "move"]].corr(method="spearman").iloc[0, 1]
    print(f"\n## Yield quintiles vs. next-snapshot move (live, two-sided books; n={len(m):,})")
    print(tab.to_string(float_format=lambda v: f"{v:.2f}"))
    print(f"  Spearman(yield, |move|) = {rho:.2f}")
    out.update(quintiles=tab, rho=rho)

    print("\n## Where the survivors are (series, share of survivor snapshots)")
    print((s.series_ticker.value_counts(normalize=True).head(8) * 100).round(1).to_string())

    pt = (df.groupby("ticker").agg(event=("event", "first"), series=("series_ticker", "first"), snaps=("ts_ns", "size"),
                                   surv=("survivor", "mean"),
                                   yield_all=("yield_day_k", lambda x: x.where(df.loc[x.index, "survivor"], 0).mean()),
                                   yes_c=("yes_c", "median"))
          .query("snaps >= 20").sort_values("yield_all", ascending=False).drop_duplicates("event"))
    print("\n## Best market per event, yield counted only while quotable ($/day per $1k)")
    print(pt.head(10)[["series", "surv", "yield_all", "yes_c"]].to_string(float_format=lambda v: f"{v:.2f}"))
    print(f"  events with a market >= $10/day per $1k and quotable >= 50% of the time: "
          f"{pt[(pt.yield_all >= 10) & (pt.surv >= .5)].event.nunique()}")
    out.update(per_event=pt)
    return out


def figures(df: pd.DataFrame, res: dict, outdir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    outdir.mkdir(parents=True, exist_ok=True)
    tab = res["quintiles"]
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    x = np.arange(len(tab))
    ax.bar(x - 0.2, tab.median_yield_day_per_1k, 0.4, color="#2f6f4f", label=r"median yield, \$/day per \$1k")
    ax.set_yscale("log"); ax.set_ylabel(r"\$/day per \$1k (log)")
    ax2 = ax.twinx()
    ax2.bar(x + 0.2, tab.p_move_ge_3c * 100, 0.4, color="#b5452b", label="P(mid moves ≥3¢ by next snapshot)")
    ax2.set_ylabel("% of snapshots")
    ax.set_xticks(x, [str(i) for i in tab.index], fontsize=8)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="upper left", frameon=False)
    ax.set_title("Higher subsidy yield comes with more informed flow", fontsize=10)
    fig.tight_layout(); fig.savefig(outdir / "yield_vs_move.png"); plt.close(fig)

    m = df[~df.decided & ~df.one_sided & (df.yield_day_k > 0)]
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    ax.scatter(m.remaining_seconds / 3600, m.yield_day_k, s=2, alpha=0.15, color="#555")
    sv = m[m.survivor]
    ax.scatter(sv.remaining_seconds / 3600, sv.yield_day_k, s=2, alpha=0.3, color="#2f6f4f", label="survivor (quotable book, >4h left)")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("hours left in program"); ax.set_ylabel(r"\$/day per \$1k")
    ax.set_title("The fattest yields sit in the last hours before resolution", fontsize=10)
    ax.legend(fontsize=7, frameon=False, markerscale=5)
    fig.tight_layout(); fig.savefig(outdir / "yield_vs_time_left.png"); plt.close(fig)
    print(f"\nfigures -> {outdir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(ROOT / "data"))
    ap.add_argument("--no-figs", action="store_true")
    a = ap.parse_args()
    pd.set_option("display.width", 160)
    d = load(a.src)
    r = report(d)
    if not a.no_figs:
        figures(d, r, ROOT / "docs")
