"""Polymarket US Liquidity Incentive Program snapshot scoring.

Per docs.polymarket.us/incentives/liquidity:

  - Once a second, at a random moment, the book is snapshotted. Each side
    (bids, asks) is scored and normalized to 1.0 on its own.
  - Walk from the best price outward, accumulating orders until target_size
    is reached. Orders within that range score; orders past it don't (25,000
    at the best price with a 20,000 target means the second level scores 0).
    The price where the walk reaches target_size is the size-adjusted price.
  - Each scoring order earns  size * discount ** (ticks from the best price).
  - Optional max spread: both sides must reach target_size and each
    size-adjusted price must sit within max_spread of the midpoint of the two,
    or nobody is paid that second (the pool share is forfeited).
  - Payouts under $1.00 aren't made.

Docs example (discount 0.30): 1,000 at each of 0..3 ticks scores 1000+300+90+27;
the best level holds 1000 / 1417 = 70.6%.

Unlike Kalshi (lip/scoring.py), the anchor is the best price, not a reference
price at target/5. Prices in dollars, sizes in contracts. Pass asks as
(price, size) too; ticks are measured away from the best on either side.
"""
from __future__ import annotations

from dataclasses import dataclass

from lip.scoring import TICK, ticks_between


@dataclass
class SideScore:
    qualifies: bool
    best: float | None
    size_adjusted: float | None   # price where the walk reaches target_size
    depth: float                  # all resting size on the side
    score: float
    levels: list[tuple[float, float]]   # scoring (price, size), best first


def _levels(orders: list[tuple[float, float]], asks: bool) -> list[tuple[float, float]]:
    agg: dict[float, float] = {}
    for p, s in orders:
        if s > 0:
            agg[round(float(p), 6)] = agg.get(round(float(p), 6), 0.0) + float(s)
    return sorted(agg.items(), key=lambda x: x[0] if asks else -x[0])


def score_side(orders: list[tuple[float, float]], target_size: float, discount: float,
               tick: float = TICK, asks: bool = False) -> SideScore:
    book = _levels(orders, asks)
    depth = sum(s for _, s in book)
    if not book:
        return SideScore(False, None, None, 0.0, 0.0, [])
    best = book[0][0]
    cum, adj, kept = 0.0, None, []
    for p, s in book:
        cum += s
        kept.append((p, s))
        if cum >= target_size:
            adj = p
            break
    if adj is None:
        return SideScore(False, best, None, depth, 0.0, [])
    score = sum(s * discount ** ticks_between(max(best, p), min(best, p), tick) for p, s in kept)
    return SideScore(True, best, adj, depth, score, kept)


def book_pays(bid: SideScore, ask: SideScore, max_spread: float | None) -> bool:
    """Max-spread test on the whole book. With no max spread each side stands alone."""
    if max_spread is None:
        return True
    if not (bid.qualifies and ask.qualifies):
        return False
    mid = (bid.size_adjusted + ask.size_adjusted) / 2
    return max(mid - bid.size_adjusted, ask.size_adjusted - mid) <= max_spread + 1e-9


def quote_share(orders: list[tuple[float, float]], price: float, size: float, target_size: float,
                discount: float, tick: float = TICK, asks: bool = False) -> float:
    """Share of one side's snapshot score that a new order of `size` at `price` would hold."""
    if size <= 0:
        return 0.0
    r = score_side(list(orders) + [(price, size)], target_size, discount, tick, asks)
    p = round(float(price), 6)
    if not r.qualifies or not r.score or all(lp != p for lp, _ in r.levels):
        return 0.0
    return size * discount ** ticks_between(max(r.best, p), min(r.best, p), tick) / r.score
