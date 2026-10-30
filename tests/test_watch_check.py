import pandas as pd

from analysis.watch_check import grade


def rows(sweeps, ticker, share_bps=10000, cents=50, capital=90000):
    return [{"sweep": s, "ts_ns": s * 600 * 10**9, "ticker": ticker, "share_avg_bps": share_bps,
             "subsidy_avg_cents_per_hour": cents, "capital_cents": capital} for s in sweeps]


def test_ignores_tickers_outside_the_watch_prefixes():
    df = pd.DataFrame(rows(range(10), "KXDIESELYE-26DEC31-T6.00") + rows([0], "KXTRUMPPHOTO-26NOV01-7", cents=400))
    out = grade(df)
    assert list(out.index) == ["KXDIESELYE-26DEC31-T6.00"]


def test_share_counts_against_all_sweeps_not_just_the_markets_own():
    # Seen in 1 of 10 sweeps at full share: held share in 10% of sweeps, not 100%.
    df = pd.DataFrame(rows(range(10), "KXDIESELYE-26DEC31-T6.00") + rows([3], "KXSUEZWEEKLY-26NOV01-T40", cents=400))
    out = grade(df)
    assert out.loc["KXSUEZWEEKLY-26NOV01-T40", "share_ok"] == 0.1
    assert not out.loc["KXSUEZWEEKLY-26NOV01-T40", "pass"]
    assert out.loc["KXDIESELYE-26DEC31-T6.00", "pass"]


def test_pass_needs_share_in_70pct_of_sweeps_and_40_cents_an_hour():
    held_7 = rows(range(7), "KXHORMUZMAX-A") + rows(range(7, 10), "KXHORMUZMAX-A", share_bps=5999)
    held_6 = rows(range(6), "KXHORMUZMAX-B") + rows(range(6, 10), "KXHORMUZMAX-B", share_bps=5999)
    cheap = rows(range(10), "KXHORMUZMAX-C", cents=39)
    out = grade(pd.DataFrame(held_7 + held_6 + cheap))
    assert out["pass"].to_dict() == {"KXHORMUZMAX-A": True, "KXHORMUZMAX-B": False, "KXHORMUZMAX-C": False}
