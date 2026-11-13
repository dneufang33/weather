"""Survey Polymarket price-level markets (multi-strike / hit-price / up-down) across assets."""
import gzip, json, os, urllib.request, collections, time
OUT = "data/probe4"; os.makedirs(OUT, exist_ok=True)
def http(url, data=None):
    h = {"User-Agent": "paper-research/0.1", "Accept-Encoding": "gzip"}
    if data is not None: h["Content-Type"] = "application/json"
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h, data=json.dumps(data).encode() if data is not None else None), timeout=60) as r:
                b = r.read()
                if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
                return json.loads(b)
        except Exception as e:
            err = e; time.sleep(1)
    print("ERR", url[:120], repr(err)[:150]); return None
G = "https://gamma-api.polymarket.com/"
events = {}
for tag in ["multi-strikes", "gold", "silver", "commodities", "oil", "stocks", "indices", "finance", "forex", "ethereum", "solana", "xrp", "nasdaq", "sp500", "equities", "earnings", "economy", "fed-rates", "crypto-prices", "hit-price"]:
    for off in range(0, 600, 100):
        d = http(G + f"events?tag_slug={tag}&closed=false&limit=100&offset={off}")
        if not d: break
        for e in d:
            e.setdefault("_tags", set()).add(tag)
            if e["id"] in events: events[e["id"]]["_tags"].add(tag)
            else: events[e["id"]] = e
        if len(d) < 100: break
    print(tag, "total events so far", len(events))
rows = []
for e in events.values():
    mk = e.get("markets") or []
    rows.append({"slug": e.get("slug"), "title": e.get("title"), "series": e.get("seriesSlug"), "end": e.get("endDate"),
                 "vol24": float(e.get("volume24hr") or 0), "vol": float(e.get("volume") or 0), "liq": float(e.get("liquidity") or 0),
                 "n_mk": len(mk), "tags": sorted(e["_tags"]), "all_tags": [t.get("slug") for t in e.get("tags") or []],
                 "res": (mk[0].get("resolutionSource") if mk else None), "desc": (mk[0].get("description") or "")[:600] if mk else "",
                 "q0": mk[0].get("question") if mk else None, "id": e["id"]})
json.dump(rows, open(f"{OUT}/events.json", "w"))
ser = collections.defaultdict(lambda: [0, 0.0, 0.0])
for r in rows:
    s = ser[r["series"] or r["slug"]]; s[0] += 1; s[1] += r["vol24"]; s[2] += r["liq"]
top = sorted(ser.items(), key=lambda x: -x[1][1])[:80]
for k, v in top: print(f"{k:55s} events={v[0]:3d} vol24=${v[1]:>12,.0f} liq=${v[2]:>12,.0f}")
# book spreads for the top events of interesting series (skip plain crypto up/down)
books = []
cand = sorted([e for e in events.values() if (e.get("volume24hr") or 0) and len(e.get("markets") or []) >= 3], key=lambda e: -float(e.get("volume24hr") or 0))[:120]
for e in cand:
    toks, meta = [], {}
    for m in e["markets"]:
        try:
            if m.get("closed"): continue
            t = json.loads(m["clobTokenIds"])[0]; toks.append({"token_id": t}); meta[t] = m
        except Exception: pass
    bk = http("https://clob.polymarket.com/books", data=toks[:60]) or []
    for x in bk:
        bids = [(float(y["price"]), float(y["size"])) for y in x.get("bids") or []]; asks = [(float(y["price"]), float(y["size"])) for y in x.get("asks") or []]
        if not bids or not asks: continue
        bb, ba = max(bids), min(asks); m = meta.get(x.get("asset_id"), {})
        books.append({"slug": e["slug"], "series": e.get("seriesSlug"), "q": m.get("question"), "bb": bb[0], "ba": ba[0], "bbsz": bb[1], "basz": ba[1],
                      "vol24": float(m.get("volume24hr") or 0), "tick": m.get("orderPriceMinTickSize"), "end": m.get("endDate"), "fees": m.get("feesEnabled"),
                      "rew_spread": m.get("rewardsMaxSpread"), "rew_min": m.get("rewardsMinSize")})
json.dump(books, open(f"{OUT}/books.json", "w"))
print("books", len(books))
