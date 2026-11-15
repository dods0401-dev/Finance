#!/usr/bin/env python3
"""Five investment streams on one Alpaca PAPER account, each with its own budget.

Every order carries its stream in client_order_id ("s1-", "s2-", "dt-", "sh-", "op-");
the books of each stream are rebuilt from the account's order history, so nothing is
stored and nothing gets lost when a scheduled session starts from scratch.

  s1 Bilanciato   50% SPY + 50% top-10 momentum, monthly           (autopilot.py rebalance)
  s2 Aggressivo   50% SSO (S&P 500 x2) + 50% top-10, monthly       (autopilot.py rebalance)
  dt Day trading  the intraday engine                               (autopilot.py run / flatten)
  sh Short        short a stock hit by severe news, hedged with its sector ETF, 5 days
  op Opzioni      long calls/puts on the strongest/weakest sector ETFs, defined risk

Commands (via autopilot.py): short, options, streams [--weekly]
Pause one stream from Telegram: "pausa short", "riprendi short"; "pausa" stops all.
A stream that loses 20% of its budget stops itself and says so.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILLS, "trader-journal"))
sys.path.insert(0, os.path.join(SKILLS, "trader-research"))
import journal as J  # noqa: E402

NY = J.NY
DEFAULT = {
    "start_date": "2026-11-16",
    "stop_loss_pct": 0.20,
    "list": {
        "s1": {"name": "Bilanciato", "emoji": "🟦", "budget": 10000, "kind": "portfolio", "core": "SPY", "core_weight": 0.5},
        "s2": {"name": "Aggressivo", "emoji": "🟥", "budget": 10000, "kind": "portfolio", "core": "SSO", "core_weight": 0.5},
        "dt": {"name": "Day trading", "emoji": "⚡", "budget": 10000, "kind": "daytrade"},
        "sh": {"name": "Short", "emoji": "🔻", "budget": 5000, "kind": "short", "slots": 4, "slot_usd": 1000,
               "hold_days": 5, "stop_pct": 0.08, "min_dollar_volume": 5e7},
        "op": {"name": "Opzioni", "emoji": "🎯", "budget": 5000, "kind": "options", "max_open": 4,
               "max_premium_usd": 600, "min_days": 30, "max_days": 60, "take_profit": 1.0, "stop_loss": 0.5,
               "exit_days_before_expiry": 10, "entry_weekdays": [0, 3]},
    },
}
LEGACY = {"ap": "dt", "pf": "s1"}
OPTION_ETFS = {"XLE": "energia e petrolio", "XLF": "finanza", "XLK": "tecnologia", "XLU": "energia elettrica (utility)",
               "XLV": "salute", "XLI": "industria", "XLB": "materiali", "XLP": "beni di consumo di base",
               "XLY": "consumi", "SMH": "semiconduttori (AI)", "GLD": "oro", "TLT": "titoli di Stato USA",
               "IWM": "piccole aziende USA", "XBI": "biotecnologie", "KRE": "banche regionali", "GDX": "miniere d'oro"}


def cfg():
    a = J.load(os.path.join(HERE, "autopilot.json"), {}) or {}
    s = json.loads(json.dumps(DEFAULT))
    user = a.get("streams") or {}
    for k, v in user.items():
        if k == "list":
            for sid, x in v.items():
                s["list"].setdefault(sid, {}).update(x)
        else:
            s[k] = v
    return s


def sid_of(cid):
    p = (cid or "").split("-")[0]
    return p if p in DEFAULT["list"] else LEGACY.get(p)


def send(method, path, body=None):
    import urllib.request
    req = urllib.request.Request(J.ALPACA + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=dict(J.PLACEHOLDER_HEADERS, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def err_text(e):
    return e.read().decode()[:160] if hasattr(e, "read") else type(e).__name__


def cid(sid, tag, sym):
    return "%s-%s-%s-%s" % (sid, tag, sym.replace("/", ""), J.now_utc().strftime("%m%d%H%M%S%f")[:14])


# ---------------------------------------------------------------- books
def all_orders(since):
    out, until = [], None
    for _ in range(40):
        p = {"status": "all", "after": since + "T00:00:00Z", "limit": 500, "nested": "true", "direction": "desc"}
        if until:
            p["until"] = until
        page = J.alpaca_get("/v2/orders", p) or []
        out += page
        if len(page) < 500:
            break
        until = page[-1]["submitted_at"]
    return out


def flat_orders(orders):
    """(order, stream) for every order and bracket leg; legs belong to their parent's stream."""
    for o in orders:
        s = sid_of(o.get("client_order_id"))
        yield o, s
        for leg in o.get("legs") or []:
            yield leg, s


def books(c=None, orders=None):
    """Per stream: cash change, holdings {symbol: qty}, cost, opened time, value."""
    c = c or cfg()
    orders = orders if orders is not None else all_orders(c["start_date"])
    b = {sid: {"cash": 0.0, "qty": {}, "cost": {}, "opened": {}, "fills": 0} for sid in c["list"]}
    fills = []
    for o, s in flat_orders(orders):
        q = float(o.get("filled_qty") or 0)
        if not s or s not in b or q <= 0:
            continue
        fills.append((o.get("filled_at") or o.get("submitted_at") or "", s, o))
    for when, s, o in sorted(fills, key=lambda x: x[0]):
        sym, q, px = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        mult = 100 if o.get("asset_class") == "us_option" else 1
        sign = 1 if o["side"] == "buy" else -1
        x = b[s]
        before = x["qty"].get(sym, 0.0)
        x["qty"][sym] = before + sign * q
        x["cash"] -= sign * q * px * mult
        x["cost"][sym] = x["cost"].get(sym, 0.0) + sign * q * px * mult
        x["fills"] += 1
        if abs(before) < 1e-9:
            x["opened"][sym] = when
        if abs(x["qty"][sym]) < 1e-9:
            x["qty"].pop(sym)
            x["cost"].pop(sym, None)
            x["opened"].pop(sym, None)
    prices = {p["symbol"]: float(p["current_price"]) for p in J.alpaca_get("/v2/positions") or []}
    for sid, x in b.items():
        hold = 0.0
        for sym, q in x["qty"].items():
            mult = 100 if re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", sym) else 1
            px = prices.get(sym)
            if px is None:  # not in the account any more: last cost
                px = abs(x["cost"].get(sym, 0.0) / (q * mult)) if q else 0.0
            hold += q * px * mult
        x["value"] = c["list"][sid]["budget"] + x["cash"] + hold
        x["invested"] = sum(abs(q) * prices.get(sym, 0) * (100 if re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", sym) else 1)
                            for sym, q in x["qty"].items())
        x["prices"] = prices
    return b, orders


# ---------------------------------------------------------------- pause per stream (Telegram)
ALIASES = {"s1": ["bilanciato", "1"], "s2": ["aggressivo", "2"], "dt": ["day trading", "daytrading", "day", "3"],
           "sh": ["short", "4"], "op": ["opzioni", "opzione", "5"]}


def paused():
    """Set of paused stream ids ("all" when everything is paused)."""
    return J.pause_state(ALIASES, {k: v["name"] for k, v in cfg()["list"].items()})


def is_paused(sid, ps=None):
    ps = paused() if ps is None else ps
    return "all" in ps or sid in ps


def auto_stop(sid, value, c):
    budget = c["list"][sid]["budget"]
    if value < budget * (1 - c["stop_loss_pct"]):
        J.pause_add(sid, ALIASES, {k: v["name"] for k, v in c["list"].items()},
                    "⛔ %s %s fermato: ha perso più del %d%% del suo budget (%s su %s)." % (
                        c["list"][sid]["emoji"], c["list"][sid]["name"], round(100 * c["stop_loss_pct"]),
                        J.usd(value), J.usd(budget)))
        return True
    return False


# ---------------------------------------------------------------- market helpers
def stock_snaps(symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        d = J.safe(J.alpaca_get, "/v2/stocks/snapshots", {"symbols": ",".join(symbols[i:i + 100]), "feed": "iex"}, J.DATA) or {}
        out.update(d)
    return out


def last_price(snap):
    return J.num((snap.get("latestTrade") or {}).get("p")) or J.num((snap.get("dailyBar") or {}).get("c"))


def trading_days_between(d0, d1):
    cal = J.alpaca_get("/v2/calendar", {"start": d0, "end": d1}) or []
    return max(0, len(cal) - 1)


def market_open():
    return bool((J.alpaca_get("/v2/clock") or {}).get("is_open"))


def wait_fill(order_id, seconds=30):
    for _ in range(seconds // 2):
        o = J.safe(J.alpaca_get, "/v2/orders/%s" % order_id) or {}
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            return o
        time.sleep(2)
    return J.safe(J.alpaca_get, "/v2/orders/%s" % order_id) or {}


def header(sid, c, text, dry):
    x = c["list"][sid]
    return "%s <b>%s · %s</b>%s" % (x["emoji"], x["name"].upper(), text, "  <i>(prova, nessun ordine)</i>" if dry else "")


# ---------------------------------------------------------------- stream 4: hedged short on severe news
def cmd_short(args):
    c = cfg()
    p, sid = c["list"]["sh"], "sh"
    dry = args.dry_run
    if not dry and not market_open():
        print("market closed: nothing to do")
        return 0
    b, orders = books(c)
    me = b[sid]
    lines = []
    # exits first
    today = J.now_utc().astimezone(NY).date().isoformat()
    for sym, q in list(me["qty"].items()):
        if q >= 0:
            continue
        opened = (me["opened"].get(sym) or today)[:10]
        days = trading_days_between(opened, today)
        if days < p["hold_days"]:
            continue
        lines.append(("exit", sym, -q, days))
    for kind, sym, qty, days in lines:
        for o, s in flat_orders(J.alpaca_get("/v2/orders", {"status": "open", "nested": "true"}) or []):
            if s == sid and o.get("symbol") == sym and not dry:
                J.safe(send, "DELETE", "/v2/orders/%s" % o["id"])
        if not dry:
            J.safe(send, "POST", "/v2/orders", {"symbol": sym, "qty": str(int(qty)), "side": "buy", "type": "market",
                                                 "time_in_force": "day", "client_order_id": cid(sid, "x", sym)})
    # hedges whose short is gone (closed by the stop or by the exit above)
    hedges = hedge_book(orders)
    closing = {s_ for _, s_, _, _ in lines}
    hedge_sold = []
    for short_sym, (etf, hq) in hedges.items():
        if hq > 1e-6 and (short_sym not in me["qty"] or short_sym in closing):
            if not dry:
                J.safe(send, "POST", "/v2/orders", {"symbol": etf, "qty": "%.6f" % hq, "side": "sell", "type": "market",
                                                     "time_in_force": "day", "client_order_id": cid(sid, "hx", short_sym)})
            hedge_sold.append((short_sym, etf))
    out = []
    for _, sym, qty, days in lines:
        out.append("✅ Chiuso lo short su <b>%s</b> dopo %d giorni di borsa (%d azioni ricomprate)." % (J.esc(sym), days, qty))
    stopped = [s_ for s_, _ in hedge_sold if s_ not in closing]
    for s_ in stopped:
        out.append("🛑 Lo short su <b>%s</b> è stato chiuso dalla protezione (il titolo è salito): venduta anche la copertura." % J.esc(s_))
    # entries
    ps = paused()
    if is_paused(sid, ps):
        out.append("⏸ In pausa: nessun nuovo short.")
    elif auto_stop(sid, me["value"], c):
        out.append("⛔ Fermato per perdite: nessun nuovo short.")
    else:
        open_n = sum(1 for q in me["qty"].values() if q < 0) - len(lines)
        free = int(p["slots"]) - open_n
        if free > 0:
            out += short_entries(c, p, sid, b, free, dry)
        else:
            out.append("Tutti i %d posti sono occupati: nessun nuovo short oggi." % p["slots"])
    if not out:
        print("short: nothing to report")
        return 0
    text = header(sid, c, "scommesse sul calo", dry) + "\n" + "\n\n".join(out)
    print(text if args.no_telegram else J.telegram(text))
    return 0


def hedge_book(orders):
    """{short symbol: (etf, hedge qty still held)} from the sh-h-/sh-hx- orders."""
    out = {}
    for o, s in flat_orders(orders):
        cid_ = o.get("client_order_id") or ""
        q = float(o.get("filled_qty") or 0)
        if s != "sh" or q <= 0:
            continue
        parts = cid_.split("-")
        if len(parts) < 3 or parts[1] not in ("h", "hx"):
            continue
        short_sym = parts[2]
        etf, hq = out.get(short_sym, (o["symbol"], 0.0))
        out[short_sym] = (o["symbol"], hq + (q if o["side"] == "buy" else -q))
    return out


def short_entries(c, p, sid, b, free, dry):
    import context as C
    import data as D
    words = (J.load(os.path.join(D.RES, "themes.json"), {}) or {}).get("red_flag_words", {})
    news = C.news_24h(60)
    flags = C.red_flags(news, words)
    uni = {r["symbol"]: r for r in D.load_universe() if r["type"] == "stock"}
    held_long = {sym for x in b.values() for sym, q in x["qty"].items() if q > 0}
    held_long |= {pp["symbol"] for pp in J.alpaca_get("/v2/positions") or [] if float(pp["qty"]) > 0}
    cands = [s_ for s_, x in flags.items() if x["grave"] >= 1 and s_ in uni and float(uni[s_]["adv_usd"]) >= p["min_dollar_volume"]
             and float(uni[s_]["price"]) >= 10 and s_ not in held_long and s_ not in b[sid]["qty"]
             and D.SECTOR_ETF.get(uni[s_]["sector"])]
    snaps = stock_snaps(cands)
    ranked = []
    for s_ in cands:
        sn = snaps.get(s_) or {}
        last, prev = last_price(sn), J.num((sn.get("prevDailyBar") or {}).get("c"))
        if not last or not prev or last >= prev:      # only stocks already falling
            continue
        a = J.safe(J.alpaca_get, "/v2/assets/%s" % s_) or {}
        if not (a.get("shortable") and a.get("easy_to_borrow")):
            continue
        ranked.append((last / prev - 1, s_, last))
    ranked.sort()
    out = []
    if not ranked:
        return ["Nessuna azienda oggi con notizie gravi, in calo e vendibile allo scoperto: nessun nuovo short."]
    for move, s_, px in ranked[:free]:
        etf = D.SECTOR_ETF[uni[s_]["sector"]]
        qty = int(p["slot_usd"] // px)
        if qty < 1:
            continue
        stop_px = round(px * (1 + p["stop_pct"]), 2)
        head = flags[s_]["headlines"][0] if flags[s_]["headlines"] else ""
        if not dry:
            try:
                o = send("POST", "/v2/orders", {"symbol": s_, "qty": str(qty), "side": "sell", "type": "market",
                                                "time_in_force": "day", "client_order_id": cid(sid, "e", s_)})
                f = wait_fill(o["id"])
                fpx = J.num(f.get("filled_avg_price")) or px
                stop_px = round(fpx * (1 + p["stop_pct"]), 2)
                send("POST", "/v2/orders", {"symbol": s_, "qty": str(qty), "side": "buy", "type": "stop", "stop_price": "%.2f" % stop_px,
                                            "time_in_force": "gtc", "client_order_id": cid(sid, "stop", s_)})
                send("POST", "/v2/orders", {"symbol": etf, "notional": "%.2f" % (qty * fpx), "side": "buy", "type": "market",
                                            "time_in_force": "day", "client_order_id": "%s-h-%s-%s" % (sid, s_, J.now_utc().strftime("%m%d%H%M%S"))})
            except Exception as e:
                out.append("⚠️ %s: ordine non riuscito (%s)" % (J.esc(s_), J.esc(err_text(e))))
                continue
        out.append("🔻 <b>Scommetto sul calo di %s</b> (%s)\n"
                   "Venduto allo scoperto: %d azioni a %s $ (%s)\n"
                   "📰 Perché: notizie gravi: «%s»; oggi %s.\n"
                   "🛡 Copertura: comprato %s di %s (il suo settore) → guadagno solo se %s fa peggio del settore.\n"
                   "🛑 Se sale fino a %s $ chiudo: perdita massima circa %s.\n"
                   "⏱ Chiudo comunque tra %d giorni di borsa." % (
                       J.esc(s_), J.esc(uni[s_]["name"][:32]), qty, J.price(px), J.usd(qty * px), J.esc(head[:120]),
                       J.pct(100 * move), J.usd(qty * px), etf, J.esc(s_), J.price(stop_px), J.usd(qty * px * p["stop_pct"]),
                       p["hold_days"]))
    return out or ["Nessun nuovo short oggi."]


# ---------------------------------------------------------------- stream 5: options with defined risk
def occ_parse(sym):
    m = re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", sym)
    if not m:
        return None
    return {"under": m.group(1), "expiry": "20%s-%s-%s" % (m.group(2), m.group(3), m.group(4)),
            "type": "call" if m.group(5) == "C" else "put", "strike": int(m.group(6)) / 1000}


def cmd_options(args):
    c = cfg()
    p, sid = c["list"]["op"], "op"
    dry = args.dry_run
    if not dry and not market_open():
        print("market closed: nothing to do")
        return 0
    b, _ = books(c)
    me = b[sid]
    out = []
    today = J.now_utc().astimezone(NY).date()
    # exits: doubled, halved, or close to expiry
    for sym, q in list(me["qty"].items()):
        info = occ_parse(sym)
        if not info or q <= 0:
            continue
        paid = me["cost"].get(sym, 0.0)
        now_px = me["prices"].get(sym)
        if now_px is None:
            continue
        worth = q * now_px * 100
        gain = worth / paid - 1 if paid else 0
        left = (dt.date.fromisoformat(info["expiry"]) - today).days
        why = ("è raddoppiata" if gain >= p["take_profit"] else "ha perso metà del valore" if gain <= -p["stop_loss"]
               else "mancano %d giorni alla scadenza" % left if left <= p["exit_days_before_expiry"] else None)
        if not why:
            continue
        if not dry:
            try:
                send("POST", "/v2/orders", {"symbol": sym, "qty": str(int(q)), "side": "sell", "type": "market",
                                            "time_in_force": "day", "client_order_id": cid(sid, "x", sym)})
            except Exception as e:
                out.append("⚠️ Vendita di %s non riuscita (%s)" % (sym, J.esc(err_text(e))))
                continue
        out.append("%s <b>Venduta</b> l'opzione %s su %s (%s): %s → <b>%s</b> (%s)" % (
            "✅" if gain > 0 else "🛑", info["type"], info["under"], OPTION_ETFS.get(info["under"], ""), why,
            J.usd(worth - paid, True), J.pct(100 * gain, True, 0)))
    # entries on the configured weekdays
    if today.weekday() in p["entry_weekdays"] or args.force:
        ps = paused()
        if is_paused(sid, ps):
            out.append("⏸ In pausa: nessuna nuova opzione.")
        elif auto_stop(sid, me["value"], c):
            out.append("⛔ Fermato per perdite: nessuna nuova opzione.")
        else:
            open_n = sum(1 for s_ in me["qty"] if occ_parse(s_))
            if open_n < p["max_open"]:
                out += option_entries(c, p, sid, me, p["max_open"] - open_n, dry, today)
    if not out:
        print("options: nothing to report")
        return 0
    text = header(sid, c, "scommesse a rischio limitato", dry) + "\n" + "\n\n".join(out)
    print(text if args.no_telegram else J.telegram(text))
    return 0


def option_entries(c, p, sid, me, free, dry, today):
    import data as D
    held_under = {occ_parse(s_)["under"] for s_ in me["qty"] if occ_parse(s_)}
    start = (today - dt.timedelta(days=45)).isoformat()
    bars = D.bars_multi(list(OPTION_ETFS), "1Day", start)
    mom = {}
    for s_, bs in bars.items():
        if len(bs) >= 22:
            mom[s_] = bs[-1]["c"] / bs[-22]["c"] - 1
    ranked = sorted(mom, key=lambda s_: -mom[s_])
    ideas = [(s_, "call") for s_ in ranked[:2]] + ([(ranked[-1], "put")] if ranked and mom[ranked[-1]] < 0 else [])
    ideas = [(s_, t) for s_, t in ideas if s_ not in held_under][:free]
    budget_cap = min(p["max_premium_usd"], 0.12 * me["value"])
    out = []
    for under, typ in ideas:
        px = bars[under][-1]["c"]
        pick = choose_contract(under, typ, px, today, p, budget_cap)
        if not pick:
            out.append("ℹ️ %s (%s): nessuna opzione liquida sotto i %s, salto." % (under, OPTION_ETFS[under], J.usd(budget_cap)))
            continue
        sym, ask, strike, expiry = pick
        cost = ask * 100
        e_ = dt.date.fromisoformat(expiry)
        expiry = "%d %s" % (e_.day, J.MESI[e_.month - 1])
        if not dry:
            try:
                send("POST", "/v2/orders", {"symbol": sym, "qty": "1", "side": "buy", "type": "limit",
                                            "limit_price": "%.2f" % ask, "time_in_force": "day",
                                            "client_order_id": cid(sid, "e", sym)})
            except Exception as e:
                out.append("⚠️ %s: ordine non riuscito (%s)" % (under, J.esc(err_text(e))))
                continue
        direction = "rialzo" if typ == "call" else "calo"
        out.append("🎯 <b>Scommessa sul %s di %s</b> (%s)\n"
                   "Comprata 1 opzione %s, scadenza %s, prezzo di riferimento %s $ (oggi %s $)\n"
                   "💰 Costo: <b>%s</b> = il massimo che posso perdere\n"
                   "📈 Perché: è il settore %s dell'ultimo mese (%s)\n"
                   "✅ Se raddoppia vendo (+%s) · 🛑 se dimezza vendo (−%s) · esco comunque %d giorni prima della scadenza" % (
                       direction, under, OPTION_ETFS[under], typ, expiry, J.price(strike), J.price(px), J.usd(cost),
                       "più forte" if typ == "call" else "più debole", J.pct(100 * mom[under]),
                       J.usd(cost * p["take_profit"]), J.usd(cost * p["stop_loss"]), p["exit_days_before_expiry"]))
    return out or ["Nessuna nuova opzione oggi."]


def choose_contract(under, typ, px, today, p, cap):
    """The listed contract 30-60 days out, closest to 2% out of the money, affordable and liquid."""
    lo, hi = (today + dt.timedelta(days=p["min_days"])).isoformat(), (today + dt.timedelta(days=p["max_days"])).isoformat()
    q = {"underlying_symbols": under, "type": typ, "expiration_date_gte": lo, "expiration_date_lte": hi, "limit": 200,
         "strike_price_gte": "%.2f" % (px * (0.85 if typ == "put" else 1.0)),
         "strike_price_lte": "%.2f" % (px * (1.0 if typ == "put" else 1.15))}
    cons = (J.safe(J.alpaca_get, "/v2/options/contracts", q) or {}).get("option_contracts") or []
    if not cons:
        return None
    exp = sorted({x["expiration_date"] for x in cons})[0]
    cons = [x for x in cons if x["expiration_date"] == exp]
    target = px * (1.02 if typ == "call" else 0.98)
    cons.sort(key=lambda x: abs(float(x["strike_price"]) - target))
    syms = [x["symbol"] for x in cons[:12]]
    snaps = (J.safe(J.alpaca_get, "/v1beta1/options/snapshots", {"symbols": ",".join(syms), "feed": "indicative"}, J.DATA) or {}).get("snapshots") or {}
    for x in cons[:12]:
        qt = (snaps.get(x["symbol"]) or {}).get("latestQuote") or {}
        ask, bid = J.num(qt.get("ap")), J.num(qt.get("bp"))
        if not ask or not bid or bid <= 0:
            continue
        mid = (ask + bid) / 2
        if (ask - bid) / mid > 0.35 or ask * 100 > cap:
            continue
        return x["symbol"], ask, float(x["strike_price"]), exp
    return None


# ---------------------------------------------------------------- league table
def spy_since(start):
    import data as D
    bs = D.bars_multi(["SPY"], "1Day", (dt.date.fromisoformat(start) - dt.timedelta(days=7)).isoformat()).get("SPY") or []
    before = [x for x in bs if x["t"][:10] < start]
    if not before or not bs:
        return None
    sn = stock_snaps(["SPY"]).get("SPY") or {}
    last = last_price(sn) or bs[-1]["c"]
    return last / before[-1]["c"] - 1


def history_path():
    return os.path.join(J.TRADER, "journal", "streams_history.jsonl")


def cmd_streams(args):
    c = cfg()
    b, _ = books(c)
    ps = paused()
    today = J.now_utc().astimezone(NY).date().isoformat()
    prev = {}
    try:
        for line in open(history_path()):
            r = json.loads(line)
            if r["date"] < today:
                prev = r
    except OSError:
        pass
    rows = []
    for sid, x in c["list"].items():
        v = b[sid]["value"]
        rows.append((v / x["budget"] - 1, sid, v))
        auto_stop(sid, v, c)
    rows.sort(reverse=True)
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    tot_b = sum(x["budget"] for x in c["list"].values())
    tot_v = sum(r[2] for r in rows)
    lines = []
    for i, (ret, sid, v) in enumerate(rows):
        x = c["list"][sid]
        bar = "▰" * max(0, min(5, int(round(abs(ret) * 100 / 2)))) or "·"
        mark = " ⏸" if is_paused(sid, ps) else ""
        lines.append("%s %s %-11s %9s %7s %s%s" % (medals[i], x["emoji"], x["name"], J.usd(round(v)), J.pct(100 * ret),
                                                 ("🟢" if ret >= 0 else "🔴") + bar, mark))
    spy = J.safe(spy_since, c["start_date"])
    title = "🏁 <b>LE 5 STRATEGIE</b> · %s%s" % (J.it_date(J.now_utc()), " · <b>settimana</b>" if args.weekly else "")
    out = [title, "<pre>%s</pre>" % J.esc("\n".join(lines))]
    if spy is not None:
        out.append("📈 S&amp;P 500 dallo stesso giorno: <b>%s</b>" % J.pct(100 * spy))
    out.append("💼 Totale: %s → <b>%s</b> (%s)" % (J.usd(tot_b), J.usd(round(tot_v)), J.pct(100 * (tot_v / tot_b - 1))))
    if prev.get("values"):
        moves = []
        for ret, sid, v in rows:
            pv = prev["values"].get(sid)
            if pv is not None:
                d_ = v - pv
                moves.append("%s %s" % (c["list"][sid]["emoji"], J.usd(round(d_), True)))
        if moves:
            out.append("📆 Da ieri: " + " · ".join(moves))
    held = []
    for sid in ("s1", "s2", "dt", "sh", "op"):
        n = len(b[sid]["qty"])
        if n:
            held.append("%s %d" % (c["list"][sid]["emoji"], n))
    if held:
        out.append("📂 Posizioni aperte: " + " · ".join(held))
    if ps:
        names = "tutte" if "all" in ps else ", ".join(c["list"][s_]["name"] for s_ in ps if s_ in c["list"])
        out.append("⏸ In pausa: %s (scrivi «riprendi» + nome)" % names)
    out.append("<i>Pausa di una sola strategia: rispondi «pausa short», «pausa opzioni»…</i>")
    text = "\n".join(out)
    if not args.dry_run:
        os.makedirs(os.path.dirname(history_path()), exist_ok=True)
        with open(history_path(), "a") as f:
            f.write(json.dumps({"date": today, "values": {sid: round(b[sid]["value"], 2) for sid in c["list"]}}) + "\n")
    print(text if args.no_telegram else J.telegram(text))
    return 0
