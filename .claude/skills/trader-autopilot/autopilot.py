#!/usr/bin/env python3
"""trader-autopilot: rules-based, data-driven day trading on the Alpaca PAPER account.

Commands
  run       One decision pass (meant for an hourly scheduled task, 09:52-14:52 New York):
            guards -> daily scan candidates -> entry trigger (opening-range breakout, or a
            close above yesterday's high for daily setups) -> earnings filter (SEC 8-K) ->
            exit plan at the live price (1.5R and 2R) -> Time Machine (10,000 replays +
            history) -> budget -> risk check -> one bracket order (stop + target live at
            Alpaca). Every decision, taken or not, goes to Telegram and the journal.
  flatten   Cancels open orders and closes every position (flat-by, 15:50 New York).
  status    Prints the guards and today's state; sends nothing.

Flags: --dry-run (decide and report, never send an order), --now "YYYY-MM-DD HH:MM"
(New York; implies --dry-run), --quiet (no Telegram summary when nothing happened).

Hard limits, in code: paper endpoint only; the playbook's limits (rules.md, config.json)
and the extra autopilot ones below; one open position at a time; kill switches:
  - .claude/skills/trader-autopilot/autopilot.json  {"enabled": false}  (in git)
  - a Telegram message "/pausa" to the bot (holds while it is among the bot's last 24h
    of messages and newer than any "/riprendi")
Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILLS, "trader-journal"))
import journal as J  # noqa: E402

ROOT = J.ROOT
TRADER = J.TRADER
WORK = os.path.join(TRADER, "autopilot")
NY = J.NY
PAPER = "https://paper-api.alpaca.markets"
assert J.ALPACA == PAPER, "autopilot runs on the paper endpoint only"

SCAN = os.path.join(SKILLS, "trader-scanner", "scan.py")
ENTRY = os.path.join(SKILLS, "trader-entry", "entry.py")
EXIT = os.path.join(SKILLS, "trader-exit", "exit.py")
TM = os.path.join(SKILLS, "trader-timemachine", "timemachine.py")
SIZE = os.path.join(SKILLS, "trader-budget", "size.py")
RISK = os.path.join(SKILLS, "trader-risk", "check.py")

# FOMC decision days (statement 14:00 New York): no new entries before 14:30.
FOMC_DAYS = {"2026-12-09"}
DAILY_SETUPS = {"breakout_20d", "bull_flag", "reclaim_ma20"}
SETTINGS_DEFAULT = {"enabled": True, "max_candidates": 40, "min_price": 10, "max_open_positions": 1,
                    "min_tm_avg_r": 0.05, "min_history_trades": 8, "max_chase_r": 0.25,
                    "targets": ["1.5R", "2R"], "level_stop": "atr:1", "orb_stop": "or-mid"}


# ---------------------------------------------------------------- helpers
def settings():
    s = dict(SETTINGS_DEFAULT)
    s.update(J.load(os.path.join(HERE, "autopilot.json"), {}) or {})
    return s


def ny_now(args):
    if args.now:
        return dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    return J.now_utc().astimezone(NY)


def hhmm(s):
    h, m = (int(x) for x in s.split(":"))
    return h * 60 + m


def run_json(cmd, timeout=180):
    """Runs an upstream script with --json; returns (dict or None, error text)."""
    try:
        p = subprocess.run([sys.executable] + cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]


def alpaca_send(method, path, body=None):
    url = PAPER + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(J.PLACEHOLDER_HEADERS, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def telegram_paused():
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        d = J.http_json("https://api.telegram.org/bot%s/getUpdates" % token, timeout=25)
    except Exception:
        return False
    state = False
    for u in d.get("result") or []:
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(chat):
            continue
        t = (m.get("text") or "").strip().lower()
        if t.startswith("/pausa") or t.startswith("/stop"):
            state = True
        elif t.startswith("/riprendi") or t.startswith("/start"):
            state = False
    return state


# ---------------------------------------------------------------- earnings (SEC EDGAR 8-K item 2.02)
_CIK = None


def sec_ua(cfg):
    ua = os.environ.get("SEC_USER_AGENT") or str(cfg.get("sec_user_agent") or "")
    return ua if ua and "SET-ME" not in ua else "FinanceLab paper-trading research contact@example.org"


def earnings_block(symbol, today, cfg):
    """(blocked, reason). Blocks the earnings day and the day before, estimated from the
    8-K 2.02 filed for the same quarter a year earlier (+364 days, window -4/+1 days),
    and the day after a 2.02 filing (the report just came out)."""
    global _CIK
    ua = {"User-Agent": sec_ua(cfg)}
    try:
        if _CIK is None:
            raw = J.http_json("https://www.sec.gov/files/company_tickers.json", ua, timeout=30)
            _CIK = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
        cik = _CIK.get(symbol.upper().replace(".", "-"))
        if not cik:
            return False, "nessun CIK SEC (ADR/estero?): filtro utili non applicabile"
        d = J.http_json("https://data.sec.gov/submissions/CIK%010d.json" % cik, ua, timeout=20)
        r = d["filings"]["recent"]
        dates = [dt.date.fromisoformat(r["filingDate"][i]) for i in range(len(r["form"]))
                 if r["form"][i] in ("8-K", "6-K") and "2.02" in (r["items"][i] or "")]
    except Exception as e:
        return False, "SEC non raggiungibile (%s): filtro utili saltato" % type(e).__name__
    for f in dates:
        if 0 <= (today - f).days <= 1:
            return True, "risultati appena pubblicati (8-K del %s)" % f
    for f in dates:
        est = f + dt.timedelta(days=364)
        if -4 <= (today - est).days <= 1 and 330 <= (today - f).days <= 400:
            return True, "utili attesi intorno al %s (stima da 8-K del %s)" % (est, f)
    return False, None


# ---------------------------------------------------------------- state from Alpaca
def day_state(cfg, now):
    snap = J.snapshot(cfg)
    start = dt.datetime(now.year, now.month, now.day, tzinfo=NY)
    today_trades = [t for t in snap["trades"] if t.get("close_ts") and t["close_ts"] >= start]
    orders = J.alpaca_get("/v2/orders", {"status": "all", "after": start.isoformat(), "limit": 200, "nested": "true"}) or []
    entries = [o for o in orders if o.get("side") == "buy" and o.get("status") not in ("canceled", "rejected", "expired")
               and not (o.get("status") == "canceled" and float(o.get("filled_qty") or 0) == 0)]
    open_risk = 0.0
    for o in J.alpaca_get("/v2/orders", {"status": "open", "nested": "true"}) or []:
        for leg in [o] + (o.get("legs") or []):
            if leg.get("type") == "stop" and leg.get("side") == "sell":
                pos = next((p for p in snap["positions"] if p["symbol"] == leg["symbol"]), None)
                if pos:
                    open_risk += max(0.0, (float(pos["avg_entry_price"]) - float(leg["stop_price"])) * float(leg["qty"]))
    losses = -sum(t["pnl"] for t in today_trades if t["pnl"] < 0)
    streak = 0
    for t in reversed(today_trades):
        if t["pnl"] < 0:
            streak += 1
        else:
            break
    traded = {o.get("symbol") for o in orders}
    return {"snap": snap, "today_trades": today_trades, "entries": len(entries), "losses": losses,
            "open_risk": open_risk, "streak": streak, "traded": traded}


def guards(args, cfg, st, now):
    """Reasons not to look for a new entry now (empty list = go)."""
    s, why = settings(), []
    if not s.get("enabled", True):
        why.append("il pilota automatico è spento")
    if not args.now and telegram_paused():
        why.append("sono in pausa (scrivi /riprendi per ripartire)")
    if not args.now:
        clock = J.alpaca_get("/v2/clock")
        if not clock.get("is_open"):
            why.append("la borsa è chiusa")
    m = now.hour * 60 + now.minute
    if m < hhmm(cfg["no_entries_before"]) or m > hhmm(cfg["no_entries_after"]):
        why.append("compro solo tra le %s e le %s" % (J.ny_to_it(cfg["no_entries_before"]), J.ny_to_it(cfg["no_entries_after"])))
    if now.date().isoformat() in FOMC_DAYS and m < hhmm("14:30"):
        why.append("oggi decide la Fed: aspetto la sua decisione")
    if st["entries"] >= int(cfg["max_trades_per_day"]):
        why.append("ho già fatto %d operazioni, il massimo di oggi" % st["entries"])
    if st["streak"] >= int(cfg.get("max_losses_in_a_row", 2)):
        why.append("%d perdite di fila, per oggi mi fermo" % st["streak"])
    if st["losses"] >= float(cfg["max_daily_loss_usd"]):
        why.append("ho raggiunto la perdita massima di oggi")
    if len(st["snap"]["positions"]) >= int(s["max_open_positions"]):
        why.append("ho già %d posizioni aperte, il massimo (%d)" % (len(st["snap"]["positions"]), s["max_open_positions"]))
    return why


def run_config(cfg, st, extra_risk=0.0, extra_value=0.0, extra_entries=0):
    """The config handed to budget and risk for this order: the brake, today's room, and
    the capital split in max_open_positions slots (cash = basis minus what is invested)."""
    c = dict(J.load(os.path.join(TRADER, "config.json"), {}) or {})
    slots = max(1, int(settings()["max_open_positions"]))
    basis = float(st["snap"]["budget"])
    invested = sum(abs(float(p.get("market_value") or 0)) for p in st["snap"]["positions"]) + extra_value
    risk = J.risk_per_trade(st["snap"], cfg)
    room = float(cfg["max_daily_loss_usd"]) - st["losses"] - st["open_risk"] - extra_risk
    c["max_loss_per_trade_usd"] = round(max(0.0, min(risk, room)), 2)
    c["max_daily_loss_usd"] = round(max(0.0, room), 2)
    c["max_trades_per_day"] = max(0, int(cfg["max_trades_per_day"]) - st["entries"] - extra_entries)
    c["budget"] = round(max(0.0, basis - invested), 2)          # free cash on the training basis
    c["max_order_usd"] = round(min(float(c.get("max_order_usd") or basis), basis / slots), 2)
    c["max_position_pct"] = 100                                  # the slot size above is the cap
    c["paper"] = True
    path = os.path.join(WORK, "config.json")
    with open(path, "w") as f:
        json.dump(c, f, indent=2)
    return path, c


# ---------------------------------------------------------------- candidates
def ensure_watchlist(now, args):
    wl = J.load(os.path.join(TRADER, "watchlist.json"), {}) or {}
    made = str(wl.get("created_at") or "")[:10]
    if wl.get("hits") and wl.get("mode") == "daily" and made >= (now.date() - dt.timedelta(days=1)).isoformat():
        return wl, None
    if not os.path.exists(os.path.join(TRADER, "universe.txt")):
        subprocess.run([sys.executable, SCAN, "universe"], cwd=ROOT, capture_output=True, timeout=300)
    cmd = [sys.executable, SCAN, "daily"] + (["--now", args.now] if args.now else [])
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
    wl = J.load(os.path.join(TRADER, "watchlist.json"), {}) or {}
    return wl, None if wl.get("hits") else "scan fallito: %s" % (p.stdout or p.stderr).strip()[-200:]


def snapshots(symbols):
    out = {}
    for i in range(0, len(symbols), 50):
        chunk = [s for s in symbols[i:i + 50]]
        d = J.safe(J.alpaca_get, "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": "iex"}, J.DATA) or {}
        out.update(d)
    return out


def find_triggers(wl, st, args, s, log):
    hits = [h for h in wl.get("hits") or [] if (h.get("price") or 0) >= s["min_price"]
            and h.get("symbol") not in st["traded"]][:int(s["max_candidates"])]
    snaps = snapshots([h["symbol"] for h in hits])
    now_flag = ["--now", args.now] if args.now else []
    found = []
    for h in hits:
        sym = h["symbol"]
        d, err = run_json([ENTRY, "check", sym, "--json"] + now_flag, timeout=60)
        if d and d.get("status") == "triggered":
            found.append({"symbol": sym, "setup": "orb", "entry": d, "hit": h})
            continue
        if set(h.get("setups") or []) & DAILY_SETUPS:
            prev = (snaps.get(sym) or {}).get("prevDailyBar") or {}
            lvl = prev.get("h")
            if args.now:  # replay: yesterday relative to the pinned day comes from the entry check
                lvl = None
            if lvl:
                d2, _ = run_json([ENTRY, "check", sym, "--setup", "level", "--level", "%.2f" % lvl, "--json"] + now_flag, timeout=60)
                if d2 and d2.get("status") == "triggered":
                    found.append({"symbol": sym, "setup": "level", "entry": d2, "hit": h, "level": lvl})
    log.append("%d titoli controllati, %d segnali di entrata" % (len(hits), len(found)))
    return found, snaps


def live_price(sym, snaps, entry):
    sn = snaps.get(sym) or {}
    p = J.num((sn.get("latestTrade") or {}).get("p"))
    return p or J.num((entry.get("forming") or {}).get("price")) or J.num((entry.get("trigger") or {}).get("price"))


def evaluate(c, s, args, now, snaps):
    """Plans and replays one triggered candidate; returns the best variant or a reason."""
    sym, setup, e = c["symbol"], c["setup"], c["entry"]
    d = os.path.join(WORK, sym)
    os.makedirs(d, exist_ok=True)
    epath = os.path.join(d, "entry.json")
    with open(epath, "w") as f:
        json.dump(e, f)
    trig = J.num((e.get("trigger") or {}).get("price"))
    px = trig if args.now else live_price(sym, snaps, e)
    stop_rule = s["orb_stop"] if setup == "orb" else s["level_stop"]
    if args.now:
        etime = (e.get("trigger") or {}).get("time")
    else:  # entering now: the last completed 5-minute bar
        b = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        etime = b.strftime("%I:%M %p").lstrip("0")
    now_flag = ["--now", args.now] if args.now else []
    best, notes = None, []
    for tgt in s["targets"]:
        ppath = os.path.join(d, "plan_%s.json" % tgt)
        plan, err = run_json([EXIT, "plan", "--from", epath, "--entry", "%.2f" % px, "--stop", stop_rule,
                              "--target", tgt, "--write", ppath, "--json"] + (["--time", etime] if etime else []) + now_flag)
        if not plan or plan.get("error") or not plan.get("stop"):
            notes.append("piano %s rifiutato: %s" % (tgt, (plan or {}).get("error") or err))
            continue
        rps = plan["entry"] - plan["stop"]
        if trig and rps > 0 and px - trig > s["max_chase_r"] * rps:
            return None, ("chase", "prezzo %.2f, segnale %.2f, oltre %.2fR" % (px, trig, s["max_chase_r"]))
        tm, err = run_json([TM, "run", "--plan", ppath, "--until", "15:55", "--json",
                            "--write", os.path.join(d, "tm_%s.json" % tgt)] + now_flag, timeout=240)
        if not tm or "mc" not in tm:
            notes.append("Time Machine %s fallita: %s" % (tgt, err))
            continue
        v = {"target": tgt, "plan": plan, "ppath": ppath, "tm": tm, "avg_r": tm["mc"].get("avg_R") or 0.0,
             "verdict": tm.get("verdict") or "", "hist": tm.get("history") or {}}
        if best is None or v["avg_r"] > best["avg_r"]:
            best = v
    if not best:
        return None, ("noplan", "; ".join(str(n) for n in notes) or "nessun piano valido")
    head = best["verdict"].split(":")[0]
    h = best["hist"]
    if "toward the stop" in head:
        return None, ("tm_bad", "%s, avgR %+.2f" % (head, best["avg_r"]))
    if "toward the target" not in head:
        return None, ("tm_flip", "%s, avgR %+.2f" % (head, best["avg_r"]))
    if best["avg_r"] < s["min_tm_avg_r"]:
        return None, ("tm_small", "avgR %+.2f < %+.2f" % (best["avg_r"], s["min_tm_avg_r"]))
    if setup == "orb" and (h.get("triggered") or 0) >= s["min_history_trades"] and (h.get("avg_R") or 0) <= 0:
        return None, ("hist_bad", "%d casi, avgR %+.2f" % (h["triggered"], h.get("avg_R") or 0))
    return best, None


# ---------------------------------------------------------------- context diary (exogenous factors)
CTXDIR = os.path.join(ROOT, "research", "context")


def recent_contexts(now, days=5):
    if not os.path.isdir(CTXDIR):
        return []
    files = sorted(f for f in os.listdir(CTXDIR) if f.endswith(".json") and f[:10] <= now.date().isoformat())
    return [J.load(os.path.join(CTXDIR, f)) for f in files[-days:]]


def red_flags(now):
    """{symbol: reason} for companies with risk headlines (probes, lawsuits, departures...) in the last 5 diaries."""
    out = {}
    for doc in recent_contexts(now):
        for sym, x in ((doc or {}).get("data", {}).get("red_flags") or {}).items():
            out[sym] = "; ".join(x.get("headlines", [])[:2])
    return out


def sector_note(sym):
    try:
        import csv
        sec = {r["symbol"]: r["sector"] for r in csv.DictReader(open(os.path.join(ROOT, "research", "universe.csv")))}.get(sym)
        doc = recent_contexts(J.now_utc().astimezone(NY), 1)[-1]
        a = ((doc or {}).get("assessment") or {}).get("sectors", {}).get(sec)
    except Exception:
        return ""
    if not a:
        return ""
    mood = {2: "molto positivo", 1: "positivo", 0: "neutro", -1: "negativo", -2: "molto negativo"}.get(a.get("tone"), "")
    return "Clima del settore %s: %s." % (sec, mood) if mood else ""


# ---------------------------------------------------------------- commands
REASONS = {
    "earnings": "conti in arrivo o appena usciti: troppo imprevedibile",
    "chase": "era già salito troppo: comprarlo ora vorrebbe dire rincorrerlo",
    "noplan": "il prezzo era già ricaduto sotto il livello di rottura",
    "tm_bad": "sui dati passati operazioni così hanno perso più spesso che guadagnato",
    "tm_flip": "sui dati passati il risultato è da testa o croce",
    "tm_small": "il guadagno atteso era troppo piccolo",
    "hist_bad": "in passato questo segnale su questo titolo ha fatto perdere",
    "redflag": "notizie negative recenti sull'azienda (indagini, cause, dimissioni...)",
    "budget": "con i limiti di rischio la posizione sarebbe stata troppo piccola",
    "risk": "bloccato dai controlli di sicurezza",
    "slots": "buono, ma ho già il numero massimo di posizioni aperte",
    "room": "buono, ma per oggi ho già usato il rischio disponibile",
    "rejected": "il broker ha rifiutato l'ordine",
}
SETUP_PLAIN = {"orb": "ha superato il prezzo massimo della prima mezz'ora di contrattazione",
               "level": "ha superato il prezzo massimo di ieri"}


def log_decisions(now, rows):
    path = os.path.join(TRADER, "journal", "decisions.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(dict(r, ts=J.now_utc().isoformat(timespec="seconds"), ny=now.strftime("%Y-%m-%d %H:%M")),
                               ensure_ascii=False) + "\n")


def cmd_run(args):
    os.makedirs(WORK, exist_ok=True)
    cfg, s, now = J.config(), settings(), ny_now(args)
    dry = args.dry_run or bool(args.now)
    log, sent_order, dec, checked = [], None, [], 0
    try:
        st = day_state(cfg, now)
    except Exception as e:
        J.telegram("🤖 Non riesco a leggere il conto (%s): controllo saltato." % J.esc(type(e).__name__))
        return 1
    stop = guards(args, cfg, st, now)
    if not stop:
        wl, err = ensure_watchlist(now, args)
        if err:
            log.append(err)
        found, snaps = find_triggers(wl, st, args, s, log)
        checked = len([h for h in wl.get("hits") or []][:int(s["max_candidates"])])
        flags = red_flags(now)
        good = []
        for c in found:
            sym = c["symbol"]
            if sym in flags:
                dec.append({"symbol": sym, "outcome": "skip", "code": "redflag", "detail": flags[sym]})
                continue
            blocked, why = earnings_block(sym, now.date(), cfg)
            if blocked:
                dec.append({"symbol": sym, "outcome": "skip", "code": "earnings", "detail": why})
                continue
            best, why = evaluate(c, s, args, now, snaps)
            if not best:
                dec.append({"symbol": sym, "outcome": "skip", "code": why[0], "detail": why[1], "setup": c["setup"]})
                continue
            best["c"] = c
            good.append(best)
        good.sort(key=lambda v: -v["avg_r"])
        slots = int(s["max_open_positions"]) - len(st["snap"]["positions"])
        left = int(cfg["max_trades_per_day"]) - st["entries"]
        used_risk = used_value = 0.0
        n = 0
        for v in good:
            sym = v["c"]["symbol"]
            if n >= min(slots, left):
                dec.append({"symbol": sym, "outcome": "skip", "code": "slots"})
                continue
            cfg_path, rcfg = run_config(cfg, st, used_risk, used_value, n)
            if rcfg["max_loss_per_trade_usd"] < 5 or rcfg["budget"] < 100:
                dec.append({"symbol": sym, "outcome": "skip", "code": "room"})
                continue
            o, code, detail = place(v, cfg_path, rcfg, args, dry, cfg, st)
            if o:
                sent_order = o
                n += 1
                used_risk += float(o["plan"].get("max_loss") or 0)
                used_value += float(o["qty"]) * float(o["plan"].get("entry") or 0)
                dec.append({"symbol": sym, "outcome": "would_buy" if dry else "bought", "qty": o["qty"],
                            "plan": o["plan"], "avg_r": v["avg_r"], "verdict": v["verdict"]})
            else:
                dec.append({"symbol": sym, "outcome": "skip", "code": code, "detail": detail})
    if not dry:
        log_decisions(now, dec + ([{"outcome": "no_search", "detail": stop}] if stop else []))
    text = summary_text(now, stop, checked, dec, log, dry)
    if args.quiet and stop:
        print(text)
        return 0
    print(J.telegram(text) if not args.no_telegram else text)
    return 0


def summary_text(now, stop, checked, dec, log, dry):
    it = now.astimezone(J.IT).strftime("%H:%M")
    head = "🤖 <b>Controllo delle %s</b>%s" % (it, "  <i>(prova, nessun ordine)</i>" if dry else "")
    if stop:
        return head + "\nOggi non cerco nuove operazioni: " + J.esc("; ".join(stop)) + "."
    bought = [d for d in dec if d["outcome"] in ("bought", "would_buy")]
    skipped = [d for d in dec if d["outcome"] == "skip"]
    out = [head, "Ho guardato %d titoli: %d avevano un segnale d'acquisto." % (checked, len(bought) + len(skipped))]
    for d in bought:
        out.append("✅ %s %s (%s azioni)" % ("Comprerei" if dry else "Comprato", J.esc(d["symbol"]), d["qty"]))
    if skipped:
        by = {}
        for d in skipped:
            by.setdefault(d.get("code"), []).append(d["symbol"])
        out.append("❌ Non comprati:")
        for code, syms in sorted(by.items(), key=lambda kv: -len(kv[1])):
            out.append("• %s: %s" % (REASONS.get(code, code), J.esc(", ".join(syms[:6]) + ("…" if len(syms) > 6 else ""))))
    if not bought and not skipped:
        out.append("Nessun segnale d'acquisto in questo momento.")
    return "\n".join(out)


def place(v, cfg_path, rcfg, args, dry, cfg, st):
    sym, plan = v["c"]["symbol"], v["plan"]
    d = os.path.join(WORK, sym)
    opath = os.path.join(d, "orders.json")
    now_flag = ["--now", args.now] if args.now else []
    sz, err = run_json([SIZE, "--plan", v["ppath"], "--config", cfg_path, "--write", opath, "--json",
                        "--portfolio", os.path.join(d, "portfolio.json")] + now_flag)
    orders = (J.load(opath, {}) or {}).get("orders") or []
    if not orders:
        return None, "budget", str((sz or {}).get("error") or err or "stop too wide")
    rk, err = run_json([RISK, "--orders", opath, "--config", cfg_path, "--state", os.path.join(d, "broker.json"),
                        "--portfolio", os.path.join(d, "portfolio.json"), "--out", os.path.join(d, "risk.json"),
                        "--json"] + now_flag)
    res = ((rk or {}).get("results") or [{}])[0]
    if res.get("verdict") != "PASS":
        return None, "risk", "; ".join(res.get("reasons") or [str(err or "?")])
    o = orders[0]
    h = v["hist"]
    why = "%s; %s%s." % (SETUP_PLAIN.get(v["c"]["setup"], "segnale d'acquisto"), J.verdict_it(v["verdict"]),
                         (" e nelle ultime %d volte in cui si è presentato su questo titolo ha guadagnato in media" % h["triggered"])
                         if (h.get("triggered") or 0) >= 8 and (h.get("avg_R") or 0) > 0 else "")
    ctx = sector_note(sym)
    if ctx:
        why += " " + ctx
    body = {"symbol": sym, "qty": str(o["qty"]), "side": "buy", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit": {"limit_price": str(o["take_profit_limit_price"])},
            "stop_loss": {"stop_price": str(o["stop_loss_stop_price"])},
            "client_order_id": "ap-%s-%s" % (J.now_utc().strftime("%Y%m%d%H%M%S"), sym)}
    if dry:
        card = "🧪 <b>PROVA</b> (nessun ordine inviato)\n" + J.buy_card(
            {"symbol": sym, "qty": o["qty"], "type": "market", "side": "buy"}, o["plan"], why, v["verdict"], cfg)
        print(card if args.no_telegram else J.telegram(card))
        return o, None, None
    try:
        resp = alpaca_send("POST", "/v2/orders", body)
    except Exception as e:
        detail = e.read().decode()[:200] if hasattr(e, "read") else type(e).__name__
        return None, "rejected", detail
    J.append_journal({"ts": J.now_utc().isoformat(timespec="seconds"), "event": "place_stock_order", "paper": True,
                      "autopilot": True, "symbol": sym, "side": "buy", "qty": o["qty"], "input": body,
                      "order_id": resp.get("id"), "why": why, "plan": o["plan"], "timemachine": v["verdict"],
                      "tm_avg_r": v["avg_r"], "response": json.dumps(resp)[:600]})
    snap = J.safe(J.snapshot, cfg)
    J.telegram("🤖 " + J.buy_card({"symbol": sym, "qty": o["qty"], "type": "market", "side": "buy"},
                                  o["plan"], why, v["verdict"], cfg, snap))
    return o, None, None


def cmd_flatten(args):
    dry = args.dry_run
    lines = []
    try:
        opens = J.alpaca_get("/v2/orders", {"status": "open", "nested": "true"}) or []
        positions = J.alpaca_get("/v2/positions") or []
    except Exception as e:
        print(J.telegram("🤖 <b>FLAT</b> · errore lettura Alpaca: %s" % J.esc(type(e).__name__)))
        return 1
    for o in opens:
        lines.append("annullo ordine %s %s %s" % (o.get("side"), o.get("qty"), o.get("symbol")))
        if not dry:
            J.safe(alpaca_send, "DELETE", "/v2/orders/%s" % o["id"])
    for p in positions:
        lines.append("chiudo %s %s az. (P/L %s)" % (p["symbol"], p["qty"], J.usd(p.get("unrealized_pl"), True)))
        if not dry:
            try:
                r = alpaca_send("DELETE", "/v2/positions/%s" % urllib.parse.quote(p["symbol"]))
                J.append_journal({"ts": J.now_utc().isoformat(timespec="seconds"), "event": "close_position",
                                  "paper": True, "autopilot": True, "symbol": p["symbol"], "side": "sell",
                                  "qty": p["qty"], "order_id": r.get("id"), "why": "flat by 15:55 (regola)"})
            except Exception as e:
                lines.append("ERRORE chiusura %s: %s" % (p["symbol"], type(e).__name__))
    if not lines:
        print("flat: nothing open")
        return 0
    print(J.telegram("🚪 <b>FLAT DI FINE GIORNATA</b>%s\n" % ("  <i>(prova)</i>" if dry else "")
                     + "\n".join("• " + J.esc(l) for l in lines)))
    return 0


def cmd_status(args):
    cfg, now = J.config(), ny_now(args)
    st = day_state(cfg, now)
    g = guards(args, cfg, st, now)
    print(json.dumps({"now_ny": now.strftime("%Y-%m-%d %H:%M"), "settings": settings(), "entries_today": st["entries"],
                      "losses_today": st["losses"], "open_risk": st["open_risk"], "loss_streak": st["streak"],
                      "equity_basis": round(st["snap"]["equity"], 2), "brake": st["snap"]["brake"],
                      "open_positions": [p["symbol"] for p in st["snap"]["positions"]],
                      "guards": g or "GO"}, indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--now", help="pin the New York clock (replay; implies --dry-run)")
    common.add_argument("--quiet", action="store_true", help="no Telegram when nothing was found")
    common.add_argument("--no-telegram", action="store_true", help="print instead of sending")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("run", "flatten", "status"):
        sub.add_parser(n, parents=[common])
    args = ap.parse_args(argv)
    if args.no_telegram:
        J.DRY_RUN = True
    return {"run": cmd_run, "flatten": cmd_flatten, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
