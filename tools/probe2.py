"""Probe endpoints needed by the paper-trading bot."""
import json, os, urllib.request, urllib.parse, traceback, datetime
OUT = "data/probe2"; os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "weather-research/0.1"}
def get(url, name, data=None):
    try:
        h = dict(UA)
        if data is not None:
            h["Content-Type"] = "application/json"; data = json.dumps(data).encode()
        with urllib.request.urlopen(urllib.request.Request(url, headers=h, data=data), timeout=60) as r:
            body = r.read()
        open(f"{OUT}/{name}", "wb").write(body); print("OK", len(body), url)
        try: return json.loads(body)
        except Exception: return None
    except Exception as e:
        print("ERR", url, e); open(f"{OUT}/{name}.err", "w").write(traceback.format_exc()); return None

now = datetime.datetime.now(datetime.timezone.utc)
found = []
for k in range(0, 3):
    d = now + datetime.timedelta(days=k)
    mon = d.strftime("%B").lower()
    for pat in ("bitcoin-above-on-{m}-{d}", "bitcoin-price-on-{m}-{d}", "ethereum-above-on-{m}-{d}"):
        slug = pat.format(m=mon, d=d.day)
        ev = get("https://gamma-api.polymarket.com/events?slug=" + slug, f"ev_{slug}.json")
        if ev: found.append(ev[0])
get("https://gamma-api.polymarket.com/events?tag_slug=bitcoin&closed=false&limit=100&order=volume24hr&ascending=false", "ev_tag_bitcoin.json")
get("https://gamma-api.polymarket.com/events?tag_slug=gold&closed=false&limit=50", "ev_tag_gold.json")
if found:
    e = found[0]; m = e["markets"][len(e["markets"]) // 2]
    toks = json.loads(m["clobTokenIds"])
    get("https://clob.polymarket.com/book?token_id=" + toks[0], "clob_book.json")
    get("https://clob.polymarket.com/books", "clob_books.json", data=[{"token_id": t} for t in toks])
    get(f"https://data-api.polymarket.com/v2/trades?event_id={e['id']}&limit=50", "trades.json")
get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option", "deribit_opts.json")
get("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL", "deribit_perp.json")
get("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd", "deribit_index.json")
get("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", "binance.json")
print("done")
