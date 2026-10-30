# BTC fair-value test: rules written before results

*Written 2026-10-29, before the historical run and before any paper entry.*

## The claim

Kalshi's BTC above/below markets (KXBTCD) are priced by flow that leans YES.
A digital-option fair value built from Deribit's implied-vol smile is closer to
the truth than the Kalshi quote, closely enough to beat the taker fee.

## Gate 1: history (`python -m fv.backtest --days 120`)

The trade rule is fixed: lift a side when model EV beats the ask by ≥1¢ after
the taker fee **and** 1¢ of slippage, price in [5¢, 95¢], first qualifying
snapshot only per (market, side). Snapshots are 20, 12, 8, 4 and 2 hours before
the 5pm ET close. Events split by date: first half dev, second half
out-of-sample (OOS).

**Pass only if every one of these holds on OOS:**

- At least 150 trades across at least 25 events.
- Mean P&L ≥ +1.0¢ per contract after fee and slippage.
- Event-clustered bootstrap 2.5th percentile > 0.
- The dev half has the same sign.
- The model's Brier score is no worse than the market mid's.

**Fail = stop.** No re-tuning of the vol, offsets or thresholds to rescue it.

### Gate 1 result (run 2026-10-29): **FAIL**

67 settled 5pm events (late Aug to Oct 28), 6,335 priced quotes, 486 trades.

| | Trades / events | Mean P&L per contract | Model expected | Event-clustered 95% CI |
|---|---|---|---|---|
| Dev | 245 / 32 | −1.99¢ | +3.05¢ | [−7.37, +3.70]¢ |
| **OOS** | 241 / 33 | **−0.82¢** | +2.85¢ | [−6.74, +5.24]¢ |
| OOS buy NO | 179 / 33 | +0.37¢ | +2.92¢ | [−7.16, +7.57]¢ |
| OOS buy YES | 62 / 20 | −4.26¢ | +2.65¢ | [−21.66, +12.38]¢ |

Brier on 3,397 quotes with mid in [5, 95]¢: market 0.1353, model 0.1347.

Reading: the model is about as well calibrated as the Kalshi mid, and no better
than that. The ~3¢ gaps it flags are the spread plus noise, not mispricing.
After the taker fee and 1¢ of slippage there's nothing left. Per the rule
above: stop. No re-tuning.

What survives: the fair value tracks the market closely, so it can serve as a
quoting anchor (maker side, LIP subsidy) rather than a taker signal. That's a
different test and needs its own pre-registered rules.

## Gate 2: paper (only if Gate 1 passes)

`python -m fv.scanner scan --loop 60 --min-ttl 7200`. Two weeks, 1-lot paper
entries, graded by `python -m fv.scanner settle`. Same pass bar as Gate 1 on
the settled paper entries, with at least 10 events.

## Gate 3: live (only if Gate 2 passes)

$200 cap. 5-lot max per trade. Same daily loss rule as the futures plan. Kill on
a −$60 drawdown or if the realised mean drops below +0.5¢ after 100 fills.

## Known limits

- Hourly candle closes are top-of-book quotes; size at that price is unknown.
- The backtest centers on Coinbase spot. The live scanner uses Deribit's
  options-implied forward.
- Contracts in the same event share one BTC path, so the real sample size is
  the number of events, not the number of trades. The bootstrap resamples events.
- Montana: crypto contracts only. Sports contracts are legally exposed in the
  Ninth Circuit.
