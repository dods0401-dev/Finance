---
name: trader-playbook
description: >-
  The user's own trading rules and a curated knowledge base of day-trading best
  practices (sourced from research, financial podcasts and the financial press).
  Read it BEFORE any /trader-* skill, before proposing or sizing a trade, and when
  the user asks "what are my rules", "is this a good trade", "best practices",
  "what do the experts say", "pre-market checklist", "post-trade review", or adds
  a new rule/lesson ("add to the playbook", "remember this rule"). It can only make
  the day-trader-skill pack stricter, never looser.
---

# trader-playbook

Extends the day-trader-skill pack without editing it. Three files:

| file | what |
| --- | --- |
| `rules.md` | the user's own hard rules (capital, risk, daily stop, hours). Binding. |
| `knowledge.md` | best practices with their source and how strong the evidence is |
| `checklists.md` | pre-market, pre-entry and post-trade checklists |

## How to use it

1. Before `/trader-scanner`, `/trader-entry`, `/trader-budget`, `/trader-risk` or
   `/trader-broker`: read `rules.md` and apply it. If a rule here is stricter than
   `trader/config.json`, the rule here wins and you say so.
2. Before asking the user for a "yes" on an order, walk `checklists.md`
   (pre-entry) and show any item that fails. A failed hard rule = do not offer the order.
3. After a trade closes, run the post-trade checklist and add a `lesson` with
   `/trader-journal`.
4. When the user adds a rule or a lesson, append it to `rules.md` or
   `knowledge.md` with the date and, for knowledge, the source. Never delete a
   user rule without them asking. Commit and push the change.
5. When adding knowledge from a podcast, article or book: summarise in your own
   words, name the source (show, episode, date, URL), and rate the evidence:
   **research** (peer-reviewed / regulator data), **practitioner** (experienced
   trader's rule of thumb), **opinion**. Never copy paywalled text.

## Never

- Never loosen a limit from `rules.md` because a source says otherwise; propose
  the change to the user instead.
- Never present a podcast opinion as a fact or a forecast.
- This is education and discipline, not financial advice.
