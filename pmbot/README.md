# pmbot: Polymarket US tools

**Status: a source watcher (logs only) plus a rewards-MM strategy core and backtest. Nothing here places orders.**

## Watcher: be first to the resolution source

`python -m pmbot.watch` polls the sources tech, finance and culture markets resolve on. It logs every change next to the Polymarket US price fetched right after it, in `signals.jsonl`. It also snapshots every watched market each minute, in `prices-*.jsonl`. Run it for a couple of weeks before trading: the log shows how long a market takes to price a source change, and that lead time is the whole edge.

| Source | Markets | Why |
|---|---|---|
| SEC EDGAR full-text search, filtered by filer (`entityName`) | IPO markets: `2026ipos`, `anthropic`, `openai` | Resolve on S-1 effectiveness (EFFECT), the 424B4, or an exchange listing approval. All are indexed. |
| `arena.ai/leaderboard/text/overall-no-style-control` | "#1 AI model" markets | The named resolution source |
| Hugging Face new repos by lab | AI model markets | Open-weight launches show up here first |
| anthropic.com/news, openai.com/news | "Claude 6 / GPT-x released" | Resolve on the lab's own release |

Edit `watchlist.json` to add sources. `deploy/pmbot-watch.service` runs the watcher under systemd. Set `PMBOT_UA="pmbot you@example.com"`, because the SEC asks for a contact in the User-Agent.

What the IPO rules mean in practice: a non-EGC filing confidentially (DRS) must file its S-1 publicly at least 15 days before the roadshow starts. No public S-1 means no IPO for at least ~3 weeks. That deadline math prices the "IPO by date X" markets.

## Rewards market making (shelved)

The Polymarket US Liquidity Incentive Program pays for resting size near the top of the book. The formula is close to Kalshi's: a random snapshot every second, `size × discount^ticks`, and a target size per side. So everything learned in `WRITEUP.md` carries over, including the bad news.

### Backtest: the subsidy doesn't cover the fills

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

### Layout

| File | What |
|---|---|
| `scoring.py` | Polymarket US LIP scoring: best-price anchor, a walk that stops at target size, and the book-wide max-spread test. Matches the docs' examples. |
| `strategy.py` | `evaluate()` filters one book into a quote or a list of skip reasons. `allocate()` applies capital caps (never shrinks a quote below target, since that earns nothing). `killed()` is the daily-loss kill switch. |
| `auth.py` | Ed25519 request signer (`X-PM-*` headers, `timestamp + METHOD + path`), checked against the docs. |
| `watch.py`, `watchlist.json` | The watcher above. |
| `backtest.py` | The table above. |

## Network

The cloud environment needs these hosts allowed: `gateway.polymarket.us`, `api.polymarket.us`, `docs.polymarket.us`, `efts.sec.gov`, `huggingface.co`, `arena.ai`, `www.anthropic.com`, `openai.com`. Unreachable sources log a warning and are skipped.
