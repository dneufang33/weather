"""Probe Polymarket APIs and save raw samples for schema inspection."""
import json, os, urllib.request, urllib.parse, traceback

OUT = "data/probe"
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "weather-research/0.1"}


def get(url, name):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            hdr = dict(r.headers)
        with open(f"{OUT}/{name}", "wb") as f:
            f.write(body)
        with open(f"{OUT}/{name}.headers.json", "w") as f:
            json.dump(hdr, f, indent=1)
        print("OK", len(body), url)
        try:
            return json.loads(body)
        except Exception:
            return None
    except Exception as e:
        print("ERR", url, e)
        with open(f"{OUT}/{name}.error.txt", "w") as f:
            f.write(traceback.format_exc())
        return None


get("https://data-api.polymarket.com/v2/openapi.json", "data_openapi.json")
get("https://data-api.polymarket.com/trades?limit=3", "v1_trades.json")
tags = get("https://gamma-api.polymarket.com/tags?limit=1000", "gamma_tags.json")
ev = get("https://gamma-api.polymarket.com/events?tag_slug=weather&closed=true&limit=50&order=endDate&ascending=false", "gamma_weather_events_closed.json")
get("https://gamma-api.polymarket.com/events?tag_slug=highest-temperature&closed=true&limit=50&order=endDate&ascending=false", "gamma_hightemp_events_closed.json")
get("https://gamma-api.polymarket.com/events?tag_slug=weather&closed=false&limit=20", "gamma_weather_events_open.json")

if isinstance(ev, list) and ev:
    e = ev[min(5, len(ev) - 1)]
    mk = e.get("markets") or []
    eid = e.get("id")
    get(f"https://data-api.polymarket.com/v2/trades?event_id={eid}&limit=500", "v2_trades_event.json")
    if mk:
        cid = mk[0].get("conditionId")
        get(f"https://data-api.polymarket.com/v2/trades?condition={cid}&limit=500", "v2_trades_condition.json")
        get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500", "v1_trades_market.json")
        get(f"https://data-api.polymarket.com/v2/activity?condition={cid}&limit=100", "v2_activity_condition.json")
print("done")
