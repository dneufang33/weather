import gzip, json, urllib.request, time
def t(url, data=None, show=500):
    try:
        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept-Encoding": "gzip", "Accept": "*/*"}
        if data is not None: h["Content-Type"] = "application/json"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h, data=json.dumps(data).encode() if data is not None else None), timeout=30) as r:
            b = r.read()
            if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
            print("OK ", url[:90], json.dumps(data)[:60] if data else "", "|", b[:show].decode("utf8", "replace").replace("\n", " "))
            return b
    except Exception as e:
        print("ERR", url[:90], "|", repr(e)[:160])
print("now", int(time.time()))
for s in ("XTI/USD", "WTI/USD", "USOIL/USD", "CL/USD", "XBR/USD"):
    t(f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{s}", show=260)
b = t("https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d", show=700)
if b:
    r = json.loads(b)["chart"]["result"][0]; print("yahoo last ts", r["timestamp"][-3:], r["indicators"]["quote"][0]["close"][-3:], r["meta"].get("regularMarketTime"))
t("https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1h&range=1mo", show=120)
for dex in ("", "xyz", "flx", "vntl", "hyna", "km"):
    b = t("https://api.hyperliquid.xyz/info", {"type": "allMids", "dex": dex}, show=60)
    if b:
        d = json.loads(b); print(dex, len(d), {k: v for k, v in d.items() if any(x in k.upper() for x in ("CL", "OIL", "WTI", "BRENT", "GOLD", "XAU", "SILVER", "XAG"))})
t("https://api.hyperliquid.xyz/info", {"type": "perpDexs"}, show=900)
for s in ("WTI", "CL", "BRENT"): t(f"https://api.gold-api.com/price/{s}", show=200)
for ser in ("crude-oil-cl-hit", "wti-crude-oil-hit-price-weekly"):
    b = t(f"https://gamma-api.polymarket.com/events?series_slug={ser}&closed=false&limit=20", show=10)
    if b:
        for e in json.loads(b):
            print(ser, e["slug"], e["endDate"], len(e["markets"]), [m["question"] for m in e["markets"][:3]]); print(e["markets"][0]["description"][:1600])
