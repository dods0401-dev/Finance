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
   "themes": {"<theme>": {"tone": 0, "outlook": "in rafforzamento|stabile|in indebolimento",
                          "impact": "who is exposed and how (sectors, tickers, spillovers)", "why": "..."}},
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
OUTLOOK = {"in rafforzamento", "stabile", "in indebolimento"}


def load_themes():
    return J.load(os.path.join(D.RES, "themes.json"), {}) or {}


import re

_RX = {}


def has_any(text, words):
    """Whole-word (or whole-phrase) match, case-insensitive."""
    for w in words:
        rx = _RX.get(w)
        if rx is None:
            rx = _RX[w] = re.compile(r"(?<![a-z0-9])" + re.escape(w.strip().lower()) + r"(?![a-z0-9])")
        if rx.search(text):
            return True
    return False


def headline_text(n):
    return (n.get("headline", "") + " " + (n.get("summary") or "")).lower()


_NAMES = None


def about(n, sym):
    """Is this article really about sym? Yes if it tags at most 2 companies, or names it in the headline."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {r["symbol"]: r["name"] for r in D.load_universe()}
    syms = n.get("symbols") or []
    if len(syms) <= 2:
        return True
    h = n.get("headline", "")
    if re.search(r"(?<![A-Za-z])" + re.escape(sym) + r"(?![A-Za-z])", h):
        return True
    first = (_NAMES.get(sym) or "").split(" ")[0].strip(",.")
    return len(first) >= 4 and first.lower() in h.lower()


def theme_stats(news, m, themes, day):
    """News count, basket returns and top headlines per theme; heat vs the last 10 diaries."""
    past = []
    if os.path.isdir(CTX):
        for f in sorted(os.listdir(CTX))[-11:]:
            if f.endswith(".json") and f[:10] < day:
                past.append((J.load(os.path.join(CTX, f), {}) or {}).get("data", {}).get("themes") or {})
    out = {}
    for name, t in themes.items():
        kws = [k.lower() for k in t.get("keywords", [])]
        hits = [n for n in news if has_any(headline_text(n), kws)]
        b = [x for x in t.get("basket", []) if x in m]
        hist = [p[name]["news_count"] for p in past if name in p]
        avg = stx.mean(hist) if hist else None
        out[name] = {"news_count": len(hits), "news_avg_prev": round(avg, 1) if avg is not None else None,
                     "heat": round(len(hits) / avg, 2) if avg else None,
                     "basket_r1": med(m[x]["r1"] for x in b), "basket_r5": med(m[x]["r5"] for x in b),
                     "basket_r20": med(m[x]["r20"] for x in b),
                     "movers": sorted([[x, round(m[x]["r1"], 1)] for x in b if m[x]["r1"] is not None], key=lambda z: z[1])[:2]
                     + sorted([[x, round(m[x]["r1"], 1)] for x in b if m[x]["r1"] is not None], key=lambda z: -z[1])[:2],
                     "headlines": [{"t": n["created_at"][:16], "symbols": (n.get("symbols") or [])[:4],
                                    "headline": n["headline"][:160]} for n in hits[:8]]}
    return out


def red_flags(news, words):
    """Companies with risk headlines: 1 severe or 2+ warning headlines in 24h."""
    sev = [w.lower() for w in words.get("severe", [])]
    warn = [w.lower() for w in words.get("warning", [])]
    per = {}
    for n in news:
        h = n.get("headline", "").lower()
        level = "grave" if has_any(h, sev) else "attenzione" if has_any(h, warn) else None
        if not level:
            continue
        for sym in [x for x in (n.get("symbols") or []) if about(n, x)][:3]:
            x = per.setdefault(sym, {"grave": 0, "attenzione": 0, "headlines": []})
            x[level] += 1
            if len(x["headlines"]) < 4:
                x["headlines"].append(n["headline"][:160])
    return {k: v for k, v in per.items() if v["grave"] >= 1 or v["attenzione"] >= 2}


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
    th = load_themes()
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
                    "themes": theme_stats(news, m, th.get("themes", {}), day),
                    "red_flags": red_flags(news, th.get("red_flag_words", {})),
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
    if d.get("themes"):
        out += ["", "## Temi (notizie 24h, temperatura = notizie / media dei giorni precedenti; paniere di titoli esposti)"]
        for name, t in d["themes"].items():
            out.append("- %s: notizie %d%s, paniere 1g %s%% 5g %s%% 20g %s%%, movimenti %s" % (
                name, t["news_count"], (" (temperatura %.1fx)" % t["heat"]) if t.get("heat") else "",
                fmt(t["basket_r1"]), fmt(t["basket_r5"]), fmt(t["basket_r20"]),
                ", ".join("%s %+.1f" % (x, y) for x, y in t["movers"])))
            for h in t["headlines"][:5]:
                out.append("    · [%s] %s" % (",".join(h["symbols"]), h["headline"]))
    if d.get("red_flags"):
        out += ["", "## Bandierine rosse (indagini, cause, dimissioni... nelle ultime 24h)"]
        for sym, x in sorted(d["red_flags"].items(), key=lambda kv: -(kv[1]["grave"] * 3 + kv[1]["attenzione"]))[:15]:
            out.append("- %s: gravi %d, attenzione %d" % (sym, x["grave"], x["attenzione"]))
            for h in x["headlines"][:2]:
                out.append("    · " + h)
    if d["positions"]:
        out += ["", "## Posizioni aperte"]
        for p in d["positions"]:
            out.append("- %s (%s) P/L %s, 1g %s%% 5g %s%%" % (p["symbol"], p["sector"], p["unrealized_pl"],
                                                            fmt(p["metrics"].get("r1")), fmt(p["metrics"].get("r5"))))
            for h in p["headlines"]:
                out.append("    · " + h)
    return "\n".join(out)


def validate(a, sectors, positions, themes=()):
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
    for k in themes:
        v = (a.get("themes") or {}).get(k)
        if not v or v.get("tone") not in TONES or v.get("outlook") not in OUTLOOK or not v.get("impact") or not v.get("why"):
            errs.append("tema %s: tone, outlook (in rafforzamento|stabile|in indebolimento), impact, why" % k)
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
    errs = validate(ass, doc["data"]["sectors"], {p["symbol"] for p in doc["data"]["positions"]}, doc["data"].get("themes") or {})
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
    trend = {"in rafforzamento": "↗️ si rafforza", "stabile": "➡️ stabile", "in indebolimento": "↘️ si indebolisce"}
    risk = {"basso": "basso", "medio": "medio", "alto": "alto ⚠️"}
    out = ["🧭 <b>Il mercato stasera · %s</b>" % doc["date"],
           "%s %s" % (face.get(a["market"]["tone"], ""), J.esc(a["market"]["summary"])),
           "Rischio che la tendenza cambi: <b>%s</b>" % risk.get(a["market"]["regime_change_risk"], "")]
    if a.get("themes"):
        out += ["", "🌍 <b>Grandi temi</b>"]
        for name, x in a["themes"].items():
            t = (d.get("themes") or {}).get(name, {})
            hot = " 🔥" if (t.get("heat") or 0) >= 1.5 else ""
            out.append("%s <b>%s</b>%s: %s. %s" % (face.get(x["tone"], ""), J.esc(name), hot, trend.get(x["outlook"], ""),
                                                  J.esc(x["why"])))
    rows = []
    for sec, s in sorted(d["sectors"].items(), key=lambda kv: -(kv[1]["r5"] or 0)):
        x = a["sectors"].get(sec, {})
        rows.append("%-3s %-14s %6s%%%s" % (face.get(x.get("tone"), "?"), sec[:14], fmt(s["r5"]),
                                          "  ⚠" if x.get("trend_change_risk") == "alto" else ""))
    out += ["", "📊 <b>Settori</b> (andamento ultima settimana)", "<pre>%s</pre>" % J.esc("\n".join(rows)),
            "<i>🟢 clima positivo · 🔴 negativo · ⚠ possibile cambio di direzione</i>"]
    deep = [k for k, v in a["sectors"].items() if v.get("deep_dive")]
    for k in deep[:3]:
        out.append("🔎 <b>Da approfondire: %s</b>. %s" % (J.esc(k), J.esc(a["sectors"][k].get("why") or "")))
    flags = sorted((d.get("red_flags") or {}).items(), key=lambda kv: -(kv[1]["grave"] * 3 + kv[1]["attenzione"]))[:5]
    if flags:
        out.append("🚩 <b>Aziende con notizie preoccupanti</b> (non le compro per qualche giorno): " + J.esc(", ".join(k for k, _ in flags)))
    for k, v in (a.get("positions") or {}).items():
        word = {"tenere": "la terrei", "chiudere": "la chiuderei", "neutro": "nessuna preferenza"}[v["hold_bias"]]
        out.append("📂 %s: %s. %s" % (J.esc(k), word, J.esc(v.get("why") or "")))
    if a.get("watch"):
        out.append("👀 Da guardare domani: " + J.esc(", ".join(a["watch"][:10])))
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
    s.add_argument("--news-pages", type=int, default=80)
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
