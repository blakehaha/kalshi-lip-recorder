# pmbot: Polymarket US liquidity-rewards market maker

**Status: strategy core and backtest only. It can't place orders, and nothing here should be trusted with money yet.**

The Polymarket US Liquidity Incentive Program pays for resting size near the top of the book. The formula is close to Kalshi's: a random snapshot every second, `size × discount^ticks`, and a target size per side. So everything learned in `WRITEUP.md` carries over, including the bad news.

## Backtest: the subsidy doesn't cover the fills

`python -m pmbot.backtest` replays the 69,450 Kalshi snapshots in `data/` through the strategy. It quotes target size at the best bid on both sides, and charges it for every fill at the next snapshot's bid.

| Variant | Quoted intervals | Subsidy | Fill P&L | Net $/day per $1k |
|---|---|---|---|---|
| Everything two-sided | 59,266 | $11,529 | −$443,751 | −2,679 |
| WRITEUP filters (undecided, ≤4¢ wide, ≥4h left, organic depth) | 12,727 | $1,392 | −$12,885 | −244 |
| … + skip yields >$20/day/1k | 7,900 | $260 | −$1,255 | −34 |
| Culture + politics series only | 6,062 | $837 | −$3,330 | −110 |

- **The filters cut losses about 10×, and the result is still negative.** The yield-cap variant turns positive on one of the two days (+$6/day/1k) and loses on the other. Over 41 hours, that's noise.
- **Losses are lumpy.** 10 fills make up 57% of the filtered loss. Most of those are one NFL yardage ladder whose books collapsed at kickoff, and one Rotten Tomatoes market at its score reveal. These are scheduled information events.
- **Culture and politics aren't safe.** Rotten Tomatoes earned $152 in subsidy and lost $1,160 on fills. Trump approval earned $601 and lost $1,120.
- **The fill model is pessimistic.** A level that disappears counts as a fill even if it was cancelled, and the exit crosses the spread immediately. Marking fills to the next mid instead and dropping the 10 worst events brings the filtered set to roughly breakeven. A real result is somewhere between those two bounds.

**Conclusion:** passive rewards farming on these books loses money. The missing edge is **pulling quotes before scheduled information**: score reveals, poll drops, kickoffs, earnings calls. That needs an event calendar per market, and it's the next thing to build, not live trading.

## Layout

| File | What |
|---|---|
| `scoring.py` | Polymarket US LIP scoring (best-price anchor, target size, optional max spread). Matches the docs' worked example. |
| `strategy.py` | `evaluate()` filters one book into a quote or a list of skip reasons. `allocate()` applies capital caps (never shrinks a quote below target, since that earns nothing). `killed()` is the daily-loss kill switch. |
| `auth.py` | Ed25519 request signer: `timestamp + METHOD + path`. |
| `backtest.py` | The table above. |

## Not built yet, and why

The Polymarket US hosts (`docs.polymarket.us`, `api.polymarket.us`, `gateway.polymarket.us`) were blocked in the environment this was written in. So these were taken from secondhand summaries and **need checking against the docs**:

- Auth header names (placeholders in `auth.py`) and the exact signing example.
- Whether the scoring walk stops at target size, and how the size-adjusted price is defined.
- Every REST and WebSocket schema: markets, books, rewards programs (`discountFactor`, `targetSize`, `rewardPool`), and order create/cancel.

There's no venue client and no live loop on purpose. Writing order code against guessed field names is how you send a wrong-side order.
