"""Polymarket US Liquidity Incentive Program snapshot scoring.

From the Polymarket US incentives docs (docs.polymarket.us/incentives/liquidity),
as summarized, NOT yet checked against the page itself (the host is blocked in
the environment this was written in):

  - Once a second, at a random moment, the book is snapshotted.
  - Each resting order scores  size * discount ** (ticks from the best price).
  - A side qualifies only if at least target_size contracts rest on it.
  - Optional max spread: the side's size-adjusted price (where the walk down
    the book reaches target_size) must sit within max_spread of the midpoint.
  - The pool is split pro rata by score. Payouts under $1.00 aren't made.

Docs worked example (discount 0.30): 1,000 at the best price scores 1,000;
1,000 three ticks back scores 27; with 1,000 at each of 0..3 ticks the best
level holds 1000 / 1417 = 70.6% of the side.

Differences from Kalshi (lip/scoring.py): the anchor is the best price, not a
reference price at target/5, and nothing here says the walk stops at
target_size, so every level scores. Verify both against the docs before
trusting live share estimates.

Prices in dollars, sizes in contracts.
"""
from __future__ import annotations

from dataclasses import dataclass

from lip.scoring import TICK, _merge, ticks_between


@dataclass
class SideScore:
    qualifies: bool
    best: float | None
    size_adjusted: float | None   # price where cumulative size reaches target_size
    depth: float
    score: float


def score_side(bids: list[tuple[float, float]], target_size: float, discount: float,
               tick: float = TICK) -> SideScore:
    book = _merge(bids)
    depth = sum(s for _, s in book)
    if not book:
        return SideScore(False, None, None, 0.0, 0.0)
    best = book[0][0]
    cum, adj = 0.0, None
    for p, s in book:
        cum += s
        if cum >= target_size:
            adj = p
            break
    qualifies = depth >= target_size
    score = sum(s * discount ** ticks_between(best, p, tick) for p, s in book) if qualifies else 0.0
    return SideScore(qualifies, best, adj, depth, score)


def within_max_spread(side: SideScore, mid: float, max_spread: float | None) -> bool:
    if max_spread is None:
        return True
    return side.size_adjusted is not None and abs(mid - side.size_adjusted) <= max_spread + 1e-9


def quote_share(bids: list[tuple[float, float]], price: float, size: float, target_size: float,
                discount: float, tick: float = TICK) -> float:
    """Share of one side's snapshot score that a new bid of `size` at `price` would hold."""
    if size <= 0:
        return 0.0
    r = score_side(list(bids) + [(price, size)], target_size, discount, tick)
    if not r.qualifies or not r.score:
        return 0.0
    return size * discount ** ticks_between(r.best, round(price, 6), tick) / r.score
