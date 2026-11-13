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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hold")
    h.add_argument("--split", default="2022-01-01")
    h.add_argument("--min-adv", type=float, default=2e7)
    a = ap.parse_args(argv)
    return {"hold": cmd_hold}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
