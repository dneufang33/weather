import gzip, json, urllib.request, urllib.parse, time
def http(url):
    h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept-Encoding": "gzip"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
        b = r.read()
        if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
        return json.loads(b)
def tryp(label, f):
    try: print("OK ", label, f())
    except Exception as e: print("ERR", label, repr(e)[:200])
for s in ("^OVX", "^GVZ", "^VXSLV", "CL=F", "GC=F", "SI=F"):
    def f(s=s):
        r = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?interval=1d&range=2y")["chart"]["result"][0]
        c = r["indicators"]["quote"][0]["close"]; t = r["timestamp"]
        r2 = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?interval=1h&range=730d")["chart"]["result"][0]
        return (len(t), time.strftime("%Y-%m-%d", time.gmtime(t[0])), c[-3:], "hourly", len(r2["timestamp"]), time.strftime("%Y-%m-%d", time.gmtime(r2["timestamp"][0])))
    tryp(s, f)
G = "https://gamma-api.polymarket.com/"
tok = None
for ser in ("crude-oil-cl-hit", "wti-crude-oil-hit-price-weekly", "xauusd-hit-month", "gold-hit-price-weekly", "xagusd-hit-month", "silver-hit-price-weekly"):
    def f(ser=ser):
        global tok
        ev = []
        for off in range(0, 1000, 100):
            d = http(G + f"events?series_slug={ser}&closed=true&limit=100&offset={off}")
            ev += d
            if len(d) < 100: break
        ends = sorted(e["endDate"][:10] for e in ev)
        if ev and tok is None:
            tok = json.loads(ev[0]["markets"][0]["clobTokenIds"])[0]
        return (len(ev), ends[:1], ends[-1:], sum(len(e["markets"]) for e in ev))
    tryp(ser, f)
def f():
    d = http(f"https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity=60")
    h = d["history"]; return (len(h), h[:2], h[-1:])
tryp("prices-history", f)
