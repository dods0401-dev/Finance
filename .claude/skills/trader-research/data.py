#!/usr/bin/env python3
"""trader-research data layer: the full US listed universe with sectors, and price history.

Commands
  universe [--min-price 5] [--min-adv 5e6] [--max-new-sic 400]
        Every active, tradable Alpaca asset on NYSE/NASDAQ/AMEX/ARCA/BATS, tagged stock or
        ETF (Nasdaq Trader directories), with its SEC SIC code and sector (cached in
        research/sectors.json; at most --max-new-sic new SEC lookups per run), last price
        and 20-day average dollar volume. Keeps the liquid ones -> research/universe.csv.
  bars [--timeframe 1Day|5Min] [--since 2016-01-01] [--symbols A,B] [--limit N]
        Downloads Alpaca SIP bars (split/dividend adjusted) into research/cache/<tf>/SYM.csv.gz
        (git-ignored). Incremental: only what is missing after the last cached bar.

Read only towards the broker. Standard library only. Keys come from the network proxy.
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
RES = os.path.join(ROOT, "research")
CACHE = os.path.join(RES, "cache")
H = {"APCA-API-KEY-ID": "PKPLACEHOLDERPROXYINJECTS", "APCA-API-SECRET-KEY": "placeholder-proxy-injects"}
DATA = "https://data.alpaca.markets"
TRADE = "https://paper-api.alpaca.markets"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT") or "FinanceLab paper-trading research contact@example.org"}

# Sector ETFs (SPDR sectors + broad market + a few industry ETFs) used for the cluster view.
SECTOR_ETF = {"Tecnologia": "XLK", "Finanza": "XLF", "Energia": "XLE", "Salute": "XLV",
              "Consumi discrezionali": "XLY", "Consumi di base": "XLP", "Industria": "XLI",
              "Materiali": "XLB", "Utility": "XLU", "Immobiliare": "XLRE", "Comunicazioni": "XLC"}
MARKET_ETF = ["SPY", "QQQ", "IWM", "DIA", "SMH", "KRE", "XBI", "ITB", "XOP", "GDX", "TLT", "GLD"]


def sector(code):
    """SEC SIC code -> a GICS-like sector name (Italian)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "Altro"
    if 1300 <= c <= 1399 or 2900 <= c <= 2999 or c in (4610, 4612, 4613, 4922, 4923, 4924, 4925):
        return "Energia"
    if 2830 <= c <= 2836 or 3840 <= c <= 3851 or 8000 <= c <= 8099 or c in (5122, 8731):
        return "Salute"
    if 3570 <= c <= 3579 or 3600 <= c <= 3699 or 7370 <= c <= 7379 or 3820 <= c <= 3829 or c == 3861:
        return "Tecnologia"
    if 4800 <= c <= 4899 or 2710 <= c <= 2799 or 7810 <= c <= 7819:
        return "Comunicazioni"
    if 4900 <= c <= 4999:
        return "Utility"
    if c == 6798 or 6500 <= c <= 6599:
        return "Immobiliare"
    if 6000 <= c <= 6799:
        return "Finanza"
    if 5000 <= c <= 5999 or 7000 <= c <= 7299 or 3711 <= c <= 3716 or 2300 <= c <= 2399 or 3140 <= c <= 3149 or 7900 <= c <= 7999:
        return "Consumi discrezionali"
    if 2000 <= c <= 2199:
        return "Consumi di base"
    if 1000 <= c <= 1499 or 2600 <= c <= 2899 or 3300 <= c <= 3399:
        return "Materiali"
    if 1500 <= c <= 1799 or 3400 <= c <= 3569 or 3580 <= c <= 3599 or 3700 <= c <= 3799 or 4000 <= c <= 4799 or 7300 <= c <= 7369 or 8700 <= c <= 8799:
        return "Industria"
    return "Altro"


def get(url, headers=None, timeout=40, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def gj(url, headers=None, timeout=40):
    return json.loads(get(url, headers, timeout))


def bars_multi(symbols, timeframe, start, end=None):
    """Alpaca multi-symbol bars with pagination -> {sym: [bar, ...]}."""
    out, token = {}, None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "limit": 10000,
             "feed": "sip", "adjustment": "all"}
        if end:
            p["end"] = end
        if token:
            p["page_token"] = token
        d = gj(DATA + "/v2/stocks/bars?" + urllib.parse.urlencode(p), H, timeout=60)
        for s, bs in (d.get("bars") or {}).items():
            out.setdefault(s, []).extend(bs)
        token = d.get("next_page_token")
        if not token:
            return out


def etf_set():
    """Symbols flagged as ETFs in the Nasdaq Trader directories."""
    etfs = set()
    for url, col_sym, col_etf in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", "ETF"),
                                  ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", "ETF")):
        try:
            txt = get(url, {"User-Agent": "Mozilla/5.0"}).decode("utf-8", "replace")
        except Exception:
            continue
        rows = list(csv.DictReader(io.StringIO(txt), delimiter="|"))
        for r in rows:
            if (r.get(col_etf) or "").strip() == "Y":
                etfs.add((r.get(col_sym) or "").strip().replace(".", "-").replace("$", "-"))
    return etfs


def cmd_universe(a):
    os.makedirs(RES, exist_ok=True)
    assets = gj(TRADE + "/v2/assets?status=active&asset_class=us_equity", H, timeout=90)
    assets = [x for x in assets if x.get("tradable") and x.get("exchange") in EXCHANGES
              and "/" not in x["symbol"] and len(x["symbol"]) <= 6]
    print("listed tradable assets:", len(assets))
    etfs = etf_set()
    syms = [x["symbol"] for x in assets]
    start = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    stats = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        try:
            b = bars_multi(chunk, "1Day", start)
        except Exception as e:
            print("bars error", type(e).__name__)
            continue
        for s, bs in b.items():
            last = bs[-20:]
            if not last:
                continue
            stats[s] = {"price": last[-1]["c"], "adv": sum(x["c"] * x["v"] for x in last) / len(last)}
    print("with recent bars:", len(stats))
    keep = [x for x in assets if x["symbol"] in stats and stats[x["symbol"]]["price"] >= a.min_price
            and stats[x["symbol"]]["adv"] >= a.min_adv]
    # SEC SIC codes, cached
    spath = os.path.join(RES, "sectors.json")
    cache = json.load(open(spath)) if os.path.exists(spath) else {}
    try:
        tick = gj("https://www.sec.gov/files/company_tickers.json", SEC_UA)
        cik = {v["ticker"].upper(): int(v["cik_str"]) for v in tick.values()}
    except Exception:
        cik = {}
    new = 0
    for x in sorted(keep, key=lambda x: -stats[x["symbol"]]["adv"]):
        s = x["symbol"]
        if s in cache or s.replace(".", "-") in etfs or new >= a.max_new_sic:
            continue
        c = cik.get(s.replace(".", "-"))
        if not c:
            cache[s] = {"sic": None, "sic_desc": None}
            continue
        try:
            d = gj("https://data.sec.gov/submissions/CIK%010d.json" % c, SEC_UA, timeout=20)
            cache[s] = {"sic": d.get("sic"), "sic_desc": d.get("sicDescription")}
            new += 1
        except Exception:
            pass
        time.sleep(0.12)
    json.dump(cache, open(spath, "w"), indent=0, sort_keys=True)
    rows = []
    for x in keep:
        s = x["symbol"]
        is_etf = s.replace(".", "-") in etfs
        sc = cache.get(s) or {}
        rows.append({"symbol": s, "name": (x.get("name") or "")[:60], "exchange": x["exchange"],
                     "type": "etf" if is_etf else "stock", "sic": sc.get("sic") or "",
                     "sector": "ETF" if is_etf else sector(sc.get("sic")),
                     "price": round(stats[s]["price"], 2), "adv_usd": int(stats[s]["adv"]),
                     "shortable": int(bool(x.get("shortable")))})
    rows.sort(key=lambda r: -r["adv_usd"])
    with open(os.path.join(RES, "universe.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[r["sector"]] = by.get(r["sector"], 0) + 1
    missing = sum(1 for r in rows if r["type"] == "stock" and not r["sic"])
    print("universe:", len(rows), "| by sector:", json.dumps(by, ensure_ascii=False), "| stocks without SIC yet:", missing)
    return 0


def load_universe():
    p = os.path.join(RES, "universe.csv")
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def cache_path(tf, s):
    return os.path.join(CACHE, tf, s.replace("/", "_") + ".csv.gz")


def read_bars(tf, s):
    p = cache_path(tf, s)
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt") as f:
        return [{"t": r["t"], "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                 "c": float(r["c"]), "v": float(r["v"])} for r in csv.DictReader(f)]


def write_bars(tf, s, bars):
    p = cache_path(tf, s)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with gzip.open(p, "wt") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for b in bars:
            w.writerow([b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]])


def cmd_bars(a):
    syms = a.symbols.split(",") if a.symbols else [r["symbol"] for r in load_universe()]
    syms = syms + [s for s in list(SECTOR_ETF.values()) + MARKET_ETF if s not in syms]
    if a.limit:
        syms = syms[:a.limit]
    have = {s: read_bars(a.timeframe, s) for s in syms}
    groups = {}
    for s in syms:
        last = have[s][-1]["t"] if have[s] else None
        start = (last[:10] if last else a.since)
        groups.setdefault(start, []).append(s)
    n = 0
    batch = 100 if a.timeframe == "1Day" else 10
    for start, ss in groups.items():
        for i in range(0, len(ss), batch):
            chunk = ss[i:i + batch]
            try:
                got = bars_multi(chunk, a.timeframe, start)
            except Exception as e:
                print("error", type(e).__name__, chunk[:3])
                continue
            for s in chunk:
                new = [b for b in got.get(s, []) if not have[s] or b["t"] > have[s][-1]["t"]]
                if new:
                    write_bars(a.timeframe, s, have[s] + new)
                    n += 1
            print("…", min(i + batch, len(ss)), "/", len(ss), "since", start, flush=True)
    print("updated", n, "symbols in", os.path.join(CACHE, a.timeframe))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("universe")
    u.add_argument("--min-price", type=float, default=5)
    u.add_argument("--min-adv", type=float, default=5e6)
    u.add_argument("--max-new-sic", type=int, default=400)
    b = sub.add_parser("bars")
    b.add_argument("--timeframe", default="1Day", choices=["1Day", "5Min"])
    b.add_argument("--since", default="2016-01-01")
    b.add_argument("--symbols")
    b.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    return {"universe": cmd_universe, "bars": cmd_bars}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
