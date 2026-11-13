import gzip, json, urllib.request, time
def http(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-research/0.1", "Accept-Encoding": "gzip"}), timeout=40) as r:
            b = r.read()
            if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
            return json.loads(b)
    except Exception as e:
        print("ERR", url[:110], repr(e)[:200]); return None
for q in ("XAU", "XAG"):
    d = http(f"https://hermes.pyth.network/v2/price_feeds?query={q}&asset_type=metal")
    print(q, json.dumps(d)[:900])
    if d:
        fid = [x for x in d if x["attributes"].get("base") == q and x["attributes"].get("quote_currency") == "USD"]
        print("match", [(x["id"], x["attributes"].get("symbol")) for x in fid])
        if fid:
            p = http(f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={fid[0]['id']}&parsed=true")
            print(json.dumps((p or {}).get("parsed"))[:500])
now = int(time.time())
for sym in ("Metal.XAU/USD", "Metal.XAG/USD"):
    h = http(f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol={sym}&resolution=60&from={now-30*86400}&to={now}")
    if h: print(sym, h.get("s"), len(h.get("t", [])), h.get("t", [])[:2], h.get("t", [])[-2:], h.get("c", [])[-3:])
    h = http(f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol={sym}&resolution=1&from={now-3600}&to={now}")
    if h: print(sym, "1m", h.get("s"), len(h.get("t", [])), h.get("c", [])[-2:])
d = http("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=ETH&kind=option"); print("eth opts", len((d or {}).get("result", [])), (d or {}).get("result", [{}])[0].get("instrument_name"))
print(json.dumps(http("https://www.deribit.com/api/v2/public/ticker?instrument_name=ETH-PERPETUAL"))[:200])
print(http("https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDT"), http("https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT"))
d = http("https://www.deribit.com/api/v2/public/get_instruments?currency=USDC&kind=option&expired=false"); import collections
print("usdc opts", collections.Counter(x["instrument_name"].split("-")[0] for x in (d or {}).get("result", [])))
e = http("https://gamma-api.polymarket.com/events?series_slug=ethereum-multi-strikes-weekly&closed=false&limit=20"); print([(x["slug"], x["endDate"]) for x in e or []]); print((e or [{}])[0].get("markets", [{}])[0].get("description", "")[:500])
for s in ("xauusd-hit-month", "gold-hit-price-weekly", "xagusd-hit-month", "silver-hit-price-weekly"):
    e = http(f"https://gamma-api.polymarket.com/events?series_slug={s}&closed=false&limit=20"); print(s, [(x["slug"], x["endDate"], len(x["markets"]), sum(1 for m in x["markets"] if m.get("closed"))) for x in e or []])
    if e:
        m = e[0]["markets"]; print([ (y["question"], y.get("closed"), y.get("outcomePrices")) for y in m[:4]]); print(m[0].get("description", "")[:1500])
