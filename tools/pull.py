"""Pull resolved daily highest-temperature events and all their trades.

Usage: python tools/pull.py DAYS [MAX_MINUTES]
Writes:
  data/events.csv            one row per event
  data/markets.csv           one row per bracket market
  data/trades/<date>.csv.gz  one row per fill side (maker and taker rows)
"""
import csv, gzip, json, os, sys, time, threading, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
MAX_MIN = float(sys.argv[2]) if len(sys.argv) > 2 else 300
T0 = time.time()
UA = {"User-Agent": "weather-research/0.1", "Accept-Encoding": "identity"}
os.makedirs("data/trades", exist_ok=True)
lock = threading.Lock()
stats = {"req": 0, "retry": 0}


def get(url, tries=8):
    for i in range(tries):
        try:
            with lock:
                stats["req"] += 1
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                ra = e.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "").isdigit() else 2 ** i
                with lock:
                    stats["retry"] += 1
                time.sleep(min(wait, 60))
                continue
            raise
        except Exception:
            with lock:
                stats["retry"] += 1
            time.sleep(min(2 ** i, 60))
    raise RuntimeError("failed " + url)


# ---------- 1. events ----------
cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).date().isoformat()
events, seen = [], set()
day = datetime.now(timezone.utc).date()
for k in range(DAYS + 2):
    d0 = day - timedelta(days=k)
    offset = 0
    while True:
        q = urllib.parse.urlencode({"tag_slug": "highest-temperature", "closed": "true", "limit": 100,
                                    "offset": offset,
                                    "end_date_min": f"{d0.isoformat()}T00:00:00Z",
                                    "end_date_max": f"{d0.isoformat()}T23:59:59Z"})
        try:
            page = get("https://gamma-api.polymarket.com/events?" + q)
        except Exception as err:
            print("gamma fail", d0, offset, err)
            break
        if not page:
            break
        for e in page:
            ed = e.get("eventDate") or (e.get("endDate") or "")[:10]
            if ed < cutoff or e["id"] in seen:
                continue
            seen.add(e["id"])
            events.append(e)
        offset += len(page)
        if len(page) < 100:
            break
    if k % 10 == 0:
        print("gamma day", d0, "events so far", len(events))
print(f"events since {cutoff}: {len(events)}  ({time.time()-T0:.0f}s)")

ev_rows, mk_rows, cond_map = [], [], {}
for e in events:
    mks = e.get("markets") or []
    ok = True
    for m in mks:
        try:
            op = json.loads(m.get("outcomePrices") or "[]")
        except Exception:
            op = []
        if len(op) != 2 or sorted(op) != ["0", "1"]:
            ok = False
        yes_won = 1 if op and op[0] == "1" else 0
        mk_rows.append({"event_id": e["id"], "condition_id": m.get("conditionId"), "bracket": m.get("groupItemTitle"),
                        "yes_won": yes_won, "volume": m.get("volumeNum") or m.get("volume"),
                        "fees_enabled": m.get("feesEnabled"), "taker_fee": m.get("takerBaseFee"),
                        "maker_fee": m.get("makerBaseFee")})
    ev_rows.append({"event_id": e["id"], "series": e.get("seriesSlug"), "title": e.get("title"),
                    "event_date": e.get("eventDate"), "start": e.get("startDate"), "end": e.get("endDate"),
                    "closed_time": e.get("closedTime"), "volume": e.get("volume"), "n_markets": len(mks),
                    "clean_resolution": int(ok), "resolution_source": e.get("resolutionSource")})


def wcsv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


wcsv("data/events.csv", ev_rows)
wcsv("data/markets.csv", mk_rows)


# ---------- 2. trades ----------
def walk(eid, taker_only):
    rows, cursor = [], None
    while True:
        if cursor:
            url = f"https://data-api.polymarket.com/v2/trades?event_id={eid}&cursor={urllib.parse.quote(cursor)}&taker_only={taker_only}&filter_amount=0"
        else:
            url = f"https://data-api.polymarket.com/v2/trades?event_id={eid}&limit=1000&taker_only={taker_only}&filter_amount=0"
        p = get(url)
        rows.extend(p.get("data") or [])
        cursor = (p.get("pagination") or {}).get("next_cursor")
        if not cursor:
            return rows


def do_event(e):
    eid = e["id"]
    allr = walk(eid, "false")
    tak = walk(eid, "true")
    tkeys = {}
    for r in tak:
        k = (r["transaction_hash"], r["proxy_wallet"], r["token_id"], r["side"])
        tkeys[k] = tkeys.get(k, 0) + 1
    out = []
    for r in allr:
        k = (r["transaction_hash"], r["proxy_wallet"], r["token_id"], r["side"])
        is_t = 0
        if tkeys.get(k, 0) > 0:
            is_t = 1
            tkeys[k] -= 1
        out.append((r["proxy_wallet"][:14], eid, r["condition_id"][:14], r["outcome_index"],
                    1 if r["side"] == "BUY" else -1, r["size"], r["price"], r["timestamp"], is_t,
                    r["transaction_hash"][:12]))
    return e.get("eventDate"), out, len(allr), len(tak)


by_date = {}
done = 0
n_rows = 0
order = sorted(events, key=lambda e: e.get("eventDate") or "", reverse=True)
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(do_event, e): e for e in order}
    for f in as_completed(futs):
        try:
            d, out, na, nt = f.result()
        except Exception as err:
            print("event failed", futs[f]["id"], err)
            continue
        by_date.setdefault(d, []).extend(out)
        done += 1
        n_rows += len(out)
        if done % 20 == 0:
            el = (time.time() - T0) / 60
            print(f"{done}/{len(events)} events, {n_rows} rows, {stats['req']} req, {stats['retry']} retries, {el:.1f} min")
        if (time.time() - T0) / 60 > MAX_MIN:
            print("time budget hit, stopping")
            for g in futs:
                g.cancel()
            break

for d, rows in by_date.items():
    with gzip.open(f"data/trades/{d}.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet", "event_id", "condition_id", "outcome_index", "side", "size", "price", "ts", "taker", "tx"])
        w.writerows(rows)
print(f"DONE events={done} rows={n_rows} req={stats['req']} retries={stats['retry']} minutes={(time.time()-T0)/60:.1f}")
