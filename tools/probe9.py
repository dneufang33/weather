import gzip, json, urllib.request, re
def http(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-research/0.1", "Accept-Encoding": "gzip"}), timeout=40) as r:
        b = r.read()
        if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
        return json.loads(b)
out = []
for d in (12, 13, 14):
    for a, sym in (("bitcoin", "BTCUSDT"), ("ethereum", "ETHUSDT")):
        try:
            e = http(f"https://gamma-api.polymarket.com/events?slug={a}-above-on-november-{d}-2026")[0]
            for m in e["markets"]:
                k = float(re.search(r"above \$([\d,]+)", m["question"]).group(1).replace(",", ""))
                out.append({"asset": a, "day": d, "K": k, "closed": m.get("closed"), "op": m.get("outcomePrices"), "cid": m["conditionId"], "vol": m.get("volumeNum")})
        except Exception as ex:
            print("ERR", a, d, repr(ex)[:100])
json.dump(out, open("data/gamma_check.json", "w"))
print(len(out))
