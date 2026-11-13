#!/usr/bin/env python3
"""trader-research context diary: market data + exogenous factors, one file a day.

Commands
  snapshot [--date YYYY-MM-DD] [--news-pages 30]
        Builds research/context/DATE.json from data: market regime (SPY/QQQ/IWM trend,
        VIX, breadth), every sector (median 1/5/20-day returns, % above the 50-day average,
        relative volume, leaders and laggards, sector ETF), the last 24h of Alpaca news
        grouped by sector, and the news on open positions. Prints a compact digest for
        Claude to read. "assessment" is left empty.
  assess DATE --from FILE
        Merges Claude's qualitative assessment (JSON, schema below) into DATE.json and
        sends the Telegram digest. Validated: unknown sectors or values are refused.
  evaluate [--horizon 5]
        Scores past assessments against what happened next: did a sector rated positive
        (tone > 0) beat the median sector over the next N trading days? Hit rate, average
        excess return by tone, and the same for "trend_change_risk: alto".
  show [DATE]   Prints a stored day.

Assessment schema (all keys required, tone in -2..2, risk in basso|medio|alto):
  {"market": {"tone": 0, "regime_change_risk": "basso", "summary": "..."},
   "sectors": {"<sector>": {"tone": 1, "trend_change_risk": "medio", "deep_dive": false,
                            "drivers": "...", "why": "..."}},
   "positions": {"<SYM>": {"hold_bias": "tenere|chiudere|neutro", "why": "..."}},
   "watch": ["SYM", ...]}
Read only towards the broker. Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import statistics as stx
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "trader-journal"))
import data as D  # noqa: E402
import journal as J  # noqa: E402

CTX = os.path.join(D.RES, "context")
TONES = {-2, -1, 0, 1, 2}
RISKS = {"basso", "medio", "alto"}
BIAS = {"tenere", "chiudere", "neutro"}


def pct(a, b):
    return 100.0 * (a / b - 1) if b else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(stx.median(xs), 2) if xs else None


def metrics(bs):
    if len(bs) < 22:
        return None
    c = [b["c"] for b in bs]
    v = [b["v"] for b in bs]
    sma50 = stx.mean(c[-50:]) if len(c) >= 50 else None
    sma200 = stx.mean(c[-200:]) if len(c) >= 200 else None
    return {"close": c[-1], "r1": pct(c[-1], c[-2]), "r5": pct(c[-1], c[-6]), "r20": pct(c[-1], c[-21]),
            "a50": (c[-1] > sma50) if sma50 else None, "a200": (c[-1] > sma200) if sma200 else None,
            "hi20": c[-1] >= max(c[-20:]), "lo20": c[-1] <= min(c[-20:]),
            "rvol": v[-1] / stx.mean(v[-21:-1]) if stx.mean(v[-21:-1]) else None}


def news_24h(pages):
    out, token = [], None
    start = (J.now_utc() - dt.timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    for _ in range(pages):
        p = {"start": start, "limit": 50, "sort": "desc"}
        if token:
            p["page_token"] = token
        try:
            d = D.gj(D.DATA + "/v1beta1/news?" + urllib.parse.urlencode(p), D.H)
        except Exception:
            break
        out += d.get("news") or []
        token = d.get("next_page_token")
        if not token:
            break
    return out


def cmd_snapshot(a):
    os.makedirs(CTX, exist_ok=True)
    day = a.date or J.now_utc().astimezone(J.NY).date().isoformat()
    uni = D.load_universe()
    if not uni:
        print("research/universe.csv missing: run data.py universe first")
        return 1
    sec_of = {r["symbol"]: r["sector"] for r in uni}
    syms = [r["symbol"] for r in uni] + [s for s in list(D.SECTOR_ETF.values()) + D.MARKET_ETF if s not in sec_of]
    start = (dt.date.fromisoformat(day) - dt.timedelta(days=320)).isoformat()
    today = J.now_utc().astimezone(J.NY).date().isoformat()
    # the free data plan refuses SIP bars of the last 15 minutes: no end date for today
    end = None if day >= today else (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    m = {}
    for i in range(0, len(syms), 100):
        try:
            got = D.bars_multi(syms[i:i + 100], "1Day", start, end)
        except Exception as e:
            print("bars error", type(e).__name__)
            continue
        for s, bs in got.items():
            x = metrics(bs)
            if x:
                m[s] = x
    stocks = [s for s in m if sec_of.get(s) not in (None, "ETF")]
    vix = J.safe(J.yahoo_quote, "^VIX")
    regime = {k: {f: (round(v, 2) if isinstance(v, float) else v) for f, v in m[k].items()} for k in ("SPY", "QQQ", "IWM") if k in m}
    regime["VIX"] = {"close": vix[0], "r1": round(pct(vix[0], vix[1]), 2)} if vix and vix[0] else None
    regime["breadth"] = {"pct_above_50d": round(100 * sum(1 for s in stocks if m[s]["a50"]) / max(1, len(stocks)), 1),
                         "pct_above_200d": round(100 * sum(1 for s in stocks if m[s]["a200"]) / max(1, len(stocks)), 1),
                         "new_20d_highs": sum(1 for s in stocks if m[s]["hi20"]),
                         "new_20d_lows": sum(1 for s in stocks if m[s]["lo20"]), "stocks": len(stocks)}
    news = news_24h(a.news_pages)
    sectors = {}
    for sec in sorted({sec_of[s] for s in stocks}):
        ss = [s for s in stocks if sec_of[s] == sec]
        ranked = sorted(ss, key=lambda s: -(m[s]["r5"] or 0))
        etf = D.SECTOR_ETF.get(sec)
        nws = [n for n in news if any(sec_of.get(x) == sec for x in n.get("symbols") or [])]
        sectors[sec] = {"n": len(ss), "r1": med(m[s]["r1"] for s in ss), "r5": med(m[s]["r5"] for s in ss),
                        "r20": med(m[s]["r20"] for s in ss),
                        "pct_above_50d": round(100 * sum(1 for s in ss if m[s]["a50"]) / len(ss), 1),
                        "rvol": med(m[s]["rvol"] for s in ss),
                        "leaders": [[s, round(m[s]["r5"], 1)] for s in ranked[:5]],
                        "laggards": [[s, round(m[s]["r5"], 1)] for s in ranked[-3:]],
                        "etf": {"symbol": etf, **{k: round(m[etf][k], 2) for k in ("r1", "r5", "r20") if m.get(etf) and m[etf][k] is not None}} if etf else None,
                        "news_count": len(nws),
                        "headlines": [{"t": n["created_at"][:16], "symbols": [x for x in n.get("symbols") or [] if sec_of.get(x) == sec][:4],
                                       "headline": n["headline"][:160], "source": n.get("source")} for n in nws[:8]]}
    positions = []
    for p in J.safe(J.alpaca_get, "/v2/positions") or []:
        s = p["symbol"]
        positions.append({"symbol": s, "sector": sec_of.get(s), "qty": p["qty"], "unrealized_pl": p.get("unrealized_pl"),
                          "metrics": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in (m.get(s) or {}).items()},
                          "headlines": [n["headline"][:160] for n in news if s in (n.get("symbols") or [])][:6]})
    doc = {"date": day, "created_at": J.now_utc().isoformat(timespec="seconds"),
           "data": {"regime": regime, "sectors": sectors, "positions": positions,
                    "news_total": len(news), "universe": len(uni)}, "assessment": None}
    path = os.path.join(CTX, day + ".json")
    old = J.load(path, {}) or {}
    if old.get("assessment"):
        doc["assessment"] = old["assessment"]
    json.dump(doc, open(path, "w"), indent=1, ensure_ascii=False)
    print(digest(doc))
    print("\nsaved", path)
    return 0


def fmt(x, d=1):
    return "—" if x is None else ("%+.*f" % (d, x))


def digest(doc):
    d = doc["data"]
    r = d["regime"]
    b = r.get("breadth", {})
    out = ["# Contesto %s" % doc["date"], "",
           "Mercato: " + " | ".join("%s %s%% (5g %s%%, 20g %s%%) %s50 %s200" % (
               k, fmt(r[k]["r1"]), fmt(r[k]["r5"]), fmt(r[k]["r20"]), "▲" if r[k]["a50"] else "▼", "▲" if r[k]["a200"] else "▼")
               for k in ("SPY", "QQQ", "IWM") if k in r),
           "VIX %s (%s%%) | ampiezza: %s%% sopra 50g, %s%% sopra 200g, max 20g %s / min 20g %s su %s titoli" % (
               (r.get("VIX") or {}).get("close"), fmt((r.get("VIX") or {}).get("r1")), b.get("pct_above_50d"),
               b.get("pct_above_200d"), b.get("new_20d_highs"), b.get("new_20d_lows"), b.get("stocks")),
           "Notizie 24h: %d" % d["news_total"], "", "## Settori (mediana titoli; ETF)"]
    for sec, s in sorted(d["sectors"].items(), key=lambda kv: -(kv[1]["r5"] or 0)):
        e = s.get("etf") or {}
        out.append("- %s (%d): 1g %s%% 5g %s%% 20g %s%%, sopra 50g %s%%, vol rel %s, %s %s%% 5g, notizie %d, leader %s" % (
            sec, s["n"], fmt(s["r1"]), fmt(s["r5"]), fmt(s["r20"]), s["pct_above_50d"], s["rvol"], e.get("symbol") or "", fmt(e.get("r5")),
            s["news_count"], ", ".join("%s %+.1f" % (x, y) for x, y in s["leaders"][:3])))
        for h in s["headlines"][:4]:
            out.append("    · [%s] %s (%s)" % (",".join(h["symbols"]), h["headline"], h["source"]))
    if d["positions"]:
        out += ["", "## Posizioni aperte"]
        for p in d["positions"]:
            out.append("- %s (%s) P/L %s, 1g %s%% 5g %s%%" % (p["symbol"], p["sector"], p["unrealized_pl"],
                                                            fmt(p["metrics"].get("r1")), fmt(p["metrics"].get("r5"))))
            for h in p["headlines"]:
                out.append("    · " + h)
    return "\n".join(out)


def validate(a, sectors, positions):
    errs = []
    mk = a.get("market") or {}
    if mk.get("tone") not in TONES or mk.get("regime_change_risk") not in RISKS or not mk.get("summary"):
        errs.append("market: tone -2..2, regime_change_risk basso|medio|alto, summary")
    for k, v in (a.get("sectors") or {}).items():
        if k not in sectors:
            errs.append("settore sconosciuto: %s" % k)
        elif v.get("tone") not in TONES or v.get("trend_change_risk") not in RISKS or not isinstance(v.get("deep_dive"), bool):
            errs.append("settore %s: tone, trend_change_risk, deep_dive" % k)
    missing = [s for s in sectors if s not in (a.get("sectors") or {})]
    if missing:
        errs.append("settori mancanti: %s" % ", ".join(missing))
    for k, v in (a.get("positions") or {}).items():
        if k not in positions or v.get("hold_bias") not in BIAS:
            errs.append("posizione %s: hold_bias tenere|chiudere|neutro" % k)
    return errs


def cmd_assess(a):
    path = os.path.join(CTX, a.date + ".json")
    doc = J.load(path)
    if not doc:
        print("no snapshot for", a.date)
        return 1
    ass = json.load(open(a.from_file))
    errs = validate(ass, doc["data"]["sectors"], {p["symbol"] for p in doc["data"]["positions"]})
    if errs:
        print("assessment refused:\n- " + "\n- ".join(errs))
        return 1
    ass["assessed_at"] = J.now_utc().isoformat(timespec="seconds")
    doc["assessment"] = ass
    json.dump(doc, open(path, "w"), indent=1, ensure_ascii=False)
    print(J.telegram(telegram_card(doc)))
    return 0


def telegram_card(doc):
    d, a = doc["data"], doc["assessment"]
    r = d["regime"]
    face = {2: "🟢🟢", 1: "🟢", 0: "⚪", -1: "🔴", -2: "🔴🔴"}
    rows = []
    for sec, s in sorted(d["sectors"].items(), key=lambda kv: -(kv[1]["r5"] or 0)):
        x = a["sectors"].get(sec, {})
        rows.append("%-3s %-14s %6s%% %s%s" % (face.get(x.get("tone"), "?"), sec[:14], fmt(s["r5"]),
                                               "⚠" if x.get("trend_change_risk") == "alto" else " ",
                                               "🔎" if x.get("deep_dive") else ""))
    out = ["🧭 <b>CONTESTO · %s</b>" % doc["date"],
           "%s Mercato: %s" % (face.get(a["market"]["tone"], ""), J.esc(a["market"]["summary"])),
           "SPY 5g %s%% · VIX %s · %s%% titoli sopra media 50g" % (fmt((r.get("SPY") or {}).get("r5")),
                                                                  (r.get("VIX") or {}).get("close"), r["breadth"]["pct_above_50d"]),
           "Rischio cambio di regime: <b>%s</b>" % a["market"]["regime_change_risk"],
           "<pre>%s</pre>" % J.esc("\n".join(rows)),
           "<i>tono 🟢/🔴 · ⚠ rischio inversione alto · 🔎 da approfondire · % = 5 giorni</i>"]
    deep = [k for k, v in a["sectors"].items() if v.get("deep_dive")]
    for k in deep[:3]:
        out.append("🔎 <b>%s</b>: %s" % (J.esc(k), J.esc(a["sectors"][k].get("why") or a["sectors"][k].get("drivers") or "")))
    for k, v in (a.get("positions") or {}).items():
        out.append("📂 %s → <b>%s</b>: %s" % (J.esc(k), v["hold_bias"], J.esc(v.get("why") or "")))
    if a.get("watch"):
        out.append("👀 Da guardare: " + J.esc(", ".join(a["watch"][:10])))
    return "\n".join(out)


def cmd_evaluate(a):
    files = sorted(f for f in os.listdir(CTX) if f.endswith(".json")) if os.path.isdir(CTX) else []
    docs = [J.load(os.path.join(CTX, f)) for f in files]
    docs = [d for d in docs if d]
    rows = []
    for i, d in enumerate(docs):
        if not d.get("assessment") or i + a.horizon >= len(docs):
            continue
        fut = docs[i + a.horizon]["data"]["sectors"]
        allr = [s["r5"] for s in fut.values() if s.get("r5") is not None]
        if not allr:
            continue
        base = stx.median(allr)
        for sec, x in d["assessment"]["sectors"].items():
            if sec in fut and fut[sec].get("r5") is not None:
                rows.append((x["tone"], x["trend_change_risk"], fut[sec]["r5"] - base))
    if len(rows) < 20:
        print("Storico troppo corto: %d valutazioni con esito noto (servono settimane di diario)." % len(rows))
        return 0
    pos = [e for t, _, e in rows if t > 0]
    neg = [e for t, _, e in rows if t < 0]
    hit = sum(1 for t, _, e in rows if t != 0 and (t > 0) == (e > 0)) / max(1, sum(1 for t, _, _ in rows if t != 0))
    print("valutazioni: %d | tono>0: extra %+.2f%% (n=%d) | tono<0: extra %+.2f%% (n=%d) | hit rate %.0f%%" % (
        len(rows), stx.mean(pos) if pos else 0, len(pos), stx.mean(neg) if neg else 0, len(neg), 100 * hit))
    for rk in ("alto", "medio", "basso"):
        xs = [abs(e) for _, r, e in rows if r == rk]
        if xs:
            print("rischio inversione %s: movimento medio successivo %.2f%% (n=%d)" % (rk, stx.mean(xs), len(xs)))
    return 0


def cmd_show(a):
    day = a.date or sorted(os.listdir(CTX))[-1][:-5]
    doc = J.load(os.path.join(CTX, day + ".json"))
    print(digest(doc))
    if doc.get("assessment"):
        print("\nassessment:", json.dumps(doc["assessment"], ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--date")
    s.add_argument("--news-pages", type=int, default=30)
    s = sub.add_parser("assess")
    s.add_argument("date")
    s.add_argument("--from", dest="from_file", required=True)
    s = sub.add_parser("evaluate")
    s.add_argument("--horizon", type=int, default=5)
    s = sub.add_parser("show")
    s.add_argument("date", nargs="?")
    a = ap.parse_args(argv)
    return {"snapshot": cmd_snapshot, "assess": cmd_assess, "evaluate": cmd_evaluate, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
