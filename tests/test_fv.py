import math

import pytest

from fv.pricing import (YEAR_S, contract_fair_value, edges, effective_years, prob_above, taker_fee)
from fv.vol import Smile, VolSurface, parse_instrument


def flat(v):
    return lambda k: v


def test_above_plus_below_is_one():
    yrs = 6 * 3600 / YEAR_S
    a = contract_fair_value("greater", 77000, yrs, flat(0.6), floor=78000)
    b = contract_fair_value("less", 77000, yrs, flat(0.6), cap=78000)
    assert a + b == pytest.approx(1.0)


def test_between_equals_difference_and_brackets_sum_to_one():
    yrs = 20 * 3600 / YEAR_S
    edges_k = [70000 + 500 * i for i in range(30)]
    total = contract_fair_value("less", 77000, yrs, flat(0.5), cap=edges_k[0])
    total += sum(contract_fair_value("between", 77000, yrs, flat(0.5), floor=lo, cap=hi)
                 for lo, hi in zip(edges_k, edges_k[1:]))
    total += contract_fair_value("greater", 77000, yrs, flat(0.5), floor=edges_k[-1])
    assert total == pytest.approx(1.0)


def test_monotone_in_strike_and_atm_just_under_half():
    yrs = 24 * 3600 / YEAR_S
    ps = [prob_above(77000, k, 0.6, yrs) for k in range(74000, 80001, 500)]
    assert all(x > y for x, y in zip(ps, ps[1:]))
    atm = prob_above(77000, 77000, 0.6, yrs)
    assert 0.49 < atm < 0.5  # driftless lognormal median sits just below spot


def test_expiry_limits():
    assert prob_above(77000, 76000, 0.6, 0.0) == 1.0
    assert prob_above(77000, 78000, 0.6, 0.0) == 0.0


def test_settlement_average_shrinks_variance_near_close():
    assert effective_years(3600) == pytest.approx((3600 - 40) / YEAR_S)
    assert effective_years(60) == pytest.approx(20 / YEAR_S)       # continuous at the window edge
    assert effective_years(30) < effective_years(60)
    assert effective_years(0) == 0.0


def test_taker_fee_matches_schedule():
    assert taker_fee(0.50) == 0.02            # 0.0175 rounds up
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.90) == 0.01            # 0.0063 rounds up
    assert taker_fee(0.07, 10) == 0.05        # 0.04557 rounds up


def test_edges_buy_no_uses_one_minus_bid():
    e = edges(0.30, yes_bid=0.40, yes_ask=0.42)
    assert e["buy_yes"] == pytest.approx(0.30 - 0.42 - 0.02)
    assert e["buy_no"] == pytest.approx(0.70 - 0.60 - 0.02)
    assert edges(0.3, None, None) == {"buy_yes": None, "buy_no": None}


def test_parse_instrument():
    exp, k, cp = parse_instrument("BTC-30OCT26-70000-C")
    assert (k, cp) == (70000.0, "C")
    assert exp % 86400 == 8 * 3600
    assert parse_instrument("BTC-PERPETUAL") is None


def _summ(day, strike, cp, iv, fwd=77000):
    return {"instrument_name": f"BTC-{day}-{strike}-{cp}", "mark_iv": iv, "underlying_price": fwd}


def test_surface_keeps_otm_and_interpolates_smile():
    s = [_summ("31OCT26", k, "C", iv) for k, iv in [(78000, 60), (80000, 70), (82000, 80)]]
    s += [_summ("31OCT26", k, "P", iv) for k, iv in [(74000, 90), (76000, 75)]]
    s += [_summ("31OCT26", 74000, "C", 999)]              # ITM call: ignored
    surf = VolSurface.from_deribit(s)
    sm = surf.smiles[0]
    assert len(sm.ks) == 5 and max(sm.ivs) == 0.9
    assert sm.vol(79000) == pytest.approx(0.65, abs=0.01)
    assert sm.vol(90000) == 0.8 and sm.vol(60000) == 0.9   # flat wings


def test_surface_total_variance_time_interpolation_and_forward():
    now = 0.0
    a = Smile(expiry=86400, forward=77100, ks=[-0.1, 0.1], ivs=[0.8, 0.8])
    b = Smile(expiry=3 * 86400, forward=77300, ks=[-0.1, 0.1], ivs=[0.4, 0.4])
    surf = VolSurface([b, a])
    mid = surf.vol(77000, 2 * 86400, now)
    want = math.sqrt((0.8 ** 2 * 1 + (0.4 ** 2 * 3 - 0.8 ** 2 * 1) * 0.5) / 2)
    assert mid == pytest.approx(want)
    assert surf.vol(77000, 3600, now) == 0.8              # before first expiry: front smile
    assert surf.forward(43200, now, 77000) == pytest.approx(77050)
    assert surf.forward(10 * 86400, now, 77000) == 77300


def test_package_never_sends_non_get():
    import fv.sources as s
    src = open(s.__file__).read() + open("fv/scanner.py").read() + open("fv/backtest.py").read()
    assert "POST" not in src and "method=" not in src
