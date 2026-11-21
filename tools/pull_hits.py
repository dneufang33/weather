"""Pull resolved Polymarket 'hit price' markets for oil, gold, silver with hourly price history,
plus hourly underlying futures and CBOE implied-vol indices from Yahoo.

Writes data/hits/markets.json, data/hits/prices.csv.gz, data/hits/yahoo.json
"""
import csv, gzip, json, os, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

OUT = "data/hits"
os.makedirs(OUT, exist_ok=True)
SERIES = {"WTI": ["crude-oil-cl-hit", "wti-crude-oil-hit-price-weekly"],
          "XAU": ["xauusd-hit-month", "gold-hit-price-weekly"],
          "XAG": ["xagusd-hit-month", "silver-hit-price-weekly"]}
YAHOO = ["CL=F", "GC=F", "SI=F", "^OVX", "^GVZ", "^VXSLV"]


def http(url, ua="Mozilla/5.0 (X11; Linux x86_64)", tries=4):
    for i in range(tries):
        try:
            h = {"User-Agent": ua, "Accept-Encoding": "gzip"}
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                b = r.read()
                if b[:2] == b"\x1f\x8b":
                    b = gzip.decompress(b)
                return json.loads(b)
        except Exception as e:
            err = e
            time.sleep(1 + 2 * i)
    print("ERR", url[:120], repr(err)[:150])
    return None


# ---- yahoo ----
yh = {}
for s in YAHOO:
    r = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?interval=1h&range=730d")
    try:
        res = r["chart"]["result"][0]
        yh[s] = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
        print(s, len(yh[s]))
    except Exception as e:
        print("yahoo fail", s, e)
json.dump(yh, open(f"{OUT}/yahoo.json", "w"))

# ---- markets ----
G = "https://gamma-api.polymarket.com/"
markets = []
for asset, sers in SERIES.items():
    for ser in sers:
        for off in range(0, 2000, 100):
            d = http(G + f"events?series_slug={ser}&closed=true&limit=100&offset={off}") or []
            for e in d:
                for m in e.get("markets") or []:
                    try:
                        g = re.search(r"\((HIGH|LOW)\) \$([\d,\.]+)", m["question"])
                        op = json.loads(m.get("outcomePrices") or "[]")
                        oc = json.loads(m["outcomes"])
                        if not g or len(op) != 2 or sorted(op) != ["0", "1"]:
                            continue
                        yi = oc.index("Yes")
                        markets.append({"asset": asset, "series": ser, "weekly": "weekly" in ser, "event": str(e["id"]),
                                        "slug": e["slug"], "cid": m["conditionId"], "dirn": g.group(1),
                                        "K": float(g.group(2).replace(",", "")), "yes_won": float(op[yi]),
                                        "yes_tok": json.loads(m["clobTokenIds"])[yi],
                                        "start": e.get("startDate"), "end": e.get("endDate"),
                                        "closed_time": m.get("closedTime"), "volume": m.get("volumeNum")})
                    except Exception:
                        continue
            if len(d) < 100:
                break
        print(asset, ser, sum(1 for m in markets if m["series"] == ser))
json.dump(markets, open(f"{OUT}/markets.json", "w"))


def iso(s):
    from datetime import datetime
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def hist(m):
    try:
        t0, t1 = iso(m["start"]), iso(m["end"]) + 86400
    except Exception:
        return m["cid"], []
    out = []
    for a in range(t0, t1, 14 * 86400):          # windows of 14 days
        d = http(f"https://clob.polymarket.com/prices-history?market={m['yes_tok']}&startTs={a}&endTs={min(a + 14 * 86400, t1)}&fidelity=60")
        out += (d or {}).get("history") or []
    return m["cid"], out


rows, empty = 0, 0
with gzip.open(f"{OUT}/prices.csv.gz", "wt", newline="") as fh, ThreadPoolExecutor(8) as ex:
    w = csv.writer(fh)
    w.writerow(["cid", "t", "p"])
    for cid, h in ex.map(hist, markets):
        if not h:
            empty += 1
        seen = set()
        for x in h:
            if x["t"] in seen:
                continue
            seen.add(x["t"])
            w.writerow([cid, x["t"], x["p"]])
            rows += 1
print("markets", len(markets), "price rows", rows, "markets without history", empty)
