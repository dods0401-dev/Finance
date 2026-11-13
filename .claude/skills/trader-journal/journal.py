#!/usr/bin/env python3
"""trader-journal: trade diary + structured Telegram reports.

Commands
  hook              PostToolUse hook: after an Alpaca order tool runs, writes a journal
                    entry with the rationale (orders.json "why", plan.json, Time Machine
                    verdict) and sends a structured card to Telegram. Never blocks.
  monitor           Reads Alpaca (paper) fills from the last --minutes: one card per
                    fill (entry filled, or exit with P/L in $ and R, target/stop/manual).
                    --positions also sends the open positions. Read only.
  morning           Morning briefing: market session, futures/VIX/rates, pre-market
                    movers, watchlist, account on the $6,000 basis, today's rules.
  perf [--period day|week|month|all]
                    Performance update: equity on the training basis, P/L, win rate,
                    average R, profit factor, drawdown, brake status.
  eod               End of day: today's trades, performance, open-position warning,
                    lesson reminder.
  notify TEXT       Sends TEXT to Telegram.
  chat-id           Prints the chat ids that wrote to your bot (setup helper).
  review [--days N] Summary of the journal: trades, outcome, rule breaks noted.
  --dry-run (any command) prints the message instead of sending it.

Environment
  TELEGRAM_BOT_TOKEN   bot token from @BotFather (environment variable, never in chat)
  TELEGRAM_CHAT_ID     your chat id (from `chat-id`)
  Alpaca keys are injected by the environment's network proxy; the header values
  sent here are placeholders.

Standard library only. Files live in trader/ at the project root. Read only towards
the broker: no command here places, changes or cancels an order.
"""
import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
TRADER = os.path.join(ROOT, "trader")
JOURNAL = os.path.join(TRADER, "journal", "journal.jsonl")
ALPACA = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
PLACEHOLDER_HEADERS = {"APCA-API-KEY-ID": "PKPLACEHOLDERPROXYINJECTS",
                       "APCA-API-SECRET-KEY": "placeholder-proxy-injects"}
NY, IT = ZoneInfo("America/New_York"), ZoneInfo("Europe/Rome")
LINE = "━━━━━━━━━━━━━━━"
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
DRY_RUN = False
SETUP_IT = {"breakout_20d": "rottura 20gg", "bull_flag": "bull flag", "reclaim_ma20": "sopra MA20",
            "oversold": "ipervenduto", "oversold_bounce": "rimbalzo"}

DEFAULTS = {"budget": 6000, "max_loss_per_trade_usd": 120, "max_daily_loss_usd": 240,
            "max_trades_per_day": 3, "no_entries_before": "09:45", "no_entries_after": "15:30",
            "flat_by": "15:55", "brake_equity": 5400, "brake_risk_usd": 60}

MARKETS = [("S&P 500 fut", "ES=F", 2), ("Nasdaq fut", "NQ=F", 2), ("Russell fut", "RTY=F", 2),
           ("VIX", "^VIX", 2), ("Treasury 10A", "^TNX", 3), ("Petrolio WTI", "CL=F", 2),
           ("Oro", "GC=F", 1), ("EUR/USD", "EURUSD=X", 4), ("Bitcoin", "BTC-USD", 0)]


# ---------------------------------------------------------------- helpers
def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def config():
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (load(os.path.join(TRADER, "config.json"), {}) or {}).items()
              if v is not None})
    return c


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def usd(x, sign=False):
    x = num(x, 0.0)
    s = "{:,.2f}".format(abs(x)).replace(",", "X").replace(".", ",").replace("X", ".")
    if s.endswith(",00"):
        s = s[:-3]
    pre = ("+" if x > 0 else "−" if x < 0 else "") if sign else ("−" if x < 0 else "")
    return "%s$%s" % (pre, s)


def pct(x, sign=True, digits=1):
    x = num(x, 0.0)
    s = ("{:.%df}" % digits).format(abs(x)).replace(".", ",")
    return "%s%s%%" % (("+" if x > 0 else "−" if x < 0 else "") if sign else "", s)


def price(x, digits=2):
    x = num(x)
    return "—" if x is None else ("{:,.%df}" % digits).format(x).replace(",", "X").replace(".", ",").replace("X", ".")


def rfmt(r):
    return "—" if r is None else "%s%sR" % ("+" if r > 0 else "−" if r < 0 else "", ("%.2f" % abs(r)).replace(".", ","))


def it_date(t):
    t = t.astimezone(IT)
    return "%s %d %s %d" % (GIORNI[t.weekday()], t.day, MESI[t.month - 1], t.year)


def hm(t, tz=IT):
    return t.astimezone(tz).strftime("%H:%M")


def ny_to_it(hhmm, day=None):
    day = day or now_utc().astimezone(NY).date()
    h, m = (int(x) for x in hhmm.split(":"))
    return hm(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=NY))


def duration(a, b):
    if not a or not b:
        return ""
    mins = int((b - a).total_seconds() // 60)
    return "%dm" % mins if mins < 60 else "%dh%02dm" % (mins // 60, mins % 60)


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


def telegram(text, tries=2, timeout=15):
    if DRY_RUN:
        print(text)
        return "dry-run"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    err = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token,
                                        data=data, timeout=timeout) as r:
                return "sent" if json.load(r).get("ok") else "telegram error"
        except urllib.error.HTTPError as e:  # bad HTML: resend as plain text once
            if e.code == 400:
                plain = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
                try:
                    with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token,
                                                data=plain, timeout=timeout) as r:
                        return "sent (plain)" if json.load(r).get("ok") else "telegram error"
                except Exception as e2:
                    return "telegram failed: %s" % type(e2).__name__
            err = e
        except Exception as e:  # never let an alert break the trade flow
            err = e
    return "telegram failed: %s" % type(err).__name__


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def alpaca_get(path, params=None, base=ALPACA):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return http_json(url, PLACEHOLDER_HEADERS)


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def yahoo_quote(symbol):
    d = http_json(YAHOO % urllib.parse.quote(symbol), {"User-Agent": "Mozilla/5.0"}, timeout=12)
    meta = d["chart"]["result"][0]["meta"]
    last = num(meta.get("regularMarketPrice"))
    prev = num(meta.get("chartPreviousClose")) or num(meta.get("previousClose"))
    closes = [c for c in (d["chart"]["result"][0].get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c]
    if len(closes) >= 2:  # previous session close (chartPreviousClose is the one before the 5d window)
        prev = closes[-2]
    return last, prev


# ---------------------------------------------------------------- trades & performance
def all_fills(since=None):
    out, token = [], None
    for _ in range(50):
        params = {"direction": "asc", "page_size": 100}
        if since:
            params["after"] = since.isoformat(timespec="seconds")
        if token:
            params["page_token"] = token
        page = alpaca_get("/v2/account/activities/FILL", params) or []
        out.extend(page)
        if len(page) < 100:
            break
        token = page[-1].get("id")
    return out


def journal_by_order():
    return {e.get("order_id"): e for e in journal_entries() if e.get("order_id")}


def build_trades(fills, cfg):
    """Round trips (flat -> long -> flat) per symbol from the fill history, FIFO."""
    jby = journal_by_order()
    open_, trades = {}, []
    for f in fills:
        sym, side = f.get("symbol"), f.get("side")
        qty, px = num(f.get("qty"), 0.0), num(f.get("price"), 0.0)
        ts = parse_ts(f.get("transaction_time"))
        t = open_.get(sym)
        if side == "buy":
            if not t:
                j = jby.get(f.get("order_id")) or {}
                t = open_[sym] = {"symbol": sym, "qty": 0.0, "cost": 0.0, "max_qty": 0.0, "sold": 0.0,
                                  "proceeds": 0.0, "open_ts": ts, "close_ts": None,
                                  "plan": j.get("plan") or {}, "why": j.get("why"), "order_id": f.get("order_id")}
            t["qty"] += qty
            t["cost"] += qty * px
            t["max_qty"] = max(t["max_qty"], t["qty"])
        elif side in ("sell", "sell_short") and t:
            avg = t["cost"] / t["qty"] if t["qty"] else 0.0
            take = min(qty, t["qty"])
            t["qty"] -= take
            t["cost"] -= take * avg
            t["sold"] += take
            t["proceeds"] += take * px
            t.setdefault("basis_sold", 0.0)
            t["basis_sold"] += take * avg
            if t["qty"] <= 1e-9:
                t["close_ts"] = ts
                trades.append(finish_trade(open_.pop(sym), cfg))
    return trades, list(open_.values())


def trade_risk(t, cfg):
    p = t.get("plan") or {}
    if num(p.get("max_loss")):
        return num(p["max_loss"])
    entry = t["basis_sold"] / t["sold"] if t.get("sold") else None
    if entry and num(p.get("stop")) and entry > num(p["stop"]):
        return (entry - num(p["stop"])) * (t.get("max_qty") or t["sold"])
    return num(cfg.get("max_loss_per_trade_usd"), 120)


def finish_trade(t, cfg):
    pnl = t["proceeds"] - t.get("basis_sold", 0.0)
    risk = trade_risk(t, cfg)
    t.update({"pnl": pnl, "risk": risk, "r": pnl / risk if risk else None,
              "entry": t["basis_sold"] / t["sold"] if t["sold"] else None,
              "exit": t["proceeds"] / t["sold"] if t["sold"] else None})
    return t


def exit_kind(t):
    p = t.get("plan") or {}
    ex, stop, tgt = num(t.get("exit")), num(p.get("stop")), num(p.get("target"))
    if ex and tgt and ex >= tgt * 0.998:
        return "🎯", "TARGET"
    if ex and stop and ex <= stop * 1.002:
        return "🛑", "STOP"
    if t["pnl"] > 0:
        return "✅", "USCITA IN UTILE"
    if t["pnl"] < 0:
        return "🔻", "USCITA IN PERDITA"
    return "⚪", "USCITA A PAREGGIO"


def stats(trades):
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_w, gross_l = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    return {"n": len(trades), "w": len(wins), "l": len(losses), "pnl": sum(t["pnl"] for t in trades),
            "winrate": 100.0 * len(wins) / len(trades) if trades else None,
            "avg_r": sum(rs) / len(rs) if rs else None,
            "pf": gross_w / gross_l if gross_l else None,
            "best": max(trades, key=lambda t: t["pnl"]) if trades else None,
            "worst": min(trades, key=lambda t: t["pnl"]) if trades else None}


def loss_streak(trades):
    n = 0
    for t in reversed(trades):
        if t["pnl"] < 0:
            n += 1
        else:
            break
    return n


def snapshot(cfg):
    """Everything the performance and morning cards need, from Alpaca (read only)."""
    fills = all_fills()
    trades, _ = build_trades(fills, cfg)
    positions = alpaca_get("/v2/positions") or []
    unreal = sum(num(p.get("unrealized_pl"), 0.0) for p in positions)
    budget = num(cfg["budget"], 6000)
    realized = sum(t["pnl"] for t in trades)
    curve, peak, maxdd = budget, budget, 0.0
    for t in trades:
        curve += t["pnl"]
        peak = max(peak, curve)
        maxdd = min(maxdd, curve - peak)
    equity = budget + realized + unreal
    return {"trades": trades, "positions": positions, "unreal": unreal, "realized": realized,
            "equity": equity, "budget": budget, "peak": max(peak, equity), "maxdd": maxdd,
            "brake": equity < num(cfg.get("brake_equity"), 5400)}


def period_start(period, now=None):
    now = (now or now_utc()).astimezone(NY)
    d = now.date()
    if period == "day":
        start = d
    elif period == "week":
        start = d - dt.timedelta(days=d.weekday())
    elif period == "month":
        start = d.replace(day=1)
    else:
        return None
    return dt.datetime(start.year, start.month, start.day, tzinfo=NY)


def in_period(t, start):
    return start is None or (t.get("close_ts") and t["close_ts"] >= start)


def today_line(snap, cfg):
    start = period_start("day")
    today = [t for t in snap["trades"] if in_period(t, start)]
    entries_today = len(today) + len(snap["positions"])
    lost = -sum(t["pnl"] for t in today if t["pnl"] < 0)
    s = stats(today)
    return ("Oggi: trade %d/%d · P/L %s · %dW %dL · perdite %s / %s"
            % (entries_today, int(cfg["max_trades_per_day"]), usd(s["pnl"], True), s["w"], s["l"],
               usd(lost), usd(cfg["max_daily_loss_usd"])))


def equity_line(snap):
    ch = snap["equity"] - snap["budget"]
    return "Capitale (base %s): <b>%s</b> (%s)" % (usd(snap["budget"]), usd(snap["equity"]),
                                                   pct(100.0 * ch / snap["budget"]))


def risk_per_trade(snap, cfg):
    return num(cfg.get("brake_risk_usd"), 60) if snap["brake"] else num(cfg["max_loss_per_trade_usd"], 120)


# ---------------------------------------------------------------- cards
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


def buy_card(ti, plan, why, verdict, cfg, snap=None):
    sym, qty = ti.get("symbol"), num(ti.get("qty"), 0.0)
    plan = plan or {}
    entry = num(plan.get("entry")) or num(ti.get("limit_price")) or num(ti.get("stop_price"))
    stop = num(plan.get("stop")) or num(ti.get("stop_loss_stop_price")) or num((ti.get("stop_loss") or {}).get("stop_price"))
    target = num(plan.get("target")) or num(ti.get("take_profit_limit_price")) or num((ti.get("take_profit") or {}).get("limit_price"))
    budget = num(cfg["budget"], 6000)
    kind = (ti.get("type") or "market").lower()
    rows = ["Quantità  %s az. @ %s (%s)" % (int(qty) if qty == int(qty) else qty, price(entry), kind)]
    risk = None
    if entry and stop:
        risk = num(plan.get("max_loss")) or (entry - stop) * qty
        rows.append("Stop      %s  (−%s/az.)" % (price(stop), price(entry - stop)))
    if entry and target:
        rows.append("Target    %s  (+%s/az.)" % (price(target), price(target - entry)))
    if risk is not None:
        rows.append("Rischio   %s · %s del capitale" % (usd(risk), pct(100.0 * risk / budget, False)))
    if entry and stop and target and entry > stop:
        rows.append("R:R       %s" % ("%.1f" % ((target - entry) / (entry - stop))).replace(".", ","))
    if entry and qty:
        rows.append("Valore    %s · %s del budget" % (usd(entry * qty), pct(100.0 * entry * qty / budget, False, 0)))
    out = ["🟢 <b>ACQUISTO · %s</b>  <i>paper</i>" % esc(sym), "<pre>%s</pre>" % esc("\n".join(rows))]
    if why or plan.get("setup") or verdict:
        if plan.get("setup"):
            out.append("<b>Setup</b>: %s" % esc(plan["setup"]))
        if why:
            out.append("<b>Perché</b>: %s" % esc(why))
        if verdict:
            out.append("<b>Time Machine</b>: %s" % esc(verdict))
    else:
        out.append("⚠️ Nessun motivo trovato in trader/orders.json")
    flat = cfg.get("flat_by", "15:55")
    out.append("Flat entro %s ITA (%s NY)" % (ny_to_it(flat), flat))
    if snap:
        out += [LINE, today_line(snap, cfg)]
    return "\n".join(out)


def generic_card(tool, ti, why, plan):
    title = {"cancel_order_by_id": "❎ ORDINE ANNULLATO", "replace_order_by_id": "✏️ ORDINE MODIFICATO",
             "close_position": "🚪 POSIZIONE CHIUSA", "place_stock_order": "🔴 ORDINE DI VENDITA"}[tool]
    out = ["<b>%s</b>  <i>paper</i>" % title]
    sym = ti.get("symbol") or ti.get("symbol_or_asset_id")
    if sym:
        out.append("%s %s %s" % (esc((ti.get("side") or "").upper()), esc(ti.get("qty") or ""), esc(sym)))
    if plan:
        out.append(esc(fmt_plan(plan)))
    if why:
        out.append("<b>Perché</b>: %s" % esc(why))
    if tool != "place_stock_order":
        out.append("<code>%s</code>" % esc(json.dumps(ti, ensure_ascii=False)[:300]))
    return "\n".join(out)


def exit_card(t, snap, cfg):
    icon, label = exit_kind(t)
    q = int(t["sold"]) if t["sold"] == int(t["sold"]) else t["sold"]
    out = ["%s <b>%s · %s</b>  %s (%s)" % (icon, label, esc(t["symbol"]), usd(t["pnl"], True), rfmt(t.get("r"))),
           "<pre>%s</pre>" % esc("Entrata  %s\nUscita   %s\nQuantità %s az.\nDurata   %s"
                                  % (price(t.get("entry")), price(t.get("exit")), q,
                                     duration(t.get("open_ts"), t.get("close_ts")) or "—"))]
    if t.get("why"):
        out.append("<b>Perché era entrato</b>: %s" % esc(t["why"]))
    out += [LINE, today_line(snap, cfg), equity_line(snap)]
    streak = loss_streak(snap["trades"])
    if streak >= int(cfg.get("max_losses_in_a_row", 2)):
        out.append("⛔ <b>%d perdite di fila: stop per oggi</b> (regola del playbook)" % streak)
    start = period_start("day")
    lost = -sum(x["pnl"] for x in snap["trades"] if in_period(x, start) and x["pnl"] < 0)
    if lost >= num(cfg["max_daily_loss_usd"], 240):
        out.append("⛔ <b>Limite di perdita giornaliero raggiunto: giornata finita</b>")
    out.append("📝 Scrivi la lezione nel diario.")
    return "\n".join(out)


def fill_entry_card(f, j):
    out = ["📥 <b>ESEGUITO · %s</b>  <i>paper</i>" % esc(f.get("symbol")),
           "Acquistate %s az. @ %s alle %s ITA" % (esc(f.get("qty")), price(f.get("price")),
                                                   hm(parse_ts(f.get("transaction_time")) or now_utc()))]
    if j.get("plan"):
        p = j["plan"]
        out.append("Stop %s · Target %s" % (price(p.get("stop")), price(p.get("target"))))
    out.append("<b>Perché</b>: %s" % esc(j.get("why") or "non trovato nel diario (ordine fatto fuori da Claude?)"))
    return "\n".join(out)


def positions_card(positions, cfg):
    if not positions:
        return None
    rows = []
    for p in positions:
        rows.append("%-5s %4s @ %s  %s (%s)" % (p.get("symbol"), p.get("qty"), price(p.get("avg_entry_price")),
                                               usd(p.get("unrealized_pl"), True),
                                               pct(100 * num(p.get("unrealized_plpc"), 0.0))))
    flat = cfg.get("flat_by", "15:55")
    return "📂 <b>POSIZIONI APERTE</b>  <i>paper</i>\n<pre>%s</pre>Flat entro %s ITA" % (
        esc("\n".join(rows)), ny_to_it(flat))


def perf_card(snap, cfg, period="week"):
    labels = {"day": "OGGI", "week": "SETTIMANA", "month": "MESE", "all": "DALL'INIZIO"}
    out = ["📊 <b>PERFORMANCE · %s</b> · %s" % (labels[period], it_date(now_utc())), equity_line(snap)]
    rows = []
    for p in ("day", "week", "month", "all"):
        s = stats([t for t in snap["trades"] if in_period(t, period_start(p))])
        rows.append("%-11s %9s  %2d trade  %dW %dL" % (labels[p].capitalize(), usd(s["pnl"], True), s["n"], s["w"], s["l"]))
    out.append("<pre>%s</pre>" % esc("\n".join(rows)))
    s = stats([t for t in snap["trades"] if in_period(t, period_start(period))])
    if s["n"]:
        out.append("<b>%s</b>: win rate %s · R medio %s · profit factor %s" % (
            labels[period].capitalize(), pct(s["winrate"], False, 0), rfmt(s["avg_r"]),
            ("%.2f" % s["pf"]).replace(".", ",") if s["pf"] is not None else "∞" if s["w"] else "—"))
        out.append("Migliore %s %s · Peggiore %s %s" % (esc(s["best"]["symbol"]), usd(s["best"]["pnl"], True),
                                                        esc(s["worst"]["symbol"]), usd(s["worst"]["pnl"], True)))
    else:
        out.append("Nessun trade chiuso in questo periodo.")
    out.append("Max drawdown %s · P/L aperto %s" % (usd(snap["maxdd"], True), usd(snap["unreal"], True)))
    if snap["brake"]:
        out.append("🧯 <b>Freno drawdown ATTIVO</b>: rischio per trade %s finché il capitale non torna sopra %s"
                   % (usd(cfg.get("brake_risk_usd", 60)), usd(snap["budget"])))
    else:
        out.append("Freno drawdown: off · rischio per trade %s" % usd(risk_per_trade(snap, cfg)))
    pc = positions_card(snap["positions"], cfg)
    if pc:
        out += [LINE, pc]
    return "\n".join(out)


def market_session():
    clock = alpaca_get("/v2/clock")
    today = now_utc().astimezone(NY).date()
    cal = alpaca_get("/v2/calendar", {"start": today.isoformat(), "end": (today + dt.timedelta(days=7)).isoformat()})
    nxt = cal[0] if cal else None
    if nxt and nxt.get("date") == today.isoformat():
        o, c = nxt["open"], nxt["close"]
        early = " (chiusura anticipata)" if c != "16:00" else ""
        status = "🟢 aperto ora" if clock.get("is_open") else "apre alle %s ITA" % ny_to_it(o, today)
        return "Wall Street oggi: %s · chiude %s ITA%s" % (status, ny_to_it(c, today), early), True
    when = "%s %s" % (nxt["date"], ny_to_it(nxt["open"], dt.date.fromisoformat(nxt["date"]))) if nxt else "?"
    return "🔒 Wall Street chiusa oggi · prossima apertura %s ITA" % when, False


def morning_card(cfg):
    now = now_utc()
    out = ["☀️ <b>BUONGIORNO · %s</b>" % it_date(now)]
    sess = safe(market_session)
    trading_day = True
    if sess:
        out.append(sess[0])
        trading_day = sess[1]
    rows = []
    for name, sym, d in MARKETS:
        q = safe(yahoo_quote, sym)
        if q and q[0] is not None:
            last, prev = q
            ch = 100.0 * (last - prev) / prev if prev else 0.0
            arrow = "▲" if ch > 0.05 else "▼" if ch < -0.05 else "•"
            rows.append("%-13s %10s %s %s" % (name, price(last, d), arrow, pct(ch, True, 1)))
    if rows:
        out += ["", "🌍 <b>Mercati</b>", "<pre>%s</pre>" % esc("\n".join(rows))]
    movers = safe(alpaca_get, "/v1beta1/screener/stocks/movers", {"top": 50}, DATA)
    if movers:
        def ok(m):
            s = m.get("symbol") or ""
            return s.isalpha() and len(s) <= 4 and num(m.get("price"), 0) >= 5
        g = [m for m in movers.get("gainers", []) if ok(m)][:5]
        lo = [m for m in movers.get("losers", []) if ok(m)][:5]
        if g or lo:
            rows = ["%-5s %8s %s" % (m["symbol"], price(m["price"]), pct(m["percent_change"])) for m in g + lo]
            out += ["🚀 <b>Movers</b> (≥ $5, ultima seduta/pre-market)", "<pre>%s</pre>" % esc("\n".join(rows))]
    wl = load(os.path.join(TRADER, "watchlist.json"), {}) or {}
    hits = wl.get("hits") or []
    if hits:
        syms = [h.get("symbol") for h in hits[:8] if h.get("symbol")]
        snaps = safe(alpaca_get, "/v2/stocks/snapshots", {"symbols": ",".join(syms), "feed": "iex"}, DATA) or {}
        rows = []
        for h in hits[:8]:
            s = h.get("symbol")
            sn = snaps.get(s) or {}
            last = num((sn.get("latestTrade") or {}).get("p"))
            prev = num((sn.get("prevDailyBar") or {}).get("c"))
            ch = pct(100.0 * (last - prev) / prev) if last and prev else ""
            setup = h.get("setups") or h.get("setup") or h.get("signal") or ""
            if isinstance(setup, list):
                setup = ", ".join(SETUP_IT.get(x, x) for x in setup)
            rsi = " RSI %d" % h["rsi"] if num(h.get("rsi")) is not None else ""
            rows.append("%-5s %8s %7s%s  %s" % (s, price(last or h.get("price")), ch, rsi, str(setup)[:24]))
        out += ["👀 <b>Watchlist</b> (scan %s del %s: %d titoli, i più scambiati)" % (
                    esc(wl.get("mode", "")), esc(str(wl.get("as_of") or wl.get("created_at") or "")[:10]), len(hits)),
                "<pre>%s</pre>" % esc("\n".join(rows))]
    else:
        out.append("👀 Watchlist vuota: lancia /trader-scanner daily prima dell'apertura.")
    snap = safe(snapshot, cfg)
    if snap:
        y = [t for t in snap["trades"] if t.get("close_ts") and t["close_ts"] < period_start("day")]
        last_day = max((t["close_ts"].astimezone(NY).date() for t in y), default=None)
        prev_pnl = sum(t["pnl"] for t in y if t["close_ts"].astimezone(NY).date() == last_day) if last_day else 0.0
        wk = stats([t for t in snap["trades"] if in_period(t, period_start("week"))])
        out += ["💼 <b>Conto</b>", equity_line(snap),
                "Ultima seduta %s · settimana %s (%d trade)" % (usd(prev_pnl, True), usd(wk["pnl"], True), wk["n"])]
        if snap["positions"]:
            out.append("⚠️ %d posizioni aperte dalla notte: la regola è flat ogni sera" % len(snap["positions"]))
    else:
        snap = {"brake": False}
    flat = cfg.get("flat_by", "15:55")
    if trading_day:
        out += ["📏 <b>Regole di oggi</b>",
                "<pre>%s</pre>" % esc(
                    "Rischio/trade  %s%s\nStop giornata  %s o %d perdite\nMax trade      %d\n"
                    "Entrate        %s–%s ITA\nFlat entro     %s ITA\nR:R minimo     %s"
                    % (usd(risk_per_trade(snap, cfg)), " (freno)" if snap.get("brake") else "",
                       usd(cfg["max_daily_loss_usd"]), int(cfg.get("max_losses_in_a_row", 2)), int(cfg["max_trades_per_day"]),
                       ny_to_it(cfg["no_entries_before"]), ny_to_it(cfg["no_entries_after"]), ny_to_it(flat),
                       str(cfg.get("min_reward_risk", 1.5)).replace(".", ","))),
                "✅ Checklist: notizie e 8-K sulla watchlist · niente titoli con utili oggi/domani · "
                "giorno macro (CPI/Fed/NFP)? aspetta 30 min · Time Machine prima di ogni trade"]
    return "\n".join(out)


# ---------------------------------------------------------------- commands
def cmd_hook(_args):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = (payload.get("tool_name") or "").split("__")[-1]
    if tool not in ("place_stock_order", "cancel_order_by_id", "replace_order_by_id", "close_position"):
        return 0
    ti = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    resp_text = json.dumps(resp, ensure_ascii=False)[:600] if resp is not None else ""
    symbol, side, qty = ti.get("symbol"), ti.get("side"), ti.get("qty")
    why, plan, verdict = rationale_for(symbol, side, qty) if symbol else (None, None, None)
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("id") or resp.get("order_id")
    elif isinstance(resp, str):
        try:
            order_id = (json.loads(resp) or {}).get("id")
        except (ValueError, AttributeError):
            pass
    entry = {"ts": now_utc().isoformat(timespec="seconds"), "event": tool, "paper": True,
             "symbol": symbol, "side": side, "qty": qty, "input": ti, "order_id": order_id,
             "why": why, "plan": plan, "timemachine": verdict, "response": resp_text}
    append_journal(entry)
    cfg = config()
    if tool == "place_stock_order" and side == "buy":
        text = buy_card(ti, plan, why, verdict, cfg, safe(snapshot, cfg))
    else:
        text = generic_card(tool, ti, why, plan)
    status = telegram(text, tries=1, timeout=20)
    print(json.dumps({"systemMessage": "trader-journal: logged, Telegram %s" % status}))
    return 0


def cmd_monitor(args):
    cfg = config()
    since = now_utc() - dt.timedelta(minutes=args.minutes)
    try:
        snap = snapshot(cfg)
        recent = all_fills(since)
    except Exception as e:
        print("alpaca read failed: %s" % e)
        return 1
    jby = journal_by_order()
    closed = {}
    for t in snap["trades"]:
        if t.get("close_ts") and t["close_ts"] >= since:
            closed[t["symbol"]] = t
    sent, done = 0, set()
    for f in recent:
        sym = f.get("symbol")
        if f.get("side") == "buy":
            print(telegram(fill_entry_card(f, jby.get(f.get("order_id")) or {})))
            sent += 1
        elif sym in closed and sym not in done:
            print(telegram(exit_card(closed[sym], snap, cfg)))
            done.add(sym)
            sent += 1
    if args.positions:
        pc = positions_card(snap["positions"], cfg)
        if pc:
            print(telegram(pc))
    print("fills: %d, open positions: %d" % (sent, len(snap["positions"])))
    return 0


def cmd_morning(_args):
    print(telegram(morning_card(config())))
    return 0


def cmd_perf(args):
    cfg = config()
    try:
        snap = snapshot(cfg)
    except Exception as e:
        print("alpaca read failed: %s" % e)
        return 1
    print(telegram(perf_card(snap, cfg, args.period)))
    return 0


def cmd_eod(_args):
    cfg = config()
    try:
        snap = snapshot(cfg)
    except Exception as e:
        print("alpaca read failed: %s" % e)
        return 1
    today = [t for t in snap["trades"] if in_period(t, period_start("day"))]
    out = ["🌙 <b>CHIUSURA · %s</b>" % it_date(now_utc())]
    if today:
        rows = ["%-5s %9s %7s  %s" % (t["symbol"], usd(t["pnl"], True), rfmt(t.get("r")), exit_kind(t)[1].lower())
                for t in today]
        out.append("<pre>%s</pre>" % esc("\n".join(rows)))
    else:
        out.append("Nessun trade chiuso oggi. Anche non tradare è una scelta valida.")
    if snap["positions"]:
        out.append("⛔ <b>%d posizioni ancora aperte</b>: la regola è flat entro %s ITA"
                   % (len(snap["positions"]), ny_to_it(cfg.get("flat_by", "15:55"))))
    out += [LINE, perf_card(snap, cfg, "day")]
    if today:
        out.append("📝 Per ogni trade: lezione nel diario (ho seguito il piano? cosa rifarei?).")
    print(telegram("\n".join(out)))
    return 0


def cmd_notify(args):
    print(telegram(esc(" ".join(args.text))))
    return 0


def cmd_chat_id(_args):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set")
        return 1
    data = http_json("https://api.telegram.org/bot%s/getUpdates" % token, timeout=40)
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
        ts = parse_ts(e.get("ts"))
        if ts and ts >= since:
            rows.append(e)
    print("Journal, last %d days: %d entries" % (args.days, len(rows)))
    for e in rows:
        print("- %s %s %s %s %s | %s | lesson: %s" % (e.get("ts"), e.get("event"), e.get("side") or "",
                                                       e.get("qty") or "", e.get("symbol") or "",
                                                       e.get("why") or "-", e.get("lesson") or "-"))
    return 0


def main(argv=None):
    global DRY_RUN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    m = sub.add_parser("monitor")
    m.add_argument("--minutes", type=int, default=65)
    m.add_argument("--positions", action="store_true", help="also send the open positions")
    sub.add_parser("morning")
    p = sub.add_parser("perf")
    p.add_argument("--period", choices=["day", "week", "month", "all"], default="week")
    sub.add_parser("eod")
    n = sub.add_parser("notify")
    n.add_argument("text", nargs="+")
    sub.add_parser("chat-id")
    r = sub.add_parser("review")
    r.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    DRY_RUN = args.dry_run
    return {"hook": cmd_hook, "monitor": cmd_monitor, "notify": cmd_notify, "morning": cmd_morning,
            "perf": cmd_perf, "eod": cmd_eod, "chat-id": cmd_chat_id, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
