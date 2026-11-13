# My rules (binding)

Last updated: 2026-11-13. Paper trading on Alpaca until I decide otherwise.

## Main strategy: monthly portfolio (decided 2026-11-13, after the 10-year backtests)
- Capital: the **$6,000** basis plus everything gained or lost since the start.
- **50% in the S&P 500 index (SPY)**, held.
- **50% split equally across the 10 most traded stocks (over $100M a day, price over $10)
  that rose most over the last 12 months, skipping the last month.**
- Rebalanced once a month, on the first trading day, at about 10:05 New York. Positions are
  held overnight and for weeks; no stop and no target (the backtests showed stops and market
  filters cut the return more than the losses).
- Expect swings: in the 2022–2026 test the worst fall from a peak was about −33%.
- No margin, no short selling, no options, no crypto.

## Day trading (switched OFF on 2026-11-13: no edge after costs in the backtests)
The rules below apply only if day trading is switched back on.

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
