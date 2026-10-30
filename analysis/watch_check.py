"""Grade the 3-day LIP watch against the pre-registered step-1 rule (DIESEL_LIP_TEST.md).

    python analysis/watch_check.py /var/lib/lip/watch

A market passes if its quote held >= 60% share in >= 70% of sweeps and its
recorded subsidy averaged >= 40 cents/hr. Step 1 passes if >= 3 markets pass.
"""
from __future__ import annotations

import glob
import sys

import pandas as pd


def main(path: str) -> int:
    files = glob.glob(f"{path}/**/*.parquet", recursive=True)
    if not files:
        print(f"no parquet files under {path}")
        return 1
    df = pd.concat(pd.read_parquet(f) for f in files)
    hours = (df.ts_ns.max() - df.ts_ns.min()) / 3.6e12
    g = df.groupby("ticker")
    out = pd.DataFrame({
        "sweeps": g.size(),
        "share_ok": g.apply(lambda x: (x.share_avg_bps >= 6000).mean()),
        "cents_hr": g.subsidy_avg_cents_per_hour.mean(),
        "capital": g.capital_cents.median() / 100,
    })
    out["usd_day_per_1k"] = out.cents_hr / 100 * 24 / out.capital * 1000
    out["pass"] = (out.share_ok >= 0.70) & (out.cents_hr >= 40)
    out = out.sort_values(["pass", "usd_day_per_1k"], ascending=False)
    pd.set_option("display.width", 160)
    print(f"{len(files)} sweeps over {hours:.1f} h, {len(out)} markets")
    print(out.head(20).round(2).to_string())
    n = int(out["pass"].sum())
    print(f"\n{n} markets pass -> STEP 1 {'PASS: go to step 2' if n >= 3 else 'FAIL: stop'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/var/lib/lip/watch"))
