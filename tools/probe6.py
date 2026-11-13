import gzip, json, urllib.request
def t(url, ua="Mozilla/5.0 (X11; Linux x86_64)"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip", "Accept": "*/*"}), timeout=30) as r:
            b = r.read()
            if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
            print("OK ", url[:95], "|", b[:420].decode("utf8", "replace").replace("\n", " "))
    except Exception as e:
        print("ERR", url[:95], "|", repr(e)[:160])
X = "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
t(f"https://hermes.pyth.network/api/latest_price_feeds?ids[]={X}")
t(f"https://hermes.pyth.network/v2/updates/price/latest?ids%5B%5D=0x{X}")
t(f"https://hermes-beta.pyth.network/v2/updates/price/latest?ids[]={X}&parsed=true")
for s in ("XAU", "XAG"):
    t(f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{s}/USD")
    t(f"https://api.gold-api.com/price/{s}")
    t(f"https://stooq.com/q/l/?s={s.lower()}usd&f=sd2t2ohlcv&h&e=csv")
    t(f"https://query1.finance.yahoo.com/v8/finance/chart/{s}USD=X?interval=1m&range=1h")
t("https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=1mo")
t("https://query1.finance.yahoo.com/v8/finance/chart/SI=F?interval=1h&range=1mo")
t("https://stooq.com/q/d/l/?s=xagusd&i=d")
