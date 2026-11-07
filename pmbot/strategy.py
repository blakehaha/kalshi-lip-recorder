"""Which liquidity-rewards books to quote, and how much.

The filters come straight out of WRITEUP.md (Kalshi, 69,450 snapshots):
  - decided books (a best bid <=5c or >=95c) pay you to hold the loser;
  - one-sided and thin books are where a target-size quote owns the side,
    and also where informed flow lives;
  - the highest-yield fifth moved 3c+ within ~5 min 36% of the time, mostly
    in programs about to end with wide spreads.
So: quote only liquid, undecided, two-sided books with time left, and treat
an unusually high yield as a warning, not a prize.

The quote is always target size, joining the best bid on both sides (YES bid
and NO bid). Never improving the price: improving buys fills, and fills are
the cost here, not the product.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MarketState:
    market: str
    yes_best: float | None          # best YES bid, dollars (excluding our own orders)
    no_best: float | None           # best NO bid, dollars
    yes_depth: float                # resting YES bid size without us
    no_depth: float
    target_size: float
    reward_per_hour: float          # program pool, dollars/hour
    share: float                    # est. fraction of the pool a target-size quote earns, 0..1
    hours_left: float
    category: str = ""


@dataclass
class Params:
    price_floor: float = 0.06
    price_ceiling: float = 0.94
    min_hours_left: float = 4.0
    max_spread: float = 0.04        # 1 - yes_best - no_best
    require_organic: bool = True    # both sides already at target without us
    min_yield: float = 1.0          # $/day per $1k posted
    max_yield: float | None = None  # yields above this look like bait
    categories: frozenset[str] | None = None  # e.g. {"culture", "tech", "politics"}; None = all


@dataclass
class Quote:
    market: str
    yes_price: float
    no_price: float
    size: float
    capital: float                  # dollars locked if both bids rest
    subsidy_per_hour: float         # dollars/hour expected from rewards
    yield_day_per_1k: float


@dataclass
class Decision:
    quote: Quote | None
    reasons: list[str] = field(default_factory=list)   # why it was skipped


def evaluate(m: MarketState, p: Params) -> Decision:
    why = []
    if p.categories is not None and m.category.lower() not in p.categories:
        why.append("category")
    if m.yes_best is None or m.no_best is None or m.yes_depth <= 0 or m.no_depth <= 0:
        return Decision(None, why + ["one_sided"])
    if min(m.yes_best, m.no_best) < p.price_floor or max(m.yes_best, m.no_best) > p.price_ceiling:
        why.append("decided")
    if m.hours_left < p.min_hours_left:
        why.append("ending")
    if 1 - m.yes_best - m.no_best > p.max_spread + 1e-9:
        why.append("wide")
    if p.require_organic and (m.yes_depth < m.target_size or m.no_depth < m.target_size):
        why.append("thin")
    size = m.target_size
    capital = size * (m.yes_best + m.no_best)
    sub = m.reward_per_hour * m.share
    yld = sub * 24 / capital * 1000 if capital > 0 else 0.0
    if yld < p.min_yield:
        why.append("low_yield")
    if p.max_yield is not None and yld > p.max_yield:
        why.append("bait_yield")
    if why:
        return Decision(None, why)
    return Decision(Quote(m.market, m.yes_best, m.no_best, size, capital, sub, yld))


@dataclass
class Limits:
    max_capital: float = 500.0          # dollars across all markets
    max_capital_per_market: float = 100.0
    max_markets: int = 10
    max_daily_loss: float = 50.0        # realized + marked; trips the kill switch


def allocate(quotes: list[Quote], lim: Limits) -> list[Quote]:
    """Best yield first, inside the caps. A quote too big for the per-market cap
    is skipped, never shrunk: below target size it earns nothing."""
    out, used = [], 0.0
    for q in sorted(quotes, key=lambda q: -q.yield_day_per_1k):
        if len(out) >= lim.max_markets:
            break
        if q.capital > lim.max_capital_per_market or used + q.capital > lim.max_capital:
            continue
        out.append(q)
        used += q.capital
    return out


def killed(pnl_today: float, lim: Limits) -> bool:
    return pnl_today <= -lim.max_daily_loss
