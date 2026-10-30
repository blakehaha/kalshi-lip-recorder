"""Fair value for Kalshi BTC price contracts, and Kalshi's taker fee.

Kalshi's BTC series (KXBTCD "above/below", KXBTC "range") settle on the simple
average of the last 60 seconds of CF Benchmarks' BRTI before the close. We
price them as digital options on a driftless lognormal:

    P(S_T > K) = N(d2),  d2 = (ln(S/K) - var/2) / sqrt(var)

The 60-second average matters only near the close. For an arithmetic average
over the final window w, the log-variance is approximately
sigma^2 * (T - 2w/3) instead of sigma^2 * T, so `effective_years` shrinks T.

Prices are probabilities in [0, 1] (Kalshi dollars per contract).
"""
from __future__ import annotations

import math

YEAR_S = 365.0 * 24 * 3600
SETTLE_WINDOW_S = 60.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def effective_years(seconds_to_close: float, window_s: float = SETTLE_WINDOW_S) -> float:
    """Variance-equivalent time for a contract that settles on a trailing average."""
    if seconds_to_close <= 0:
        return 0.0
    if seconds_to_close <= window_s:
        # inside the averaging window: part of the average is already fixed;
        # the remaining random part is (t/w) of an average over t seconds
        t = seconds_to_close
        return (t / window_s) ** 2 * (t / 3.0) / YEAR_S
    return (seconds_to_close - 2.0 * window_s / 3.0) / YEAR_S


def prob_above(spot: float, strike: float, vol: float, years: float) -> float:
    """P(S_T > strike) under a driftless lognormal with annualised vol."""
    if strike <= 0:
        return 1.0
    var = vol * vol * years
    if var <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * var) / math.sqrt(var)
    return norm_cdf(d2)


def contract_fair_value(strike_type: str, spot: float, years: float, vol_at,
                        floor: float | None = None, cap: float | None = None) -> float:
    """Fair YES price for a Kalshi BTC market.

    strike_type: 'greater' (above floor), 'less' (below cap), 'between' (floor..cap).
    vol_at: callable strike -> annualised vol, so each strike reads its own point
    on the smile.
    """
    if strike_type in ("greater", "greater_or_equal"):
        return prob_above(spot, floor, vol_at(floor), years)
    if strike_type in ("less", "less_or_equal"):
        return 1.0 - prob_above(spot, cap, vol_at(cap), years)
    if strike_type == "between":
        return max(0.0, prob_above(spot, floor, vol_at(floor), years)
                   - prob_above(spot, cap, vol_at(cap), years))
    raise ValueError(f"unsupported strike_type {strike_type!r}")


def taker_fee(price: float, count: float = 1.0, rate: float = 0.07) -> float:
    """Kalshi quadratic taker fee: round up to the cent of rate * C * P * (1 - P)."""
    raw = rate * count * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100.0


def edges(fv: float, yes_bid: float | None, yes_ask: float | None, count: float = 1.0,
          slippage: float = 0.0) -> dict:
    """Per-contract expected value, after taker fee and slippage, of lifting each side.

    Buying YES at the ask pays yes_ask. Buying NO lifts the NO ask, which is
    1 - yes_bid. Returns None for a side with no quote."""
    out = {"buy_yes": None, "buy_no": None}
    if yes_ask is not None and 0 < yes_ask < 1:
        fee = taker_fee(yes_ask, count) / count
        out["buy_yes"] = fv - yes_ask - fee - slippage
    if yes_bid is not None and 0 < yes_bid < 1:
        no_ask = 1.0 - yes_bid
        fee = taker_fee(no_ask, count) / count
        out["buy_no"] = (1.0 - fv) - no_ask - fee - slippage
    return out
