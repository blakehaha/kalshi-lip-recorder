"""Grade the 3-day LIP watch against the pre-registered step-1 rule (DIESEL_LIP_TEST.md).

    python analysis/watch_check.py /var/lib/lip/watch

A market passes if its quote held >= 60% share in >= 70% of sweeps and its
recorded subsidy averaged >= 40 cents/hr. Step 1 passes if >= 3 markets pass.

Only the watch series count, and share is measured against every watch sweep,
so a stray test sweep or a market seen once can't pass on a single snapshot.
"""
from __future__ import annotations

import glob
import sys

import pandas as pd

PREFIXES = ("KXDIESEL", "KXHORMUZ", "KXBABELMANDEB", "KXSUEZ")


def grade(df: pd.DataFrame, prefixes: tuple[str, ...] = PREFIXES) -> pd.DataFrame:
    df = df[df.ticker.str.startswith(prefixes)]
    n_sweeps = df.sweep.nunique()
    g = df.groupby("ticker")
    out = pd.DataFrame({
        "sweeps": g.size(),
        "share_ok": g.share_avg_bps.apply(lambda s: (s >= 6000).sum()) / n_sweeps,
        "cents_hr": g.subsidy_avg_cents_per_hour.mean(),
        "capital": g.capital_cents.median() / 100,
    })
    out["usd_day_per_1k"] = out.cents_hr / 100 * 24 / out.capital * 1000
    out["pass"] = (out.share_ok >= 0.70) & (out.cents_hr >= 40)
    return out.sort_values(["pass", "usd_day_per_1k"], ascending=False)


def main(path: str) -> int:
    files = glob.glob(f"{path}/**/*.parquet", recursive=True)
    if not files:
        print(f"no parquet files under {path}")
        return 1
    df = pd.concat(pd.read_parquet(f).assign(sweep=f) for f in files)
    watch = df[df.ticker.str.startswith(PREFIXES)]
    hours = (watch.ts_ns.max() - watch.ts_ns.min()) / 3.6e12
    out = grade(df)
    pd.set_option("display.width", 160)
    print(f"{watch.sweep.nunique()} watch sweeps over {hours:.1f} h, {len(out)} markets")
    print(out.head(20).round(2).to_string())
    n = int(out["pass"].sum())
    print(f"\n{n} markets pass -> STEP 1 {'PASS: go to step 2' if n >= 3 else 'FAIL: stop'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/var/lib/lip/watch"))
