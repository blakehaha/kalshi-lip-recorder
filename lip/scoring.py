"""Kalshi Liquidity Incentive Program (LIP) snapshot scoring.

Implements the procedure in Kalshi's CFTC rule filing (Liquidity Incentive
Program, July 15 2026 update) for one side of one market's book:

  1. Start at the highest bid. Add *every* bid at that price level to the
     qualifying set and add the level's size to the running total.
  2. The first level where the running total reaches target_size / 5 becomes
     the Reference Price.
  3. Stop once the running total reaches target_size. Levels past that point
     don't qualify.
  4. Each qualifying bid scores size * discount ** N, where N is the number of
     ticks it sits below the Reference Price (N = 0 at or above it).
  5. A user's normalized side score = their raw score / the side's total raw
     score. Snapshot score = yes share + no share (max 2.0). The period reward
     is split by each user's summed snapshot scores.

Because each snapshot's normalized scores sum to 2 across all users when both
sides qualify, a quote holding share s_yes and s_no earns about
(s_yes + s_no) / 2 of that snapshot's reward.

Levels are processed whole: queue position inside a price level doesn't
matter. For a quote smaller than target_size / 5, improving the price by a
tick earns *nothing extra* while the existing best level is already at or
above the Reference Price; it only buys you fills. A quote of target_size / 5
or more is different: stepping in front makes your level the Reference Price
and discounts everyone below it.

Prices are in dollars (0.01 = 1 cent). Sizes are contracts (floats allowed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

TICK = 0.01


@dataclass
class SideResult:
    qualifies: bool                 # did the side reach target_size?
    reference: float | None         # Reference Price, dollars
    best: float | None              # best bid, dollars
    depth_total: float              # all resting bid size on the side
    depth_qualifying: float         # size inside the qualifying levels
    score_total: float              # sum of raw scores of qualifying levels
    levels: list[tuple[float, float, float]] = field(default_factory=list)  # (price, size, raw score)

    @property
    def ref_gap_ticks(self) -> int:
        if self.best is None or self.reference is None:
            return 0
        return ticks_between(self.best, self.reference)


def ticks_between(hi: float, lo: float, tick: float = TICK) -> int:
    return max(0, round((hi - lo) / tick))


def _merge(levels: list[tuple[float, float]]) -> list[tuple[float, float]]:
    agg: dict[float, float] = {}
    for p, s in levels:
        if s <= 0:
            continue
        p = round(float(p), 6)
        agg[p] = agg.get(p, 0.0) + float(s)
    return sorted(agg.items(), key=lambda x: -x[0])


def score_side(bids: list[tuple[float, float]], target_size: float, discount: float,
               tick: float = TICK) -> SideResult:
    """Score one side's bids (any order, duplicates merged) under the filed procedure."""
    book = _merge(bids)
    depth_total = sum(s for _, s in book)
    best = book[0][0] if book else None
    cum, ref, kept = 0.0, None, []
    for p, s in book:
        cum += s
        kept.append((p, s))
        if ref is None and cum >= target_size / 5:
            ref = p
        if cum >= target_size:
            break
    qualifies = cum >= target_size and ref is not None
    levels = []
    for p, s in kept:
        n = ticks_between(ref, p, tick) if (ref is not None and p < ref) else 0
        levels.append((p, s, s * discount ** n))
    total = sum(x[2] for x in levels) if qualifies else 0.0
    return SideResult(qualifies, ref, best, depth_total, cum if qualifies else 0.0, total,
                      levels if qualifies else [])


def quote_share(bids: list[tuple[float, float]], price: float, size: float, target_size: float,
                discount: float, tick: float = TICK) -> float:
    """Share of one side's snapshot score a new bid of `size` at `price` would hold.

    Adds the order to the book, reruns the procedure, and returns
    my_raw_score / side_total (0 if the side doesn't qualify or my level falls
    past the target-size cutoff)."""
    if size <= 0:
        return 0.0
    r = score_side(list(bids) + [(price, size)], target_size, discount, tick)
    if not r.qualifies:
        return 0.0
    p = round(float(price), 6)
    for lp, ls, lscore in r.levels:
        if lp == p:
            mine = lscore * (size / ls)
            return mine / r.score_total if r.score_total else 0.0
    return 0.0


def snapshot_share(yes_share: float, no_share: float) -> float:
    """Fraction of a snapshot's reward: (yes share + no share) / 2."""
    return (yes_share + no_share) / 2
