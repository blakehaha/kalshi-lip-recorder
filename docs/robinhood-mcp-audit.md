# Robinhood MCP audit (step 1)

Date: 2026-11-01. Mode: read-only. No orders placed, previewed or cancelled.

## Account access

- Six brokerage accounts are visible. The agent can trade exactly one: the cash account nicknamed "MCP" (ends 7235), options level 2. Every other account is read-only to the agent.
- **`human_must_approve_trades` = false on the MCP account.** Any agent with this connector can place live orders with no human click. Turn approvals ON in the Robinhood app before step 6. No tool can change this setting; only the app can.

## Tools tested (worked)

| Tool | Result |
|---|---|
| `get_accounts` | 6 accounts, 1 agent-tradable |
| `get_trade_approval_setting` | approvals off (see above) |
| `get_portfolio` | value, cash, buying power |
| `get_equity_positions` | symbol, qty, avg cost |
| `get_option_positions` | empty (no open options) |
| `get_equity_quotes` | bid/ask/last with venue timestamps, plus official prior close |
| `get_earnings_calendar` | 996 high-cap events in a 7-day window. The response is 146 KB, so it needs a ticker filter or a narrower window |

## Tools listed but not yet tested

**Market data (read):** `get_equity_historicals`, `get_equity_price_book`, `get_equity_technical_indicators`, `get_equity_fundamentals`, `get_financials`, `get_equity_analyst_ratings`, `get_earnings_results`, `get_index_quotes`, `get_index_historicals`, `get_indexes`, `get_crypto_quotes`, `get_currency_pairs`, `search`, `get_equity_tradability`

**Options data (read):** `get_option_chains`, `get_option_instruments`, `get_option_quotes`, `get_option_historicals`, `get_option_watchlist`

**Filings / alternative data (read):** `get_sec_filing`, `get_sec_filing_index`, `get_sec_filing_facts`, `get_sec_filing_facts_catalog`, `get_politician_trades`

**Account history (read):** `get_equity_orders`, `get_option_orders`, `get_crypto_orders`, `get_crypto_positions`, `get_equity_tax_lots`, `get_pnl_trade_history`, `get_realized_pnl`, `get_trade_approvals`

**Scanners and alerts (these write account state but don't trade):** `create_scan`, `run_scan`, `preview_scan`, `update_scan_*`, `get_scans`, `get_scanner_*`, `create_alert`, `update_alert`, `delete_alert`, `get_alerts`, `get_alert_log`, `mark_alerts_read`

**Charts and watchlists (these write account state but don't trade):** `get_legend_*`, `add/remove/update_legend_indicator`, `set_legend_chart_settings`, watchlist CRUD, `follow/unfollow_watchlist`, `get_popular_watchlists`

**Order previews (should not execute, unverified):** `review_equity_order`, `review_option_order`, `preview_crypto_order`

## Money-moving tools: deny these until step 6

`place_equity_order`, `place_option_order`, `place_crypto_order`, `exercise_option`, `cancel_equity_order`, `cancel_option_order`, `cancel_crypto_order`, `cancel_option_exercise`, `decline_trade_approval`

Add this to `.claude/settings.json` in whatever project runs the portfolio session:

```json
{
  "permissions": {
    "deny": [
      "mcp__robinhood__place_equity_order",
      "mcp__robinhood__place_option_order",
      "mcp__robinhood__place_crypto_order",
      "mcp__robinhood__exercise_option",
      "mcp__robinhood__cancel_option_exercise"
    ]
  }
}
```

## What this connector can't do

- **No paper trading mode.** Step 5 needs a local signal ledger. Don't put "paper" orders through a live account.
- **No IPO calendar, lockup or float tool.** `get_sec_filing*` can pull S-1/424B filings by ticker, but you need another source to find new IPOs in the first place.
- **No news feed.**
- **No implied-volatility history surface** unless `get_option_historicals` turns out to include it (untested). Without it, the backtest can't model IV crush.
- **No backtester.** Historical bars come from `get_equity_historicals`; fees, spread and slippage have to be modelled locally.
- No futures or event-contract tools, even though the portfolio response has fields for them.

## Next checks

1. Call `get_option_chains` and `get_option_quotes` on one liquid name. Confirm whether greeks and IV are returned.
2. Call `get_option_historicals`. Does it include IV, or only price?
3. Call `get_equity_historicals`. What's the deepest lookback and the finest interval?
4. Call `review_equity_order` on a 1-share limit far from the market, then confirm with `get_equity_orders` that nothing was placed. Only then trust the "review" tools as non-executing.
