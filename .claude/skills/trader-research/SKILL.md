---
name: trader-research
description: >-
  Research layer of the day-trading lab: the full liquid US universe (stocks + ETFs,
  NYSE/NASDAQ/AMEX/ARCA) with SEC sectors, 10 years of Alpaca SIP price history, and a
  daily context diary that stores market regime, sector strength, the day's news by
  sector and Claude's qualitative assessment (tone, trend-change risk, deep-dive flags,
  hold bias for open positions), then scores those assessments against what happened.
  Use when the user says "contesto", "settori", "cluster", "quale settore approfondire",
  "notizie per settore", "come sta il mercato", "storico", "backtest", "research".
---

# trader-research

```bash
python3 "${CLAUDE_SKILL_DIR}/data.py" universe                 # -> research/universe.csv (+ sectors.json cache)
python3 "${CLAUDE_SKILL_DIR}/data.py" bars --timeframe 1Day    # 2016-today, research/cache/ (git-ignored)
python3 "${CLAUDE_SKILL_DIR}/context.py" snapshot              # -> research/context/DATE.json + digest
python3 "${CLAUDE_SKILL_DIR}/context.py" assess DATE --from assessment.json   # + Telegram card
python3 "${CLAUDE_SKILL_DIR}/context.py" evaluate              # did the assessments predict the next 5 days?
python3 "${CLAUDE_SKILL_DIR}/context.py" show [DATE]
```

## The daily context diary (scheduled after the close)
1. `context.py snapshot` writes the data part and prints a digest.
2. Claude reads the digest, `trader-playbook/market-context.md` (the 8-hour press and
   podcast refresh) and, if useful, a few headlines in full, then writes the
   assessment JSON (schema in `context.py`) **from the evidence, not from opinion**:
   - `market.tone` −2..2, `regime_change_risk` basso|medio|alto, `summary` (1–2 lines);
   - for **every** sector: `tone`, `trend_change_risk`, `deep_dive` (true when news flow,
     relative volume or a turn in the 5/20-day numbers make it worth a closer look),
     `drivers` (what moves it, citing headlines/data), `why`;
   - for each open position: `hold_bias` tenere|chiudere|neutro and why (news after the
     close, sector tone, earnings, extension);
   - `watch`: up to 10 tickers worth a look tomorrow.
3. `context.py assess DATE --from FILE` validates, stores and sends the 🧭 card.
4. Commit and push `research/context/`.

The assessments are a **hypothesis log**: `evaluate` measures over time whether a positive
tone beat the median sector over the next 5 days. Only what proves useful there may later
feed the autopilot (sector tilt, overnight hold). Until then they inform, they do not trade.

## Files
| path | what | git |
|---|---|---|
| `research/universe.csv` | liquid listed stocks + ETFs: sector, price, 20d $ volume | yes |
| `research/sectors.json` | SEC SIC cache | yes |
| `research/context/DATE.json` | daily data + assessment | yes |
| `research/cache/` | price history | no |
