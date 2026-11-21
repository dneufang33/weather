"""Fetch official Gamma resolution for every market any paper bot touched."""
import gzip, json, urllib.request, glob
def http(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-research/0.1", "Accept-Encoding": "gzip"}), timeout=60) as r:
        b = r.read()
        if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
        return json.loads(b)
cids = set()
for f in ["paper/state.json"] + glob.glob("paper2/*/state.json"):
    st = json.load(open(f))
    cids |= set(st.get("mk", {}).keys())
    for s in st["strats"].values(): cids |= set(s["inv"].keys())
cids = sorted(cids); out = {}
for i in range(0, len(cids), 20):
    q = "&".join("condition_ids=" + c for c in cids[i:i + 20])
    try:
        for m in http("https://gamma-api.polymarket.com/markets?limit=50&" + q):
            out[m["conditionId"]] = {"closed": m.get("closed"), "op": m.get("outcomePrices"), "outcomes": m.get("outcomes"), "q": m.get("question"), "end": m.get("endDate"), "umaStatus": m.get("umaResolutionStatus")}
    except Exception as e:
        print("ERR", i, repr(e)[:200])
json.dump(out, open("data/resolution.json", "w"))
print(len(cids), "cids", len(out), "found", sum(1 for v in out.values() if v["closed"]), "closed")
