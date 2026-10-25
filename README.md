# kalshi-lip-recorder

This recorder snapshots Kalshi order books for markets in the **Liquidity Incentive Program** and scores them under the program's filed rules. The output answers one question: which subsidies can you actually collect, and which ones pay you to get picked off?

**Results: [WRITEUP.md](WRITEUP.md).** Over 69,450 snapshots, the highest-paying 20% of books had a 36% chance of moving 3¢ or more within about 5 minutes. Only 22% of snapshots were books worth quoting into.

![yield vs move](docs/yield_vs_move.png)

## What's here

| Path | What |
|---|---|
| `lip/scoring.py` | The LIP snapshot procedure from Kalshi's CFTC filing: reference price at ⅕ target, whole-level walk, stop at target, `discount^ticks`. Also `quote_share()`: what share a new bid would hold. Pure functions. |
| `lip/client.py` | A read-only REST client. It can only send GET (enforced and tested). Signing (RSA-PSS) is optional. |
| `lip/recorder.py` | The sweep loop: active programs → top N by reward rate → order books → one Parquet file per sweep. |
| `lip/ledger.py` | The live-test ledger: your fills and settlements, rewards from a CSV, each fill split into edge captured and the mid move after it. |
| `analysis/report.py` | Regenerates every table and figure in the write-up. |
| `data/` | 69,450 snapshots from 2026-09-23 to 2026-09-25 (1.8 MB, zstd Parquet). |
| `deploy/lip-recorder.service` | A systemd unit. |

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q
python analysis/report.py                          # rebuild the write-up numbers from data/
python -m lip.recorder --once --out data/live      # one live sweep
python -m lip.recorder --out data/live --markets 150 --interval 300
```

If Kalshi rejects unauthenticated order-book reads, set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (use a read-only key; the client never trades). Keep the PEM outside the repo: `.gitignore` blocks `*.pem`, `*.key` and `.env`.

## Ledger: did the live quote make money?

The ledger pulls your fills (live and `/historical`), settlements and positions with signed GETs. It marks open positions at the current mid and reports, per market:

- **Trading P&L**: cash from fills, minus fees, plus position × mark (the settlement value once settled).
- **LIP rewards**: Kalshi's API has no per-account reward endpoint, so copy the payouts from the app into a CSV.
- **Edge** = qty × (mid just before the fill − fill price). This is what the quote captured.
- **Adverse** = qty × (mid N minutes later − mid before), from 1-minute candlesticks. Negative means you got picked off.
- **Taker fills**: the fills that crossed the spread (the Day 0 mistake).
- **Kalshi fees / realized**: Kalshi's own numbers from `/portfolio/positions`. If the fees disagree with the ledger's, trust Kalshi and find out why.

```bash
cat > rewards.csv <<'CSV'
date,ticker,amount_dollars,note
2026-09-28,KXRT-EXAMPLE,1.84,
2026-09-29,,0.40,lump sum without a market
CSV
python -m lip.ledger --since 2026-09-27 --rewards rewards.csv --out ledger.md   # needs a signed key
python -m lip.ledger --offline --rewards rewards.csv --markout-min 30           # re-run from the cache
```

Raw responses are cached in `data/ledger/` (gitignored, since it's your account data), along with `fills_marked.csv` (one row per fill with the mids around it). Everything is in YES terms: buying NO at q counts as selling YES at 1 − q, which has the same P&L.

## Schema

There is one row per market per sweep. The column suffix gives the unit: `_e4` = dollars × 10⁴, `_e2` = contracts × 100, `_cents`, `_bps`.

| Column | Meaning |
|---|---|
| `yes_best_e4`, `no_best_e4` | Best bid on each side |
| `yes_ref_e4`, `yes_ref_gap_ticks` | The reference price, and how many ticks it sits below the best bid |
| `*_depth_total_e2` / `*_depth_scoring_e2` / `*_score_e2` | All resting size / size inside the qualifying levels / discounted score |
| `qualifies_scoring` | Both sides reach the target size, so the snapshot pays |
| `quote_size_e2` | Hypothetical quote: target size, joining the best bid on each side |
| `share_bps` | min(yes share, no share) of that quote. Conservative; this is what v1 recorded |
| `share_avg_bps` | (yes share + no share) / 2, the fraction of the snapshot reward per the filing (v2 only) |
| `capital_cents` | Cost to post the quote on both sides |
| `subsidy_cents_per_hour` / `subsidy_avg_cents_per_hour` | Reward rate × `share_bps` / × `share_avg_bps` |

The included dataset came from v1. It lacks the `*_avg_*` columns, and its depth and score columns didn't stop at target size (details in the write-up).

## License

MIT
