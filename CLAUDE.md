# Finance: day trading lab (paper)

- Skills: `.claude/skills/trader-*` are the upstream day-trader-skill pack
  (github.com/Jakeschincariol/day-trader-skill, MIT). Do not edit them; to update,
  re-copy the upstream folders. Our extensions are `trader-playbook` and `trader-journal`.
- **Before any /trader-* skill, read `.claude/skills/trader-playbook/rules.md`.**
  Its rules are binding and only ever stricter than `trader/config.json`.
- Broker: Alpaca **paper** via the `alpaca` MCP server (`.mcp.json`). Keys are
  injected by the environment's network proxy: never ask for, print or store them.
  Never switch to live.
