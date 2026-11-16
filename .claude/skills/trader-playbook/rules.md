# My rules (binding)

Last updated: 2026-11-13. Paper trading on Alpaca until I decide otherwise.

## Six strategies in parallel (decided 2026-11-15, Day lungo added 2026-11-16, test of at least 1 month, verdict after 3–6)
Paper account only. Each strategy has its own budget and its own books (orders tagged by
strategy); together $45,000 of the $100,000 paper account.

| # | Strategy | Budget | Rules |
|---|---|---|---|
| s1 | Bilanciato | $10,000 | 50% SPY + 50% top-10 momentum, monthly (below) |
| s2 | Aggressivo | $10,000 | 50% SSO (S&P 500 ×2) + 50% top-10 momentum, monthly |
| dt | Day trading | $10,000 | the day-trading rules below; flat every evening (its own shares only) |
| dl | Day lungo | $5,000 | same entry signals as day trading, but no fixed target: a protective stop (good until cancelled) is raised every hour to the highest price since entry minus the initial risk; at 15:40 New York it keeps a position overnight only if it is in profit, the S&P 500 is above its 200-day average, the stock is less than 10% above its 20-day average, no earnings are due and no negative news; otherwise it sells; at most 5 trading days |
| sh | Short | $5,000 | short a liquid stock hit by severe news and already falling, hedged by buying its sector ETF for the same amount; max 4 at a time, $1,000 each; stop if it rises 8%; closed after 5 trading days. Never a stock another strategy holds |
| op | Opzioni | $5,000 | buy calls on the 2 strongest sector ETFs of the last month and a put on the weakest (if falling); 30–60 days, about 2% out of the money; max $600 premium each (= max loss), max 4 open; sell at +100%, at −50% or 10 days before expiry; new entries Mondays and Thursdays |

- Any strategy that loses **20% of its budget stops by itself** and says so on Telegram.
- Pause from Telegram (phone or watch): «pausa» (all), «pausa short», «riprendi», «riprendi short».
- Short selling and options are allowed **only** for the sh and op strategies, within these limits.
- Weekly league table on Friday; daily table every evening.

## Main strategy: monthly portfolio (decided 2026-11-13, after the 10-year backtests)
- Capital: the **$10,000** basis (raised from $6,000 on 2026-11-13) plus everything gained or
  lost since the start.
- **50% in the S&P 500 index (SPY)**, held.
- **50% split equally across the 10 most traded stocks (over $100M a day, price over $10)
  that rose most over the last 12 months, skipping the last month.**
- Rebalanced once a month, on the first trading day, at about 10:05 New York. Positions are
  held overnight and for weeks; no stop and no target (the backtests showed stops and market
  filters cut the return more than the losses).
- Expect swings: in the 2022–2026 test the worst fall from a peak was about −33%.
- Monthly health check after each rebalance: the same rules replayed on the last 12 months
  versus the S&P 500. If the strategy trails the index for 3 months in a row, the user is
  warned and we review it together (nothing changes automatically).
- No margin, no crypto (short and options only inside the sh and op strategies).

## Day trading (strategy dt, switched back on 2026-11-16 as one of the five)
The backtests found no edge after costs: this is a live test, not a bet on it.

## Capital and risk
- Training capital: **$6,000** (the paper account holds $100,000; size as if it were $6,000).
- Risk per trade: **2% = $120** max loss at the stop.
- Daily loss limit: **4% = $240**. Hit it, and the day is over.
- **Five losing trades in a row = stop for the day** (no revenge trading).
- Max **20 trades per day**, at most **5 positions open at the same time**: the capital is
  split in 5 slots, so one position is at most 1/5 of it ($1,200).
- Drawdown brake: if equity (on the $6,000 basis) falls below **$5,400 (-10%)**,
  risk per trade drops to **1% ($60)** until it is back above $6,000.
- Reward to risk at least **1.5R** at entry.
- No margin, no short selling, no options, no crypto.

## Time
- No new entries before 09:45 or after 15:30 New York time (15:45–21:30 Italy).
- Day trades are flat by 15:55 New York (the monthly portfolio is held).
- No trade in a stock on its earnings day or the day before.
- On big macro days (CPI, Fed decision, jobs report) wait until 30 minutes after the release.

## Process
- Every order has a stop and a target before it is sent.
- Run `/trader-timemachine` before every trade; a "coin flip" verdict needs an extra reason to trade.
- Every trade gets a journal entry with the reason, and a lesson after it closes.
