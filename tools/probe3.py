import json, urllib.request, urllib.error
def t(url, hdr):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=40) as r:
            b = r.read(); d = json.loads(b)
            print("OK", len(b), len(d) if isinstance(d, list) else type(d), [e.get("slug") for e in d][:4] if isinstance(d, list) else "", "|", url[34:], hdr)
    except urllib.error.HTTPError as e:
        print("ERR", e.code, e.read()[:200], "|", url[34:], hdr)
    except Exception as e:
        print("ERR", repr(e)[:100], "|", url[34:], hdr)
G = "https://gamma-api.polymarket.com/"
H1 = {"User-Agent": "paper-research/0.1"}
H2 = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
for h in (H1, H2):
    t(G + "events?tag_slug=bitcoin&closed=false&limit=100&order=volume24hr&ascending=false", h)
    t(G + "events?tag_slug=bitcoin&closed=false&limit=100", h)
    t(G + "events?slug=bitcoin-above-on-november-13-2026", h)
    t(G + "events?slug=bitcoin-above-on-november-14-2026&slug=bitcoin-above-on-november-15-2026", h)
    t(G + "events?series_slug=btc-multi-strikes-weekly&closed=false&limit=20", h)
    t(G + "events?tag_slug=multi-strikes&closed=false&limit=50", h)
