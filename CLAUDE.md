# Finance: day trading lab (paper)

- Skills: `.claude/skills/trader-*` are the upstream day-trader-skill pack
  (github.com/Jakeschincariol/day-trader-skill, MIT). Do not edit them; to update,
  re-copy the upstream folders. Our extensions are `trader-playbook` and `trader-journal`.
- **Before any /trader-* skill, read `.claude/skills/trader-playbook/rules.md`.**
  Its rules are binding and only ever stricter than `trader/config.json`.
- Broker: Alpaca **paper** via the `alpaca` MCP server (`.mcp.json`). Keys are
  injected by the environment's network proxy: never ask for, print or store them.
  Never switch to live.
- Manual orders (placed in a chat) always need the user's explicit "yes" plus the permission card.
- Exception (user decision 2026-11-13): trader-autopilot may place orders on the PAPER
  account by itself, only from its scheduled tasks, only via autopilot.py, within all
  limits of rules.md, reporting every decision to Telegram. Kill switch: /pausa on
  Telegram or "enabled": false in .claude/skills/trader-autopilot/autopilot.json. Never live.
- After each trade: lesson in the journal, then commit and push `trader/journal/`.
- Reply to the user in Italian.
