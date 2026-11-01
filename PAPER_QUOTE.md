# Paper-quoting the LIP: does the subsidy beat the pick-offs?

*2026-11-01 · `analysis/paper_quote.py` · included data (2026-09-23 to 2026-09-25)*

**Short version:** chasing the subsidy loses money, before and after costs, on the training day and on the held-out day. One narrow slice (slow, multi-day markets) looked positive with almost no fills. That slice is now a **pre-registered rule** that only counts on data recorded after 2026-09-25.

## Model

For each pair of consecutive snapshots of a market (at most 15 minutes apart): post target size at the best bid on both sides, then check the next snapshot.

- **Subsidy:** reward rate × share × time. The v1 data records the smaller side's share, which understates the subsidy.
- **Fill:** a side's best bid fell below our price, so we assume the whole quote was filled. A cancel looks identical in this data, which makes this pessimistic.
- **Cost of a fill**, two models:
  - **Dump (pessimistic):** sell into the new best bid and pay the 7%·p·(1−p) taker fee.
  - **Mark (optimistic):** value at the new mid, no fee. A fill never counts as a gain.
- **Ignored:** good fills and spread capture. When both sides' bids fall, we charge the worse side only, never a "YES+NO redeems at $1" profit. In a wide book a double drop is almost always people pulling quotes, and counting it as a profit was what made the first version show +$261k.

Train: 09-23 and 09-24. Test: 09-25. The grid is 80 rules over spread, hours left, reference-price gap and organic depth.

## Results ($/day per $1k posted)

| | Subsidy | Net, mark | Net, dump | Fill rate per interval |
|---|---|---|---|---|
| Naive: quote every live two-sided book, train | 96 | **−682** | −4,090 | 16% |
| Naive, test | 105 | **−810** | −4,653 | 14% |
| Best grid rule on train (spread ≤1¢, >2h left, organic, ref gap ≤2) | 20 | **−62** | −165 | 0.6% |
| Same rule, test | 22 | **−18** | −68 | 0.5% |

The best rule was chosen on training data and still lost on both days under **both** fill models. The subsidy is real, about 2% a day on capital, but the fills cost 2–5 times that.

**Where the best rule bleeds, all data:**

| Series | Subsidy | Fills | Net, mark | Net, dump |
|---|---|---|---|---|
| KXNFLLADDERRECYDS | $6 | 8 | −$1,159 | −$2,432 |
| **KXRT (your live-test series)** | $107 | 3 | **−$323** | −$807 |
| KXAAAGASD | $2 | 5 | −$168 | −$286 |
| KXTRUMPAPPROVE | $261 | 11 | +$206 | +$1 |
| KXMLBSEASONGAMES | $91 | 0 | +$91 | +$91 |
| KXHORMUZMAX | $73 | 0 | +$73 | +$73 |
| KXSUEZWEEKLY | $33 | 0 | +$33 | +$33 |

## H1: the pre-registered forward test

Locked 2026-11-01 as `analysis.paper_quote.H1_SLOW`:

- **Rule:** the best grid rule, limited to slow, multi-day series: `KXMLBSEASONGAMES`, `KXHORMUZMAX`, `KXHORMUZWEEKLY`, `KXSUEZWEEKLY`, `KXBABELMANDEBWEEKLY`.
- **In-sample (doesn't count):** 5 events, 3,475 intervals, 0.1% fill rate. +$17.6/day per $1k marked, +$13.4/day dumped. Peak capital was $15.8k across 18 markets.
- **Pass:** on data recorded after 2026-09-25, net (mark) > 0 **and** net (dump) > 0, across at least 20 distinct event-days, with no single event providing more than 50% of the net.
- **Kill:** anything else. No re-tuning the series list or thresholds on that same data.

```bash
python analysis/paper_quote.py data/live --rule h1
```

## What this can't see

- **Real fills.** Fills are inferred from 5-minute snapshots, and nothing distinguishes a fill from a cancel. Your live Rotten Tomatoes test has real fills. Comparing them with what this model would have inferred for the same market and hours calibrates both cost models.
- **Reaction speed.** A bot that pulls its quotes within seconds of the book moving dodges some pick-offs. 5-minute data can't measure that.
- **Size.** Everything here assumes target size (usually 1,000 contracts, about $960 to post both sides). A smaller quote keeps a similar subsidy-to-risk ratio while its share stays under ⅕ of the target, but payouts under $1 per period aren't paid.
