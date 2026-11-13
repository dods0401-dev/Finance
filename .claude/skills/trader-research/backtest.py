#!/usr/bin/env python3
"""trader-research backtests on the cached daily bars (research/cache/1Day).

  hold [--split 2022-01-01] [--min-adv 2e7]
      The evening decision: a stock closed a winning day (close > open). Is it worth
      holding to the next day instead of closing at the close? Measures, for that day
      and for subsets defined by what is known at the close (close near the high, relative
      volume, trend, extension, market regime), the next-day outcome: overnight gap
      (next open), next close, and next close with a stop at the day's low. Holding adds
      no extra round trip (the position is closed either way), so the comparison is
      gross. In sample (before --split) and out of sample (after) are shown apart.
      Survivorship bias: the universe is today's listed stocks, which flatters returns;
      compare subsets against the baseline, not against zero.
"""
import argparse
import csv
import gzip
import os
import statistics as stx
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data as D  # noqa: E402


def load(sym):
    p = D.cache_path("1Day", sym)
    if not os.path.exists(p):
        return None
    with gzip.open(p, "rt") as f:
        r = list(csv.DictReader(f))
    return [(x["t"][:10], float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), float(x["v"])) for x in r]


def summary(xs):
    if len(xs) < 50:
        return "n=%d (pochi)" % len(xs)
    m = stx.mean(xs)
    se = stx.pstdev(xs) / len(xs) ** 0.5
    return "n=%6d media %+6.1f bps ±%4.1f  positivi %4.1f%%" % (len(xs), m * 1e4, se * 1e4, 100 * sum(x > 0 for x in xs) / len(xs))


def cmd_hold(a):
    uni = [r for r in D.load_universe() if r["type"] == "stock" and float(r["adv_usd"]) >= a.min_adv]
    spy = {d: (o, h, l, c) for d, o, h, l, c, v in (load("SPY") or [])}
    spy_days = sorted(spy)
    spy_sma200 = {}
    closes = []
    for d in spy_days:
        closes.append(spy[d][3])
        if len(closes) >= 200:
            spy_sma200[d] = sum(closes[-200:]) / 200
    rows = []
    for r in uni:
        b = load(r["symbol"])
        if not b or len(b) < 260:
            continue
        c = [x[4] for x in b]
        v = [x[5] for x in b]
        for i in range(200, len(b) - 3):
            d, o, h, l, cl, vol = b[i]
            if cl <= o or h <= l or cl < 5:
                continue
            avgv = sum(v[i - 20:i]) / 20
            sma20 = sum(c[i - 20:i + 1]) / 21
            sma50 = sum(c[i - 49:i + 1]) / 50
            sma200 = sum(c[i - 199:i + 1]) / 200
            nd = b[i + 1]
            stop_hit = nd[3] <= l
            nxt_stop = (min(nd[1], l) / cl - 1) if stop_hit else (nd[4] / cl - 1)
            sp = spy.get(d)
            rows.append({"d": d, "sector": r["sector"], "roc": cl / o - 1, "clv": (cl - l) / (h - l),
                         "rvol": vol / avgv if avgv else 0, "t50": cl > sma50, "t200": cl > sma200,
                         "ext": cl / sma20 - 1, "spy_up": bool(sp and d in spy_sma200 and sp[3] > spy_sma200[d]),
                         "spy_day": (sp[3] / sp[0] - 1) if sp else 0,
                         "on": nd[1] / cl - 1, "n1": nd[4] / cl - 1, "n1s": nxt_stop, "n3": b[i + 3][4] / cl - 1})
    print("giornate positive analizzate:", len(rows), "| titoli:", len(uni))
    F = [("tutte le giornate positive (base)", lambda x: True),
         ("chiusura nel 20% alto del range", lambda x: x["clv"] >= 0.8),
         ("chiusura nel 20% basso del range", lambda x: x["clv"] <= 0.2),
         ("guadagno del giorno > 3%", lambda x: x["roc"] > 0.03),
         ("volume relativo >= 2x", lambda x: x["rvol"] >= 2),
         ("trend: sopra media 50 e 200", lambda x: x["t50"] and x["t200"]),
         ("sotto media 200 (controtrend)", lambda x: not x["t200"]),
         ("molto esteso: > +10% sopra media 20", lambda x: x["ext"] > 0.10),
         ("mercato sano: SPY sopra media 200", lambda x: x["spy_up"]),
         ("mercato debole: SPY sotto media 200", lambda x: not x["spy_up"]),
         ("COMBO: chiusura alta + trend + SPY sano", lambda x: x["clv"] >= 0.8 and x["t50"] and x["t200"] and x["spy_up"]),
         ("COMBO + volume >= 1.5x + non esteso (<10%)", lambda x: x["clv"] >= 0.8 and x["t50"] and x["t200"] and x["spy_up"]
          and x["rvol"] >= 1.5 and x["ext"] < 0.10),
         ("COMBO + guadagno 1-4%", lambda x: x["clv"] >= 0.8 and x["t50"] and x["t200"] and x["spy_up"] and 0.01 <= x["roc"] <= 0.04)]
    for period, sel in (("IN SAMPLE (< %s)" % a.split, lambda x: x["d"] < a.split),
                        ("OUT OF SAMPLE (>= %s)" % a.split, lambda x: x["d"] >= a.split)):
        P = [x for x in rows if sel(x)]
        print("\n==== %s: %d giornate" % (period, len(P)))
        for name, f in F:
            S = [x for x in P if f(x)]
            print("%-44s notte  %s" % (name, summary([x["on"] for x in S])))
            print("%-44s giorno %s" % ("", summary([x["n1"] for x in S])))
            print("%-44s +stop  %s" % ("", summary([x["n1s"] for x in S])))
    return 0


def fetch_news(sym, since):
    """Alpaca news archive for one symbol, cached in research/cache/news/SYM.json.gz."""
    import json as _j
    import urllib.parse as _u
    path = os.path.join(D.CACHE, "news", sym + ".json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            return _j.load(f)
    out, tok = [], None
    while True:
        q = {"symbols": sym, "start": since + "T00:00:00Z", "limit": 50, "sort": "asc"}
        if tok:
            q["page_token"] = tok
        try:
            d = D.gj(D.DATA + "/v1beta1/news?" + _u.urlencode(q), D.H)
        except Exception:
            break
        out += [{"t": n["created_at"], "h": n["headline"], "s": n.get("symbols") or []} for n in d.get("news") or []]
        tok = d.get("next_page_token")
        if not tok:
            break
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt") as f:
        _j.dump(out, f)
    return out


def cmd_news(a):
    """Event study: what happens to a stock after red-flag headlines or a sudden news spike?"""
    import json as _j
    sys.path.insert(0, HERE)
    import context as C
    words = (_j.load(open(os.path.join(D.RES, "themes.json"))) or {}).get("red_flag_words", {})
    sev, warn = words.get("severe", []), words.get("warning", [])
    uni = [r for r in D.load_universe() if r["type"] == "stock" and r["sector"] in D.SECTOR_ETF][:a.symbols]
    etf = {sec: {d: c for d, o, h, l, c, v in (load(t) or [])} for sec, t in D.SECTOR_ETF.items()}
    ev = {"grave": [], "attenzione": [], "picco notizie": [], "picco + calo del giorno": [], "picco + rialzo del giorno": []}
    for k, r in enumerate(uni):
        sym = r["symbol"]
        news = fetch_news(sym, a.since)
        bars = load(sym)
        if not bars or not news:
            continue
        print("…", k + 1, "/", len(uni), sym, len(news), "notizie", flush=True)
        idx = {b[0]: i for i, b in enumerate(bars)}
        days = [b[0] for b in bars]
        per_day, flag = {}, {}
        for n in news:
            # news after 16:00 New York belongs to the next session: approximate with UTC 21:00
            d = n["t"][:10] if n["t"][11:13] < "21" else None
            if d is None:
                nxt = [x for x in days if x > n["t"][:10]][:1]
                d = nxt[0] if nxt else None
            if not d or d not in idx:
                continue
            per_day[d] = per_day.get(d, 0) + 1
            if not C.about({"symbols": n["s"], "headline": n["h"]}, sym):
                continue
            h = n["h"].lower()
            lvl = "grave" if C.has_any(h, sev) else "attenzione" if C.has_any(h, warn) else None
            if lvl and flag.get(d) != "grave":
                flag[d] = lvl
        ec = etf.get(r["sector"]) or {}
        last_flag = -99
        for i in range(25, len(bars) - 21):
            d = bars[i][0]
            if d < a.since or d not in ec or bars[i + 20][0] not in ec:
                continue
            c0 = bars[i][4]
            def ab(j):
                e0, e1 = ec.get(d), ec.get(bars[i + j][0])
                return (bars[i + j][4] / c0 - 1) - ((e1 / e0 - 1) if e0 and e1 else 0)
            day_ret = c0 / bars[i - 1][4] - 1
            if d in flag and i - last_flag > 10:   # first flag after 10 quiet sessions
                ev[flag[d]].append((ab(1), ab(5), ab(20), day_ret))
                last_flag = i
            elif d in flag:
                last_flag = i
            base = [per_day.get(bars[j][0], 0) for j in range(i - 20, i)]
            avg = sum(base) / 20
            if per_day.get(d, 0) >= max(5, 3 * avg):
                ev["picco notizie"].append((ab(1), ab(5), ab(20), day_ret))
                ev["picco + calo del giorno" if day_ret < 0 else "picco + rialzo del giorno"].append((ab(1), ab(5), ab(20), day_ret))
    print("\nRendimento del titolo MENO il suo settore, dalla chiusura del giorno dell'evento")
    for k, xs in ev.items():
        if len(xs) < 30:
            print("%-26s n=%d (pochi)" % (k, len(xs)))
            continue
        def m(j):
            v = [x[j] for x in xs]
            return "%+5.2f%% ±%.2f" % (100 * stx.mean(v), 100 * stx.pstdev(v) / len(v) ** 0.5)
        print("%-26s n=%5d | giorno dell'evento %s | 1 giorno dopo %s | 5 giorni %s | 20 giorni %s" % (
            k, len(xs), m(3), m(0), m(1), m(2)))
    return 0


def cmd_momentum(a):
    """Monthly relative-strength portfolios on 10 years of daily bars (stocks only)."""
    import math
    from array import array
    spy = load("SPY")
    dates = [b[0] for b in spy]
    di = {d: i for i, d in enumerate(dates)}
    T = len(dates)
    uni = [r for r in D.load_universe() if r["type"] == "stock"]
    syms, sec, px, dv = [], [], [], []
    for r in uni:
        b = load(r["symbol"])
        if not b or len(b) < 300:
            continue
        c = array("d", [math.nan]) * T
        v = array("d", [0.0]) * T
        for d, o, h, l, cl, vol in b:
            i = di.get(d)
            if i is not None:
                c[i], v[i] = cl, cl * vol
        syms.append(r["symbol"]); sec.append(r["sector"]); px.append(c); dv.append(v)
    N = len(syms)
    print("azioni con storico:", N, "| giorni:", T, dates[0], "→", dates[-1])
    spyc = [b[4] for b in spy]
    # month starts: first trading day of each month; signals use the previous close
    rebal = [i for i in range(1, T) if dates[i][5:7] != dates[i - 1][5:7] and i > 260]

    def ret(k, i, back, skip=0):
        a0, a1 = px[k][i - back], px[k][i - skip]
        return a1 / a0 - 1 if a0 == a0 and a1 == a1 and a0 > 0 else None

    def liquid(k, i):
        c = px[k][i]
        if not (c == c) or c < 10:
            return False
        return sum(dv[k][i - 20:i]) / 20 >= a.min_dv

    def picks(i, mode, n):
        cand = [k for k in range(N) if liquid(k, i)]
        mom = {}
        for k in cand:
            m = ret(k, i, 252, 21) if mode != "short" else ret(k, i, 126, 21)
            if m is not None:
                mom[k] = m
        if mode == "sector":
            bys = {}
            for k in mom:
                if sec[k] not in ("Altro",):
                    r6 = ret(k, i, 126)
                    if r6 is not None:
                        bys.setdefault(sec[k], []).append(r6)
            top = sorted(bys, key=lambda s_: -stx.median(bys[s_]))[:3]
            pool = [k for k in mom if sec[k] in top]
            per = max(1, n // 3)
            out = []
            for s_ in top:
                out += sorted([k for k in pool if sec[k] == s_], key=lambda k: -mom[k])[:per]
            return out
        return sorted(mom, key=lambda k: -mom[k])[:n]

    def run(mode, n, regime=False, stop=None, cost=0.001):
        eq, hold, entry = 1.0, [], {}
        curve = [1.0] * T
        start = rebal[0]
        prev = start
        nxt = set(rebal)
        for i in range(start, T):
            if i in nxt:
                i0 = i - 1
                go = (not regime) or spyc[i0] > sum(spyc[i0 - 199:i0 + 1]) / 200
                new = picks(i0, mode, n) if go else []
                turnover = len(set(new) ^ set(hold)) / max(1, n)
                eq *= (1 - cost * turnover)
                hold = new
                entry = {k: px[k][i0] for k in hold}
            # daily P/L, equal weight of the slots (cash slots earn 0)
            if hold:
                rs = []
                for k in list(hold):
                    p0, p1 = px[k][i - 1], px[k][i]
                    if p0 == p0 and p1 == p1 and p0 > 0:
                        r_ = p1 / p0 - 1
                        if stop and entry.get(k) and p1 < entry[k] * (1 - stop):
                            hold.remove(k)
                            eq_cost = cost / 2
                            rs.append(r_ - eq_cost)
                            continue
                        rs.append(r_)
                    else:
                        rs.append(0.0)
                eq *= 1 + sum(rs) / n
            curve[i] = eq
        return curve[start:], dates[start:]

    def ew_universe():
        eq, curve = 1.0, []
        start = rebal[0]
        members = []
        for i in range(start, T):
            if i in set(rebal):
                members = [k for k in range(N) if liquid(k, i - 1)]
            rs = []
            for k in members:
                p0, p1 = px[k][i - 1], px[k][i]
                if p0 == p0 and p1 == p1 and p0 > 0:
                    rs.append(p1 / p0 - 1)
            eq *= 1 + (sum(rs) / len(rs) if rs else 0)
            curve.append(eq)
        return curve, dates[start:]

    def stats_(curve, ds, frm, to):
        idx = [j for j, d in enumerate(ds) if frm <= d < to]
        if len(idx) < 50:
            return None
        c = [curve[j] for j in idx]
        yrs = len(c) / 252
        cagr = (c[-1] / c[0]) ** (1 / yrs) - 1
        dr = [c[j] / c[j - 1] - 1 for j in range(1, len(c))]
        vol = stx.pstdev(dr) * 252 ** 0.5
        peak, mdd = c[0], 0
        for x in c:
            peak = max(peak, x)
            mdd = min(mdd, x / peak - 1)
        return cagr, vol, mdd, (cagr / vol if vol else 0)

    spy_curve = [spyc[i] / spyc[rebal[0]] for i in range(rebal[0], T)]
    variants = [("S&P 500 (solo comprare e tenere)", (spy_curve, dates[rebal[0]:])),
                ("Tutte le azioni liquide in parti uguali", ew_universe())]
    for name, kw in (("Top 20 per forza (12 mesi)", dict(mode="mom", n=20)),
                     ("Top 10 per forza (12 mesi)", dict(mode="mom", n=10)),
                     ("Top 20 per forza (6 mesi)", dict(mode="short", n=20)),
                     ("3 settori più forti, top 7 per settore", dict(mode="sector", n=21)),
                     ("Top 20 + filtro mercato", dict(mode="mom", n=20, regime=True)),
                     ("Top 20 + filtro mercato + stop -15%", dict(mode="mom", n=20, regime=True, stop=0.15)),
                     ("Settori + filtro mercato + stop -15%", dict(mode="sector", n=21, regime=True, stop=0.15))):
        variants.append((name, run(**kw)))
    if a.blends:
        base = dict(variants)
        spyv = base["S&P 500 (solo comprare e tenere)"]
        for mname in ("Top 10 per forza (12 mesi)", "Top 20 per forza (12 mesi)"):
            mv, ds = base[mname]
            for w in (0.7, 0.5, 0.4):
                c, out = 1.0, []
                for j in range(len(ds)):
                    if j:
                        c *= 1 + w * (spyv[0][j] / spyv[0][j - 1] - 1) + (1 - w) * (mv[j] / mv[j - 1] - 1)
                    out.append(c)
                variants.append(("MIX %d%% S&P / %d%% %s" % (w * 100, (1 - w) * 100, mname.split(" per")[0]), (out, ds)))
        variants = [v for v in variants if v[0].startswith("MIX") or v[0].startswith("S&P") or v[0].startswith("Top 10 per") or v[0].startswith("Top 20 per forza (12")]
    for frm, to, lab in (("2016-01-01", "2022-01-01", "2017–2021 (calibrazione)"),
                         ("2022-01-01", "2099-01-01", "2022–oggi (verifica)"),
                         ("2016-01-01", "2099-01-01", "tutto il periodo")):
        print("\n=== %s ===" % lab)
        print("%-44s %9s %9s %10s %7s" % ("", "rend/anno", "oscill.", "calo max", "qualità"))
        for name, (cv, ds) in variants:
            st_ = stats_(cv, ds, frm, to)
            if st_:
                print("%-44s %+8.1f%% %8.1f%% %+9.1f%% %7.2f" % (name, 100 * st_[0], 100 * st_[1], 100 * st_[2], st_[3]))
    print("\nPer anno (rendimento):")
    years = sorted({d[:4] for d in dates[rebal[0]:]})
    print("%-44s " % "" + " ".join("%6s" % y for y in years))
    for name, (cv, ds) in variants:
        row = []
        for y in years:
            idx = [j for j, d in enumerate(ds) if d[:4] == y]
            row.append("%+5.0f%%" % (100 * (cv[idx[-1]] / cv[max(0, idx[0] - 1)] - 1)) if idx else "   —  ")
        print("%-44s " % name[:44] + " ".join(row))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hold")
    h.add_argument("--split", default="2022-01-01")
    h.add_argument("--min-adv", type=float, default=2e7)
    n = sub.add_parser("news")
    n.add_argument("--symbols", type=int, default=80, help="most traded stocks to study")
    n.add_argument("--since", default="2022-01-01")
    m = sub.add_parser("momentum")
    m.add_argument("--min-dv", type=float, default=2e7, help="min 20-day average $ volume at each rebalance")
    m.add_argument("--blends", action="store_true", help="also S&P 500 + momentum mixes")
    a = ap.parse_args(argv)
    return {"hold": cmd_hold, "news": cmd_news, "momentum": cmd_momentum}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
