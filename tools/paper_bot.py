"""Paper-trading bot: market making on Polymarket "Bitcoin above X on <date>" markets.

Fair value comes from Deribit option implied vols (digital = call-spread limit), spot from
Binance (the resolution source). Quotes are simulated only; fills are inferred from real
Polymarket taker prints that trade THROUGH our simulated quote. Inventory is delta-hedged
with a simulated Deribit perpetual. Nothing is ever sent to any exchange.

Usage: python tools/paper_bot.py MINUTES
"""
import bisect, csv, datetime as dt, gzip, json, math, os, re, subprocess, sys, time, traceback, urllib.request

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 5
BASE, LOG = "paper", "paper/log"
os.makedirs(LOG, exist_ok=True)
STRATS = {"hs1": 0.01, "hs2": 0.02, "hs4": 0.04, "hs2b": 0.02, "hs1q": 0.01, "hs2q": 0.02, "hs2bq": 0.02}   # half-spread in price units
BLEND = {"hs2b": 0.5, "hs2bq": 0.5}
QUEUE = {"hs1q", "hs2q", "hs2bq"}   # fills also at our price once the displayed queue ahead of us has traded
#                               # weight of Polymarket mid in the quote centre
SIZE, MAXINV = 200, 1000                            # shares per quote, max |inventory| per market
TICK_S, FLUSH_S, SNAP_S, EQ_S, DISC_S = 20, 900, 60, 300, 600
FEE_PM, REBATE, PERP_FEE = 0.07, 0.20, 0.0005
STOP_QUOTE_S, MAX_AHEAD_S = 3600, 72 * 3600
HEDGE_BAND = 0.005                                  # BTC
DEADLINE = dt.datetime(2026, 11, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp()
YEAR = 365.0 * 86400
UA = {"User-Agent": "paper-research/0.1"}
T0 = time.time()
RUN = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
logbuf, rows = [], {"fills": [], "hedges": [], "snap": [], "equity": [], "settle": []}
HDR = {
    "fills": ["ts", "trade_ts", "strat", "cid", "strike", "end", "side", "px", "size", "fair_quote", "fair_now",
              "spot", "trade_px_yes", "inv_after"],
    "hedges": ["ts", "strat", "dq", "px", "fee", "pos_after"],
    "snap": ["ts", "cid", "strike", "end", "spot", "perp", "fair", "best_bid", "best_ask", "bid_sz", "ask_sz"],
    "equity": ["ts", "strat", "pm_cash", "inv_value", "perp_cash", "perp_pos", "perp_mark", "fees_perp",
               "rebate_est", "n_fills", "gross_inv_shares", "equity"],
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
            if data is not None:
                h["Content-Type"] = "application/json"
            req = urllib.request.Request(url, headers=h, data=json.dumps(data).encode() if data is not None else None)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
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
    cmd = ("git add paper && (git diff --staged --quiet || git commit -qm 'paper: " + stamp + "') && "
           "git pull -q --rebase origin claude/data-pull && git push -q origin HEAD:claude/data-pull")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        log("git fail", (r.stdout + r.stderr)[-300:])
    return os.path.exists(f"{BASE}/STOP")


def discover(now):
    ev = http("https://gamma-api.polymarket.com/events?tag_slug=bitcoin&closed=false&limit=100"
              "&order=volume24hr&ascending=false", timeout=40)
    if not ev:
        return
    found = {}
    for e in ev:
        if e.get("seriesSlug") != "btc-multi-strikes-weekly":
            continue
        try:
            end = dt.datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if not (now < end < now + MAX_AHEAD_S):
            continue
        for m in e.get("markets") or []:
            try:
                k = float(re.search(r"above \$([\d,]+)", m["question"]).group(1).replace(",", ""))
                toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
                yi = outs.index("Yes")
                cid = m["conditionId"]
                found[cid] = {"cid": cid, "K": k, "end": end, "ev": str(e["id"]), "yes": toks[yi], "no": toks[1 - yi],
                              "tick": float(m.get("orderPriceMinTickSize") or 0.01), "slug": e["slug"]}
            except Exception:
                continue
    if found:
        active.clear()
        active.update(found)
        for cid, m in found.items():
            st["mk"][cid] = {"K": m["K"], "end": m["end"], "ev": m["ev"], "slug": m["slug"]}
    log("discover:", len(found), "markets in", len({m['ev'] for m in found.values()}), "events")


def settle(now):
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
            k = http(f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m"
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
    surf, last = {}, {"opt": 0, "disc": 0, "snap": 0, "eq": 0, "flush": now}
    S = perp = None
    nt = 0
    log(f"start run {RUN} minutes={MINUTES} strats={STRATS}")
    while time.time() - T0 < MINUTES * 60:
        t_loop = time.time()
        now = t_loop
        nt += 1
        try:
            if now - last["disc"] > DISC_S or not active:
                discover(now)
                last["disc"] = now
            if now - last["opt"] > 60:
                o = http("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
                         timeout=25)
                if o and o.get("result"):
                    surf = build_surface(o["result"])
                    last["opt"] = now
            b = http("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT")
            p = http("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")
            ok = bool(b and p and p.get("result") and surf and now - last["opt"] < 300)
            if ok:
                S, perp = float(b["price"]), float(p["result"]["mark_price"])
            mks = [m for m in active.values() if m["end"] > now]
            books = {}
            if ok and mks:
                bk = http("https://clob.polymarket.com/books", data=[{"token_id": m["yes"]} for m in mks], timeout=20)
                for x in bk or []:
                    bids = [(float(y["price"]), float(y["size"])) for y in x.get("bids") or []]
                    asks = [(float(y["price"]), float(y["size"])) for y in x.get("asks") or []]
                    books[x.get("asset_id")] = (max(bids) if bids else None, min(asks) if asks else None,
                                                {round(a, 4): b_ for a, b_ in bids}, {round(a, 4): b_ for a, b_ in asks})
                if not bk:
                    ok = False
            now = time.time()
            fair, dpds = {}, {}
            if ok:
                for m in mks:
                    f0 = prob_above(surf, S, m["K"], m["end"], now)
                    f1 = prob_above(surf, S * 1.001, m["K"], m["end"], now)
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
            settle(now)

            # ---- 4. logging ----
            if ok and now - last["snap"] > SNAP_S:
                last["snap"] = now
                for m in mks:
                    bb, ba = books.get(m["yes"], (None, None))[:2]
                    rows["snap"].append([round(now), m["cid"], m["K"], m["end"], S, perp,
                                         round(fair[m["cid"]], 5) if m["cid"] in fair else "",
                                         bb[0] if bb else "", ba[0] if ba else "", bb[1] if bb else "", ba[1] if ba else ""])
            if ok and now - last["eq"] > EQ_S:
                last["eq"] = now
                for s in STRATS:
                    S_ = st["strats"][s]
                    iv_ = 0.0
                    for cid, q in S_["inv"].items():
                        mm = st["mk"].get(cid)
                        fv = fair.get(cid)
                        if fv is None and mm:
                            fv = 1.0 if S > mm["K"] else 0.0
                        iv_ += q * (fv or 0.0)
                    eq = S_["pm_cash"] + iv_ + S_["perp_cash"] + S_["perp_pos"] * perp - S_["fees_perp"]
                    rows["equity"].append([round(now), s, round(S_["pm_cash"], 2), round(iv_, 2), round(S_["perp_cash"], 2),
                                           round(S_["perp_pos"], 5), perp, round(S_["fees_perp"], 2),
                                           round(S_["rebate_est"], 2), S_["n_fills"],
                                           round(sum(abs(v) for v in S_["inv"].values())), round(eq, 2)])
                e = {s: rows["equity"][-len(STRATS) + i][-1] for i, s in enumerate(STRATS)}
                log(f"tick {nt} S={S:.0f} mkts={len(mks)} quoted={sum(1 for m in mks if (quotes['hs2'].get(m['cid']) or [[0, None]])[-1][1] is not None)}"
                    f" fills={ {s: st['strats'][s]['n_fills'] for s in STRATS} } equity={e}")
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
