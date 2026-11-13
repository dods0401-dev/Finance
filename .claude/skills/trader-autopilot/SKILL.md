---
name: trader-autopilot
description: >-
  Autonomous, rules-based day trading on the Alpaca PAPER account. Hourly scheduled
  passes scan, wait for an opening-range breakout or a close above yesterday's high
  on daily setups, skip earnings days (SEC 8-K), plan the exit at the live price,
  replay it 10,000 times with the Time Machine, size it, run the risk check and send
  one bracket order (stop + target at Alpaca), reporting every decision to Telegram.
  A 15:50 pass flattens everything. Use when the user says "autopilot", "trade by
  yourself", "pause the bot", "resume", "why didn't it trade", "autopilot status".
---

# trader-autopilot

```bash
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" status                           # guards + today's state
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run --dry-run                    # decide, never order
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run --no-telegram --now "2026-11-11 10:52"   # replay a past moment
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run                              # live pass (scheduled task)
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" flatten                          # cancel orders, close positions
```

## Main mode: monthly portfolio (`rebalance`)
```bash
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" rebalance --dry-run --no-telegram   # preview
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" rebalance --if-first-day          # scheduled, 10:05 NY
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" health --no-telegram              # monthly time machine
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" outlook --no-telegram             # 10,000 possible next months
```
Capital: $10,000 basis. 50% in SPY, 50% equally across the 10 most traded stocks (20-day average over $100M, price
over $10) with the highest 12-month return skipping the last month. Portfolio value = the
$6,000 basis + (account equity − $100,000), so it needs no stored state. Sells first, then
buys with dollar (fractional) market orders; differences under $25 are skipped. Plain card
on Telegram, ending with the health check: the same rules replayed month by month over the
last 12 months vs the S&P 500 alone (warns after 3 months in a row behind; changes nothing by
itself), and the outlook: 10,000 simulated next months drawing random days of the last year
for all holdings at once (real swings), with each stock's own past trend replaced by a prudent
15%/year drift (`outlook_annual_return`), shown as typical result, 9-in-10 range, chance of a
losing month and a 1-in-100 bad case. The outlook also goes out every Friday evening.
Settings in `autopilot.json` → `portfolio`. Chosen on 2026-11-13 from the
10-year backtests (`trader-research/backtest.py momentum --min-dv 1e8 --blends`).

## Day trading (`run`, `flatten`): switched off (`"day_trading": false`)
Kept for reference; the backtests found no edge after costs.

## Decision pass (`run`)
1. **Guards** (any one stops the pass): `autopilot.json` `enabled: false`; a `/pausa`
   on Telegram newer than any `/riprendi` (last 24h of bot messages); market closed;
   outside 09:45–15:30 New York; FOMC day before 14:30; max_trades_per_day entries today;
   max_losses_in_a_row losses in a row today; $240 lost today; max_open_positions (5) already open.
2. **Room for today** from Alpaca: max loss per trade = min($120, or $60 under the
   drawdown brake, the daily room left after today's losses and the open stop risk).
3. **Candidates**: `trader/watchlist.json` from `scan.py daily` (run if missing), the
   40 most traded hits at $10 or more, not traded today.
4. **Trigger**: opening-range breakout (`entry.py check`), or for breakout_20d /
   bull_flag / reclaim_ma20 hits a 5-minute close above yesterday's high.
5. **Earnings filter**: no trade on the day after an 8-K item 2.02, or within −4/+1
   days of the estimate (same quarter a year earlier + 364 days).
6. **Plan at the live price**: stop at the opening-range midpoint (orb) or 1×ATR
   (level); targets 1.5R and 2R. No chasing: skip if the price is more than 0.25R
   above the trigger.
7. **Time Machine** for each target; keep the better average R. Trade only if the
   verdict is "toward the target", the simulated average R is ≥ +0.05, and (orb, 8+
   past cases) the setup's history has a positive average R. Coin flips are skipped.
8. **Budget** (`size.py`) and **risk check** (`check.py`) with today's config; PASS only.
9. **Order**: market bracket (stop and target live at Alpaca), journal entry with the
   reason, Telegram card. Best candidates first, one per free slot; each sized on free
   cash with a 1/5-of-capital cap, the daily loss room recomputed after each order.

Every pass sends a Telegram summary of what it checked and why it did or did not trade.

## Settings: `autopilot.json` (in git)
`enabled`, `max_candidates`, `min_price`, `max_open_positions`, `min_tm_avg_r`,
`min_history_trades`, `max_chase_r`, `targets`, `orb_stop`, `level_stop`. Only
stricter than `trader-playbook/rules.md`, never looser.

## Kill switches
- Telegram: send `/pausa` to the bot (resume with `/riprendi`). Holds for 24h of
  bot history; for a longer stop set `"enabled": false` in `autopilot.json` and push.
- `flatten` closes everything at once.

Paper only: the endpoint is hard-coded and asserted. Never point it at live.
