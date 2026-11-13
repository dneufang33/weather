"""Paper-trading bot (multi-asset variant): ETH "above" markets, gold/silver "hit" markets.
Usage: python tools/paper_bot2.py ASSET MINUTES

Fair value comes from Deribit option implied vols (digital = call-spread limit), spot from
Binance (the resolution source). Quotes are simulated only; fills are inferred from real
Polymarket taker prints that trade THROUGH our simulated quote. Inventory is delta-hedged
with a simulated Deribit perpetual. Nothing is ever sent to any exchange.

Usage: python tools/paper_bot.py MINUTES
"""
import bisect, csv, datetime as dt, gzip, json, math, os, re, subprocess, sys, time, traceback, urllib.request

ASSET = sys.argv[1]
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 5
CFG = {
    "ETH": dict(kind="above", series=["ethereum-multi-strikes-weekly"], binance="ETHUSDT", deribit="ETH",
                perp="ETH-PERPETUAL", band=0.2),
    "XAU": dict(kind="hit", series=["xauusd-hit-month", "gold-hit-price-weekly"], vol="PAXGUSDT", band=0.1,
                pyth="765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"),
    "XAG": dict(kind="hit", series=["xagusd-hit-month", "silver-hit-price-weekly"], vol="fit", band=8.0,
                pyth="f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e"),
}[ASSET]
KIND = CFG["kind"]
BASE, LOG = f"paper2/{ASSET}", f"paper2/{ASSET}/log"
os.makedirs(LOG, exist_ok=True)
STRATS = {"hs1": 0.01, "hs2": 0.02, "hs4": 0.04, "hs2b": 0.02, "hs1q": 0.01, "hs2q": 0.02, "hs2bq": 0.02,
          "hs03q": 0.003, "hs03bq": 0.003}   # half-spread in price units
BLEND = {"hs2b": 0.5, "hs2bq": 0.5, "hs03bq": 0.5}
QUEUE = {"hs1q", "hs2q", "hs2bq", "hs03q", "hs03bq"}   # fills also at our price once the displayed queue ahead of us has traded
#                               # weight of Polymarket mid in the quote centre
SIZE, MAXINV = 200, 1000                            # shares per quote, max |inventory| per market
TICK_S, FLUSH_S, SNAP_S, EQ_S, DISC_S = 20, 900, 60, 300, 600
FEE_PM, REBATE, PERP_FEE = 0.07, 0.20, 0.0005
STOP_QUOTE_S, MAX_AHEAD_S = 3600, (72 * 3600 if KIND == "above" else 60 * 86400)
HEDGE_BAND = CFG["band"]                             # units of the underlying (~$400)
DEADLINE = dt.datetime(2026, 11, 20, 18, 0, tzinfo=dt.timezone.utc).timestamp()
YEAR = 365.0 * 86400
UA = {"User-Agent": "paper-research/0.1"}
T0 = time.time()
RUN = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
logbuf, rows = [], {"fills": [], "hedges": [], "snap": [], "equity": [], "settle": [], "ov": []}
HDR = {
    "fills": ["ts", "trade_ts", "strat", "cid", "strike", "end", "side", "px", "size", "fair_quote", "fair_now",
              "spot", "trade_px_yes", "inv_after"],
    "hedges": ["ts", "strat", "dq", "px", "fee", "pos_after"],
    "snap": ["ts", "cid", "strike", "end", "spot", "perp", "fair", "best_bid", "best_ask", "bid_sz", "ask_sz", "dirn", "vol_h"],
    "equity": ["ts", "strat", "pm_cash", "inv_value", "perp_cash", "perp_pos", "perp_mark", "fees_perp",
               "rebate_est", "n_fills", "gross_inv_shares", "equity"],
    "ov": ["ts", "strat", "overlay", "pm_cash", "inv_value", "perp_pos", "perp_cash", "fees_perp", "n_trades",
           "perp_mark", "equity"],
    "settle": ["ts", "strat", "cid", "strike", "end", "close", "payout", "inv", "cash_delta"],
}


def log(*a):
    s = f"[{dt.datetime.now(dt.timezone.utc).strftime('%m-%d %H:%M:%S')}] " + " ".join(str(x) for x in a)
    print(s, flush=True)
    logbuf.append(s)


def http(url, data=None, timeout=15, tries=2):
    for i in range(tries):
        try:
            h = dict(UA)
            h["Accept-Encoding"] = "gzip"
            if data is not None:
                h["Content-Type"] = "application/json"
            req = urllib.request.Request(url, headers=h, data=json.dumps(data).encode() if data is not None else None)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return json.loads(body)
        except Exception as e:
            err = e
            time.sleep(0.5)
    http.errors = getattr(http, "errors", 0) + 1
    if http.errors <= 20 or http.errors % 100 == 0:
        log("http fail", url[:90], repr(err)[:120])
    return None


# ---------------- pricing ----------------
def N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def build_surface(summary):
    surf = {}
    for o in summary:
        try:
            _, d, k, _ = o["instrument_name"].split("-")
            iv = o.get("mark_iv")
            if not iv or iv <= 0:
                continue
            m = re.match(r"(\d+)([A-Z]{3})(\d{2})", d)
            exp = dt.datetime(2000 + int(m.group(3)), MON[m.group(2)], int(m.group(1)), 8, tzinfo=dt.timezone.utc).timestamp()
            surf.setdefault(exp, {})[float(k)] = iv / 100.0
        except Exception:
            continue
    return {e: sorted(v.items()) for e, v in surf.items() if len(v) >= 3}


def iv_at(smile, K):
    ks = [k for k, _ in smile]
    i = bisect.bisect_left(ks, K)
    if i <= 0:
        return smile[0][1]
    if i >= len(ks):
        return smile[-1][1]
    (k0, v0), (k1, v1) = smile[i - 1], smile[i]
    return v0 + (v1 - v0) * (K - k0) / (k1 - k0)


def total_var(surf, K, T, now):
    exps = sorted(e for e in surf if e > now + 60)
    if not exps:
        return None
    tau = max(T - now, 1.0) / YEAR
    e1 = max([e for e in exps if e <= T], default=None)
    e2 = min([e for e in exps if e > T], default=None)
    if e1 is None:
        return iv_at(surf[e2], K) ** 2 * tau
    if e2 is None:
        return iv_at(surf[e1], K) ** 2 * tau
    w1 = iv_at(surf[e1], K) ** 2 * (e1 - now) / YEAR
    w2 = iv_at(surf[e2], K) ** 2 * (e2 - now) / YEAR
    return max(w1 + (w2 - w1) * (T - e1) / (e2 - e1), 1e-10)


def call(S, K, w):
    if w is None or w <= 0:
        return max(S - K, 0.0)
    sd = math.sqrt(w)
    d1 = (math.log(S / K) + w / 2) / sd
    return S * N(d1) - K * N(d1 - sd)


def prob_above(surf, S, K, T, now):
    if now >= T:
        return 1.0 if S > K else 0.0
    h = K * 0.001
    wl, wu = total_var(surf, K - h, T, now), total_var(surf, K + h, T, now)
    if wl is None or wu is None:
        return None
    return min(1.0, max(0.0, (call(S, K - h, wl) - call(S, K + h, wu)) / (2 * h)))


# ---------------- state ----------------
SP = f"{BASE}/state.json"
if os.path.exists(SP):
    st = json.load(open(SP))
else:
    st = {"mk": {}, "strats": {}, "seen": []}
for s in STRATS:
    st["strats"].setdefault(s, {"inv": {}, "pm_cash": 0.0, "perp_pos": 0.0, "perp_cash": 0.0, "fees_perp": 0.0,
                                "rebate_est": 0.0, "n_fills": 0})
seen = set(st["seen"])
seen_order = list(st["seen"])
orders = {s: {} for s in QUEUE}    # strat -> cid -> {"bid": order, "ask": order}
quotes = {s: {} for s in STRATS}   # strat -> cid -> list of [ts, bid, ask, bid_sz, ask_sz, fair]
# hedge overlays: same Polymarket fills, different perp-hedging rules -> (band in BTC, min seconds between
# rebalances, stop hedging a market this many seconds before its resolution); None = no hedge at all
HB = HEDGE_BAND
OVERLAYS = {"tick": (HB, 0, 0), "band03": (6 * HB, 0, 0), "5min": (2 * HB, 300, 0), "30min": (2 * HB, 1800, 0),
            "stop3h": (6 * HB, 0, 3 * 3600), "30min_stop3h": (2 * HB, 1800, 3 * 3600), "none": None}
VOL = {"var_h": None, "ts": 0}     # hit markets: variance of log returns per trading hour
DIAG = {"prints": 0, "prints_quoted": 0, "shares_quoted": 0.0}
active = {}                        # cid -> market dict (currently listed)


def flush(final=False):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for name, rr in rows.items():
        if not rr:
            continue
        path = f"{LOG}/{name}_{stamp}.csv.gz"
        with gzip.open(path, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(HDR[name])
            w.writerows(rr)
        rows[name] = []
    st["seen"] = seen_order[-4000:]
    json.dump(st, open(SP, "w"))
    with open(f"{LOG}/run_{RUN}.log", "w") as f:
        f.write("\n".join(logbuf[-3000:]))
    if time.time() > DEADLINE:
        open(f"{BASE}/DONE", "w").write("deadline reached\n")
    return os.path.exists("paper2/STOP")   # git commits are done by the workflow shell loop


def trading_hours(t0, t1):
    """Hours the metals market is open between t0 and t1 (UTC winter: closed 22-23h daily, Fri 22h - Sun 23h)."""
    n, t = 0, t0
    while t < t1:
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        wd, h = d.weekday(), d.hour
        closed = (h == 22) or (wd == 4 and h >= 22) or wd == 5 or (wd == 6 and h < 23)
        if not closed:
            n += 1
        t += 600
    return n / 6.0


def is_open(now):
    return trading_hours(now, now + 600) > 0


def prob_hit(S, level, dirn, var_h, hours):
    if (dirn == "HIGH" and S >= level) or (dirn == "LOW" and S <= level):
        return 1.0
    w = var_h * max(hours, 1e-6)
    return min(1.0, 2 * (1 - N(abs(math.log(level / S)) / math.sqrt(w))))


def fair_fn(m, S, now, surf, hrs):
    if KIND == "above":
        return prob_above(surf, S, m["K"], m["end"], now)
    if not VOL["var_h"]:
        return None
    return prob_hit(S, m["K"], m["dirn"], VOL["var_h"], hrs[m["end"]])


def update_vol(now, S, mks, books, hrs):
    if CFG["vol"] != "fit":
        if now - VOL["ts"] < 1800 and VOL["var_h"]:
            return
        k = http(f"https://data-api.binance.vision/api/v3/klines?symbol={CFG['vol']}&interval=1h&limit=720", timeout=25)
        if not k:
            return
        r2 = []
        for a_, b_ in zip(k[:-1], k[1:]):
            if trading_hours(b_[0] / 1000, b_[0] / 1000 + 3600) > 0.5:
                r2.append(math.log(float(b_[4]) / float(a_[4])) ** 2)
        if len(r2) > 100:
            VOL["var_h"], VOL["ts"] = sum(r2) / len(r2), now
            log(f"vol update: {math.sqrt(VOL['var_h'] * 23 * 5 * 52) * 100:.1f}% annualised from {len(r2)} hourly returns")
        return
    pts = []
    for m in mks:
        bb, ba = (books.get(m["yes"]) or (None, None))[:2]
        if not bb or not ba or ba[0] - bb[0] > 0.12:
            continue
        mid = (bb[0] + ba[0]) / 2
        if 0.04 < mid < 0.9 and hrs[m["end"]] > 1:
            pts.append((abs(math.log(m["K"] / S)), hrs[m["end"]], mid))
    if len(pts) < 3:
        return
    best = None
    for i in range(1, 400):
        v = (0.0002 * i) ** 2                       # hourly vol 0.02% .. 8%
        err = sum((min(1.0, 2 * (1 - N(d / math.sqrt(v * h)))) - mid) ** 2 for d, h, mid in pts)
        if best is None or err < best[0]:
            best = (err, v)
    VOL["var_h"], VOL["ts"] = best[1], now


def discover(now):
    found, closed = {}, {}
    for ser in CFG["series"]:
        ev = http(f"https://gamma-api.polymarket.com/events?series_slug={ser}&closed=false&limit=20", timeout=40)
        for e in ev or []:
            try:
                end = dt.datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if KIND == "hit" and dt.datetime.fromtimestamp(end, dt.timezone.utc).hour < 12:
                end -= 7 * 3600                      # month markets end 04:59 UTC; last session closes 22:00 UTC
            if not (now < end < now + MAX_AHEAD_S):
                continue
            for m in e.get("markets") or []:
                try:
                    cid = m["conditionId"]
                    if KIND == "above":
                        k, dirn = float(re.search(r"above \$([\d,]+)", m["question"]).group(1).replace(",", "")), ""
                    else:
                        g = re.search(r"\((HIGH|LOW)\) \$([\d,\.]+)", m["question"])
                        dirn, k = g.group(1), float(g.group(2).replace(",", ""))
                    toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
                    yi = outs.index("Yes")
                    if m.get("closed"):
                        op = json.loads(m.get("outcomePrices") or "[]")
                        if len(op) == 2 and sorted(op) == ["0", "1"]:
                            closed[cid] = (float(op[yi]), k)
                        continue
                    found[cid] = {"cid": cid, "K": k, "dirn": dirn, "end": end, "ev": str(e["id"]), "yes": toks[yi],
                                  "no": toks[1 - yi], "tick": float(m.get("orderPriceMinTickSize") or 0.01),
                                  "slug": e["slug"]}
                except Exception:
                    continue
    if found:
        active.clear()
        active.update(found)
        for cid, m in found.items():
            st["mk"][cid] = {"K": m["K"], "end": m["end"], "ev": m["ev"], "slug": m["slug"], "dirn": m["dirn"]}
    for cid, (pay, k) in closed.items():             # markets resolved early (hit) or individually
        for s in STRATS:
            S_ = st["strats"][s]
            inv = S_["inv"].pop(cid, 0)
            if abs(inv) > 1e-9:
                S_["pm_cash"] += inv * pay
                rows["settle"].append([round(now), s, cid, k, now, "", pay, inv, inv * pay])
                log("settled early", k, "payout", pay, s, round(inv))
    log("discover:", len(found), "markets in", len({m['ev'] for m in found.values()}), "events;", len(closed), "closed")


def settle(now):
    if KIND != "above":
        return
    done = {}
    for cid, m in list(st["mk"].items()):
        if m["end"] > now - 120:
            continue
        held = any(abs(st["strats"][s]["inv"].get(cid, 0)) > 1e-9 for s in STRATS)
        if not held:
            if m["end"] < now - 86400:
                st["mk"].pop(cid, None)
            continue
        end = m["end"]
        if end not in done:
            k = http(f"https://data-api.binance.vision/api/v3/klines?symbol={CFG['binance']}&interval=1m"
                     f"&startTime={int(end * 1000)}&limit=1")
            done[end] = float(k[0][4]) if k and int(k[0][0]) == int(end * 1000) else None
        close = done[end]
        if close is None:
            continue
        pay = 1.0 if close > m["K"] else 0.0
        for s in STRATS:
            S_ = st["strats"][s]
            inv = S_["inv"].pop(cid, 0)
            if abs(inv) > 1e-9:
                S_["pm_cash"] += inv * pay
                rows["settle"].append([round(now), s, cid, m["K"], end, close, pay, inv, inv * pay])
        log("settled", m["K"], "close", close, "payout", pay)


def main():
    now = time.time()
    run_start = now
    surf, last = {}, {"opt": 0, "disc": 0, "snap": 0, "eq": 0, "flush": now - 660}
    S = perp = None
    nt = 0
    log(f"start run {RUN} asset={ASSET} minutes={MINUTES} strats={STRATS}")
    while time.time() - T0 < MINUTES * 60:
        t_loop = time.time()
        now = t_loop
        nt += 1
        try:
            if now - last["disc"] > DISC_S or not active:
                discover(now)
                last["disc"] = now
            b = p = True
            if KIND == "above":
                if now - last["opt"] > 60:
                    o = http("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency="
                             + CFG["deribit"] + "&kind=option", timeout=25)
                    if o and o.get("result"):
                        surf = build_surface(o["result"])
                        last["opt"] = now
                b = http("https://data-api.binance.vision/api/v3/ticker/price?symbol=" + CFG["binance"])
                p = http("https://www.deribit.com/api/v2/public/ticker?instrument_name=" + CFG["perp"])
                ok = bool(b and p and p.get("result") and surf and now - last["opt"] < 300)
                if ok:
                    S, perp = float(b["price"]), float(p["result"]["mark_price"])
            else:
                b = http("https://hermes.pyth.network/v2/updates/price/latest?ids[]=" + CFG["pyth"] + "&parsed=true")
                ok = False
                try:
                    pp = b["parsed"][0]["price"]
                    if now - float(pp["publish_time"]) < 120 and is_open(now):
                        S = perp = float(pp["price"]) * 10 ** int(pp["expo"])
                        ok = True
                except Exception:
                    pass
            mks = [m for m in active.values() if m["end"] > now]
            hrs = {e_: trading_hours(now, e_) for e_ in {m["end"] for m in mks}} if KIND == "hit" else {}
            books = {}
            if ok and mks:
                bk = []
                for i_ in range(0, len(mks), 40):
                    part = http("https://clob.polymarket.com/books", data=[{"token_id": m["yes"]} for m in mks[i_:i_ + 40]],
                                timeout=20)
                    if part is None:
                        bk = None
                        break
                    bk += part
                for x in bk or []:
                    bids = [(float(y["price"]), float(y["size"])) for y in x.get("bids") or []]
                    asks = [(float(y["price"]), float(y["size"])) for y in x.get("asks") or []]
                    books[x.get("asset_id")] = (max(bids) if bids else None, min(asks) if asks else None,
                                                {round(a, 4): b_ for a, b_ in bids}, {round(a, 4): b_ for a, b_ in asks})
                if not bk:
                    ok = False
            now = time.time()
            fair, dpds = {}, {}
            if ok and KIND == "hit":
                update_vol(now, S, mks, books, hrs)
            if ok:
                for m in mks:
                    f0 = fair_fn(m, S, now, surf, hrs)
                    f1 = fair_fn(m, S * 1.001, now, surf, hrs)
                    if f0 is None or f1 is None:
                        continue
                    fair[m["cid"]], dpds[m["cid"]] = f0, (f1 - f0) / (S * 0.001)

            # ---- 1. match real taker prints against the quotes that were live at their timestamp ----
            for evid in sorted({m["ev"] for m in mks}):
                tr = http(f"https://data-api.polymarket.com/v2/trades?event_id={evid}&limit=200", timeout=20)
                for r in (tr or {}).get("data") or []:
                    key = "|".join(str(r.get(k)) for k in ("transaction_hash", "token_id", "proxy_wallet", "size", "price"))
                    if key in seen:
                        continue
                    seen.add(key)
                    seen_order.append(key)
                    ts = float(r["timestamp"])
                    m = active.get(r.get("condition_id"))
                    if ts < run_start or not m:
                        continue
                    buy = r["side"] == "BUY"
                    DIAG["prints"] += 1
                    if (quotes["hs2"].get(m["cid"]) or [[0, None]])[-1][1] is not None:
                        DIAG["prints_quoted"] += 1
                        DIAG["shares_quoted"] += float(r["size"])
                    if r["token_id"] == m["yes"]:
                        side, P = (1 if buy else -1), float(r["price"])
                    else:
                        side, P = (-1 if buy else 1), 1 - float(r["price"])
                    for s in STRATS:
                        S_ = st["strats"][s]
                        inv = S_["inv"].get(m["cid"], 0.0)
                        if s in QUEUE:
                            o = (orders[s].get(m["cid"]) or {}).get("ask" if side == 1 else "bid")
                            if not o or o["sz"] <= 0 or ts < o["ts"]:
                                continue
                            through = (P > o["px"] + 1e-6) if side == 1 else (P < o["px"] - 1e-6)
                            if through:
                                sz = min(float(r["size"]), o["sz"])
                            elif abs(P - o["px"]) <= 1e-6:
                                o["ahead"] -= float(r["size"])
                                if o["ahead"] >= 0:
                                    continue
                                sz, o["ahead"] = min(-o["ahead"], o["sz"]), 0.0
                            else:
                                continue
                            o["sz"] -= sz
                            px, sgn, qt = o["px"], -side, [0, 0, 0, 0, 0, o["fair"]]
                        else:
                            ql = quotes[s].get(m["cid"]) or []
                            i = bisect.bisect_right([x[0] for x in ql], ts) - 1
                            if i < 0 or ql[i][1] is None:
                                continue
                            qt = ql[i]
                            if side == 1 and qt[4] > 0 and P > qt[2] + 1e-9:      # taker buys YES through our ask
                                sz, px, sgn = min(float(r["size"]), qt[4]), qt[2], -1
                                qt[4] -= sz
                            elif side == -1 and qt[3] > 0 and P < qt[1] - 1e-9:   # taker sells YES through our bid
                                sz, px, sgn = min(float(r["size"]), qt[3]), qt[1], 1
                                qt[3] -= sz
                            else:
                                continue
                        inv += sgn * sz
                        S_["inv"][m["cid"]] = inv
                        S_["pm_cash"] -= sgn * sz * px
                        S_["rebate_est"] += REBATE * FEE_PM * sz * px * (1 - px)
                        S_["n_fills"] += 1
                        rows["fills"].append([round(now, 1), ts, s, m["cid"], m["K"], m["end"], "BUY" if sgn == 1 else "SELL",
                                              px, sz, round(qt[5], 4), round(fair.get(m["cid"], float("nan")), 4), S,
                                              round(P, 4), inv])

            # ---- 2. new quotes ----
            now = time.time()
            for s, hs in STRATS.items():
                for m in mks:
                    cid = m["cid"]
                    ql = quotes[s].setdefault(cid, [])
                    f = fair.get(cid)
                    bb, ba = books.get(m["yes"], (None, None))[:2]
                    if (not ok or f is None or not (0.05 <= f <= 0.95) or m["end"] - now < STOP_QUOTE_S
                            or bb is None or ba is None or time.time() > DEADLINE):
                        ql.append([now, None, None, 0, 0, f])
                        if s in QUEUE:
                            orders[s][cid] = {"bid": None, "ask": None}
                    else:
                        tk = m["tick"] if (0.04 < f < 0.96 and m["tick"] >= 0.01) else m["tick"]
                        inv = st["strats"][s]["inv"].get(cid, 0.0)
                        fb = f * (1 - BLEND.get(s, 0)) + BLEND.get(s, 0) * (bb[0] + ba[0]) / 2
                        c = fb - max(-0.03, min(0.03, 0.01 * inv / SIZE))
                        bid = math.floor((c - hs) / tk + 1e-9) * tk
                        ask = math.ceil((c + hs) / tk - 1e-9) * tk
                        ask = max(ask, bb[0] + tk)
                        bid = min(bid, ba[0] - tk)
                        bsz = SIZE if (inv < MAXINV and 0 < bid < 1) else 0
                        asz = SIZE if (inv > -MAXINV and 0 < ask < 1) else 0
                        ql.append([now, round(bid, 4), round(ask, 4), bsz, asz, f])
                        if s in QUEUE:
                            od = orders[s].setdefault(cid, {"bid": None, "ask": None})
                            dp = books[m["yes"]]
                            for nm, px_, sz_, lv in (("bid", round(bid, 4), bsz, dp[2]), ("ask", round(ask, 4), asz, dp[3])):
                                o = od[nm]
                                if sz_ <= 0:
                                    od[nm] = None
                                elif o and abs(o["px"] - px_) < 1e-6 and o["sz"] > 0:
                                    o["fair"] = f                      # same price: keep queue position
                                else:
                                    od[nm] = {"px": px_, "sz": sz_, "ahead": lv.get(px_, 0.0), "ts": now, "fair": f}
                    del ql[:-90]

            # ---- 3. hedge ----
            if ok:
                for s in STRATS:
                    S_ = st["strats"][s]
                    tgt = -sum(S_["inv"].get(m["cid"], 0.0) * dpds.get(m["cid"], 0.0) for m in mks)
                    dq = tgt - S_["perp_pos"]
                    if abs(dq) > HEDGE_BAND:
                        fee = abs(dq) * perp * PERP_FEE
                        S_["perp_cash"] -= dq * perp
                        S_["fees_perp"] += fee
                        S_["perp_pos"] = tgt
                        rows["hedges"].append([round(now, 1), s, round(dq, 5), perp, round(fee, 4), round(tgt, 5)])
                    for ov, cfg in OVERLAYS.items():
                        O = st.setdefault("ov", {}).setdefault(s, {}).setdefault(
                            ov, {"pos": 0.0, "cash": 0.0, "fees": 0.0, "last": 0, "n": 0})
                        if cfg is None:
                            continue
                        band, minint, stop_s = cfg
                        tgt = -sum(S_["inv"].get(m["cid"], 0.0) * dpds.get(m["cid"], 0.0)
                                   for m in mks if m["end"] - now > stop_s)
                        dq = tgt - O["pos"]
                        if abs(dq) > band and now - O["last"] >= minint:
                            O["cash"] -= dq * perp
                            O["fees"] += abs(dq) * perp * PERP_FEE
                            O["pos"], O["last"], O["n"] = tgt, now, O["n"] + 1
            settle(now)

            # ---- 4. logging ----
            if ok and now - last["snap"] > SNAP_S:
                last["snap"] = now
                for m in mks:
                    bb, ba = books.get(m["yes"], (None, None))[:2]
                    rows["snap"].append([round(now), m["cid"], m["K"], m["end"], S, perp,
                                         round(fair[m["cid"]], 5) if m["cid"] in fair else "",
                                         bb[0] if bb else "", ba[0] if ba else "", bb[1] if bb else "", ba[1] if ba else "",
                                         m["dirn"], VOL["var_h"] or ""])
            if ok and now - last["eq"] > EQ_S:
                last["eq"] = now
                for s in STRATS:
                    S_ = st["strats"][s]
                    iv_ = 0.0
                    for cid, q in S_["inv"].items():
                        mm = st["mk"].get(cid)
                        fv = fair.get(cid)
                        if fv is None and mm and KIND == "above":
                            fv = 1.0 if S > mm["K"] else 0.0
                        if fv is None:
                            fv = st.setdefault("lastfair", {}).get(cid, 0.0)
                        st.setdefault("lastfair", {})[cid] = fv
                        iv_ += q * (fv or 0.0)
                    eq = S_["pm_cash"] + iv_ + S_["perp_cash"] + S_["perp_pos"] * perp - S_["fees_perp"]
                    rows["equity"].append([round(now), s, round(S_["pm_cash"], 2), round(iv_, 2), round(S_["perp_cash"], 2),
                                           round(S_["perp_pos"], 5), perp, round(S_["fees_perp"], 2),
                                           round(S_["rebate_est"], 2), S_["n_fills"],
                                           round(sum(abs(v) for v in S_["inv"].values())), round(eq, 2)])
                    for ov, O in (st.get("ov", {}).get(s) or {}).items():
                        rows["ov"].append([round(now), s, ov, round(S_["pm_cash"], 2), round(iv_, 2), round(O["pos"], 5),
                                           round(O["cash"], 2), round(O["fees"], 2), O["n"], perp,
                                           round(S_["pm_cash"] + iv_ + O["cash"] + O["pos"] * perp - O["fees"], 2)])
                e = {s: rows["equity"][-len(STRATS) + i][-1] for i, s in enumerate(STRATS)}
                log(f"tick {nt} S={S:.0f} mkts={len(mks)} quoted={sum(1 for m in mks if (quotes['hs2'].get(m['cid']) or [[0, None]])[-1][1] is not None)}"
                    f" fills={ {s: st['strats'][s]['n_fills'] for s in STRATS} } equity={e} diag={DIAG}")
            if not ok and nt % 15 == 1:
                log("tick not ok: spot", bool(b), "perp", bool(p), "surf", len(surf), "mkts", len(mks))
        except Exception:
            log("tick error", traceback.format_exc()[-1500:])
        if time.time() - last["flush"] > FLUSH_S:
            last["flush"] = time.time()
            if flush():
                log("STOP file found, exiting")
                break
        time.sleep(max(1.0, TICK_S - (time.time() - t_loop)))
    log("end run, ticks", nt)
    flush(final=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal", traceback.format_exc()[-2000:])
        flush(final=True)
