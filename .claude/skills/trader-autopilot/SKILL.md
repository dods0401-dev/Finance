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

## Decision pass (`run`)
1. **Guards** (any one stops the pass): `autopilot.json` `enabled: false`; a `/pausa`
   on Telegram newer than any `/riprendi` (last 24h of bot messages); market closed;
   outside 09:45–15:30 New York; FOMC day before 14:30; 3 entries today; 2 losses in a
   row today; $240 lost today; an open position (one at a time).
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
   reason, Telegram card. The best candidate only.

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
