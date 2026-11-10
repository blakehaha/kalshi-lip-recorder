import pandas as pd
import pytest

from analysis.paper_quote import H1_SLOW, Rule, simulate, summarize


def frame(y0, n0, y1, n1, q=1000, rate=100.0, gap_h=5 / 60, spread_c=None):
    """One interval: best bids (dollars) at t and t+1, a q-lot quote, rate in cents/hour."""
    return pd.DataFrame([{
        "ticker": "T-1", "event": "T", "series_ticker": "T", "t": pd.Timestamp("2026-09-24", tz="UTC"),
        "quote_size_e2": q * 100, "capital_cents": q * (y0 + n0) * 100,
        "yes_best_e4": y0 * 1e4, "no_best_e4": n0 * 1e4, "yes_best_e4_n": y1 * 1e4, "no_best_e4_n": n1 * 1e4,
        "subsidy_cents_per_hour": rate, "gap_h": gap_h,
    }])


def test_no_fill_earns_only_subsidy():
    s = simulate(frame(0.50, 0.49, 0.50, 0.49))
    assert not s.fill_yes.iloc[0] and not s.fill_no.iloc[0]
    assert s.subsidy.iloc[0] == pytest.approx(1.00 * 5 / 60)
    assert s.cost_dump.iloc[0] == 0 and s.cost_mark.iloc[0] == 0


def test_yes_bid_drop_is_an_adverse_fill():
    # quoted YES 50, book drops to 47/52: long 1000 YES, dump at 47 + fee
    s = simulate(frame(0.50, 0.49, 0.47, 0.52))
    assert s.fill_yes.iloc[0] and not s.fill_no.iloc[0]
    fee = 0.07 * 0.47 * 0.53
    assert s.cost_dump.iloc[0] == pytest.approx(1000 * (0.03 + fee))
    assert s.cost_mark.iloc[0] == pytest.approx(1000 * (0.50 - 0.475))


def test_wide_book_fill_never_marks_to_a_gain():
    # buy YES at 20 in a 60c book; new mid 49.5 would be a fake +$295
    s = simulate(frame(0.20, 0.20, 0.19, 0.20))
    assert s.cost_mark.iloc[0] == 0


def test_both_bids_drop_charged_as_worse_side_not_pair_profit():
    s = simulate(frame(0.30, 0.30, 0.25, 0.28))
    assert s.cost_dump.iloc[0] > 0           # no phantom YES+NO redemption credit


def test_one_sided_next_snapshot_marks_to_remaining_bid():
    s = simulate(frame(0.50, 0.49, 0.10, 0.0))
    assert s.next_one_sided.iloc[0]
    assert s.cost_mark.iloc[0] == pytest.approx(1000 * 0.40)


def test_rule_series_filter_and_h1_is_frozen():
    d = pd.DataFrame({"implied_spread_e4": [100, 100], "remaining_seconds": [9e4, 9e4], "yes_c": [50, 50],
                      "no_c": [49, 49], "yes_ref_gap_ticks": [0, 0], "no_ref_gap_ticks": [0, 0],
                      "yield_day_k": [10, 10], "one_sided": [False, False], "organic": [True, True],
                      "series_ticker": ["KXSUEZWEEKLY", "KXRT"]})
    assert Rule(series=("KXSUEZWEEKLY",)).mask(d).tolist() == [True, False]
    assert "KXRT" not in H1_SLOW.series and H1_SLOW.max_spread_c == 1


def test_summary_per_1k_scaling():
    s = simulate(frame(0.50, 0.49, 0.50, 0.49, rate=100.0, gap_h=24.0))   # $24 over a day on $990
    assert summarize(s)["subsidy_day_per_1k"] == pytest.approx(24 / 990 * 1000, rel=1e-3)
