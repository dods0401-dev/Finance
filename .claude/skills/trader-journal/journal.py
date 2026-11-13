#!/usr/bin/env python3
"""trader-journal: trade diary + Telegram alerts with the rationale.

Commands
  hook              PostToolUse hook: reads Claude Code's hook JSON on stdin after
                    an Alpaca order tool runs, writes a journal entry with the
                    rationale (orders.json "why", plan.json, timemachine verdict)
                    and sends it to Telegram. Never blocks, never fails the tool.
  monitor           Reads Alpaca (paper) fills from the last --minutes and the open
                    positions, matches each fill to its journal entry and sends one
                    Telegram message per new fill. Read only: never places orders.
  notify TEXT       Sends TEXT to Telegram.
  chat-id           Prints the chat ids that wrote to your bot (setup helper).
  review [--days N] Summary of the journal: trades, outcome, rule breaks noted.

Environment
  TELEGRAM_BOT_TOKEN   bot token from @BotFather (environment variable, never in chat)
  TELEGRAM_CHAT_ID     your chat id (from `chat-id`)
  Alpaca keys are injected by the environment's network proxy; the header values
  sent here are placeholders.

Standard library only. Files live in trader/ at the project root.
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
TRADER = os.path.join(ROOT, "trader")
JOURNAL = os.path.join(TRADER, "journal", "journal.jsonl")
ALPACA = "https://paper-api.alpaca.markets"
PLACEHOLDER_HEADERS = {"APCA-API-KEY-ID": "PKPLACEHOLDERPROXYINJECTS",
                       "APCA-API-SECRET-KEY": "placeholder-proxy-injects"}
ORDER_TOOLS = {"place_stock_order": "ORDINE INVIATO", "cancel_order_by_id": "ORDINE ANNULLATO",
               "replace_order_by_id": "ORDINE MODIFICATO", "close_position": "POSIZIONE CHIUSA"}


# ---------------------------------------------------------------- helpers
def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def journal_entries():
    out = []
    try:
        with open(JOURNAL) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def append_journal(entry):
    os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token,
                                    data=data, timeout=15) as r:
            return "sent" if json.load(r).get("ok") else "telegram error"
    except Exception as e:  # never let an alert break the trade flow
        return "telegram failed: %s" % type(e).__name__


def alpaca_get(path, params=None):
    url = ALPACA + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=PLACEHOLDER_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def rationale_for(symbol, side, qty):
    """The why, plan and Time Machine verdict for this order, from trader/."""
    why, plan = None, None
    orders = (load(os.path.join(TRADER, "orders.json"), {}) or {}).get("orders") or []
    for o in orders:
        if o.get("symbol") == symbol and o.get("side") == side and str(o.get("qty")) == str(qty):
            why, plan = o.get("why"), o.get("plan")
            break
    if plan is None:
        p = load(os.path.join(TRADER, "plan.json"), {}) or {}
        if p.get("symbol") == symbol:
            plan = {k: p.get(k) for k in ("entry", "stop", "target", "setup", "flat_by", "stop_label", "target_label")}
    tm = load(os.path.join(TRADER, "timemachine.json"), {}) or {}
    verdict = tm.get("verdict") if (tm.get("plan") or {}).get("symbol") in (symbol, None) else None
    return why, plan, verdict


def fmt_plan(plan):
    if not plan:
        return ""
    parts = []
    for k, label in (("entry", "entrata"), ("stop", "stop"), ("target", "target"), ("max_loss", "perdita max $")):
        if plan.get(k) is not None:
            parts.append("%s %s" % (label, plan[k]))
    return ", ".join(parts)


# ---------------------------------------------------------------- commands
def cmd_hook(_args):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = (payload.get("tool_name") or "").split("__")[-1]
    if tool not in ORDER_TOOLS:
        return 0
    ti = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    resp_text = json.dumps(resp, ensure_ascii=False)[:600] if resp is not None else ""
    symbol, side, qty = ti.get("symbol"), ti.get("side"), ti.get("qty")
    why, plan, verdict = rationale_for(symbol, side, qty) if symbol else (None, None, None)
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("id") or resp.get("order_id")
    entry = {"ts": now_utc().isoformat(timespec="seconds"), "event": tool, "paper": True,
             "symbol": symbol, "side": side, "qty": qty, "input": ti, "order_id": order_id,
             "why": why, "plan": plan, "timemachine": verdict, "response": resp_text}
    append_journal(entry)
    lines = ["%s (PAPER)" % ORDER_TOOLS[tool]]
    if symbol:
        lines.append("%s %s %s" % ((side or "").upper(), qty or "", symbol))
    if plan:
        lines.append(fmt_plan(plan))
    if why:
        lines.append("Motivo: " + why)
    if verdict:
        lines.append("Time Machine: " + verdict)
    if tool != "place_stock_order" and ti:
        lines.append(json.dumps(ti, ensure_ascii=False)[:300])
    status = telegram("\n".join(lines))
    print(json.dumps({"systemMessage": "trader-journal: logged, Telegram %s" % status}))
    return 0


def cmd_monitor(args):
    after = (now_utc() - dt.timedelta(minutes=args.minutes)).isoformat(timespec="seconds")
    try:
        fills = alpaca_get("/v2/account/activities/FILL", {"after": after, "direction": "asc"})
        positions = alpaca_get("/v2/positions")
    except Exception as e:
        print("alpaca read failed: %s" % e)
        return 1
    by_order = {e.get("order_id"): e for e in journal_entries() if e.get("order_id")}
    sent = 0
    for f in fills or []:
        j = by_order.get(f.get("order_id")) or {}
        lines = ["ESEGUITO (PAPER): %s %s %s @ %s" % (f.get("side", "").upper(), f.get("qty"),
                                                      f.get("symbol"), f.get("price")),
                 "ora: %s" % f.get("transaction_time")]
        if j.get("why"):
            lines.append("Motivo: " + j["why"])
        elif f.get("side") == "sell":
            lines.append("Motivo: uscita (stop, target o chiusura di fine giornata)")
        else:
            lines.append("Motivo: non trovato nel diario (ordine fatto fuori da Claude?)")
        if j.get("plan"):
            lines.append(fmt_plan(j["plan"]))
        print(telegram("\n".join(lines)))
        sent += 1
    if positions and args.positions:
        rows = ["%s %s @ %s, P/L %s $" % (p.get("qty"), p.get("symbol"), p.get("avg_entry_price"),
                                          p.get("unrealized_pl")) for p in positions]
        print(telegram("POSIZIONI APERTE (PAPER):\n" + "\n".join(rows)))
    print("fills: %d, open positions: %d" % (sent, len(positions or [])))
    return 0


def cmd_notify(args):
    print(telegram(" ".join(args.text)))
    return 0


def cmd_chat_id(_args):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set")
        return 1
    with urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % token, timeout=15) as r:
        data = json.load(r)
    seen = {}
    for u in data.get("result") or []:
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not seen:
        print("No messages yet: send any message to your bot on Telegram, then run this again.")
    for cid, name in seen.items():
        print("chat id %s  (%s)" % (cid, name))
    return 0


def cmd_review(args):
    since = now_utc() - dt.timedelta(days=args.days)
    rows = []
    for e in journal_entries():
        try:
            ts = dt.datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= since:
            rows.append(e)
    print("Journal, last %d days: %d entries" % (args.days, len(rows)))
    for e in rows:
        print("- %s %s %s %s %s | %s | lesson: %s" % (e.get("ts"), e.get("event"), e.get("side") or "",
                                                       e.get("qty") or "", e.get("symbol") or "",
                                                       e.get("why") or "-", e.get("lesson") or "-"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    m = sub.add_parser("monitor")
    m.add_argument("--minutes", type=int, default=65)
    m.add_argument("--positions", action="store_true", help="also send the open positions")
    n = sub.add_parser("notify")
    n.add_argument("text", nargs="+")
    sub.add_parser("chat-id")
    r = sub.add_parser("review")
    r.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    return {"hook": cmd_hook, "monitor": cmd_monitor, "notify": cmd_notify,
            "chat-id": cmd_chat_id, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
