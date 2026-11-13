---
name: trader-journal
description: >-
  Trade diary and Telegram alerts with the rationale. Every Alpaca order Claude
  places, cancels, replaces or closes is logged automatically (a PostToolUse hook)
  with its reason, plan and Time Machine verdict, and sent to Telegram. A
  scheduled monitor reports fills (stop/target hits) and open positions while the
  user is away. Use when the user says "journal", "log this trade", "add a
  lesson", "weekly review", "how am I doing", "send me a test alert", "set up
  Telegram", or after a trade closes.
---

# trader-journal

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" review --days 7       # last week's entries
python3 "${CLAUDE_SKILL_DIR}/journal.py" notify "test"          # Telegram test
python3 "${CLAUDE_SKILL_DIR}/journal.py" chat-id                # find your chat id (setup)
python3 "${CLAUDE_SKILL_DIR}/journal.py" monitor --minutes 65   # fills since, read only
python3 "${CLAUDE_SKILL_DIR}/journal.py" morning               # morning briefing
python3 "${CLAUDE_SKILL_DIR}/journal.py" perf --period week     # performance (day|week|month|all)
python3 "${CLAUDE_SKILL_DIR}/journal.py" eod                   # end-of-day report
python3 "${CLAUDE_SKILL_DIR}/journal.py" --dry-run morning     # print instead of sending
```

The hook (`.claude/settings.json`, PostToolUse on `mcp__alpaca__place_stock_order`,
`cancel_order_by_id`, `replace_order_by_id`, `close_position`) runs `journal.py hook`.
It writes `trader/journal/journal.jsonl` and sends the Telegram message. It never
blocks or changes an order.

## Telegram messages (HTML, in Italian)
- **ACQUISTO** (hook, buy order): quantity, entry, stop, target, $ risk and % of the
  $6,000 basis, R:R, position value, setup, why, Time Machine verdict, flat-by time,
  today's count (trades n/3, P/L, losses vs the $240 daily limit).
- **ESEGUITO** (monitor, buy fill): fill price and time, stop/target, why.
- **TARGET / STOP / USCITA** (monitor, position back to flat): P/L in $ and R,
  entry → exit, duration, today's line, equity on the $6,000 basis, stop warning
  after 2 losses in a row or the daily loss limit.
- **POSIZIONI APERTE**, **PERFORMANCE** (day/week/month/all: P/L, W/L, win rate,
  average R, profit factor, best/worst, max drawdown, drawdown brake),
  **BUONGIORNO** (session hours in Italian time, futures, VIX, 10Y, oil, gold,
  EUR/USD, BTC, movers >= $5, watchlist, account, today's rules, checklist),
  **CHIUSURA** (today's trades + performance + open-position warning).
Performance is rebuilt from Alpaca fills (round trips flat->long->flat, FIFO); R uses
the plan's max_loss, else entry-stop, else the config max loss per trade.

## After each trade
1. Add the outcome and a one-sentence lesson: append a JSON line to
   `trader/journal/journal.jsonl` with `{"ts", "event": "lesson", "symbol",
   "result_r", "followed_plan": true/false, "lesson"}`.
2. Commit and push `trader/journal/` so the scheduled monitor (a fresh cloud
   session) can find the reason behind each fill.

## Weekly review
`review --days 7`, then summarise: trades, win rate, average R, rule breaks,
the lesson that repeats. Propose (never apply) rule changes to `trader-playbook/rules.md`.

## Setup: Telegram
1. On Telegram, talk to @BotFather → /newbot → copy the token.
2. Add it as environment variable `TELEGRAM_BOT_TOKEN` in the cloud environment
   settings (never in chat). Send any message to the new bot.
3. In a new session: `journal.py chat-id`, then set `TELEGRAM_CHAT_ID` the same way.
4. `journal.py notify "test"`.
