# Diesel LIP maker test: rules written before results

*Written 2026-10-29.*

## Why diesel

- **Taking prices on gas or diesel loses.** On 65 daily gas events, the Kalshi market's day-ahead forecast missed by 0.77¢ RMSE. The best public-data model (momentum plus RBOB futures) missed by 1.42¢. Someone quoting there has station-level data. Don't take.
- **The year-end diesel markets are a subsidy with little flow.** That covers KXDIESELMAXY, KXDIESELMINY and KXDIESELYE, closing 2026-12-31.
  - Each LIP program pays $100 a week per market, with a 1,000-contract target size.
  - Over the last 14 days most of these books traded under 100 contracts a day. The median daily mid move was 1–3¢, and the worst single day was 23.5¢.
  - In a snapshot on 2026-10-29, a target-size quote at the touch would have held about 99% of the score on the best few markets. That's about $14 a day per market on roughly $950 of resting capital. Across all 58 diesel programs the median share was 37%.
  - These series charge no maker fee.

One snapshot proves nothing, which is why this is a test.

## Step 1: watch (3 days, $0)

```bash
python -m lip.recorder --out data/diesel --prefix KXDIESEL --interval 600
```

Proceed only if at least 3 markets keep share ≥ 60% in at least 70% of
sweeps, and their recorded `subsidy_avg_cents_per_hour` averages ≥ 40¢/hr each.

## Step 2: live, 2 program weeks

- Use the 2–3 markets that pass step 1, with mids between 15¢ and 85¢.
- Join the best bid on both sides at target size, or less if capital is short.
  Check that the side still reaches target size, because a side short of
  target pays nothing. Never improve price, since that buys fills, not score.
- Check twice a day and re-join the touch if it moved.
- **Pull everything** if heating-oil futures (HO) move more than 4% in a session, or on
  refinery or supply headlines. Re-post the next day at the new touch.
- Hold no more than 200 contracts net per market. Past that, pull the side that
  keeps getting filled.
- Log payouts (Kalshi rewards), fills, and mark-to-market every day.

**Pass:** net (rewards + realised + mark-to-market − fees) ≥ +$5 a day per
market over the 2 weeks, and no single day loses more than $150.
**Kill:** net negative after week 2, or one day loses more than $150. No exceptions.
