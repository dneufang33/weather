"""
weather_bot.py — Multi-API Polymarket Weather Trading Bot
=========================================================
Screens weather markets every 5 minutes, computes ensemble forecasts,
identifies edge, sizes positions with Kelly criterion, and logs paper trades.

Usage:
  python weather_bot.py                  # single scan cycle
  python weather_bot.py --loop           # continuous 5-min loop
  python weather_bot.py --report         # show paper trading report
  python weather_bot.py --dry-run        # scan but don't log trades

Architecture:
  1. Fetch open weather markets from Polymarket
  2. For each market: fetch forecasts from 4 Open-Meteo models
  3. Compute ensemble probability + compare to market price
  4. If edge > threshold and models agree: log paper trade
  5. Track all trades in trades.jsonl for analysis
"""

import re, sys, json, math, time, argparse, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

import requests

# ── config ───────────────────────────────────────────────────────────────────

MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "best_match"]

# Model weights from backtest (inverse-RMSE normalized)
MODEL_WEIGHTS = {
    "ecmwf_ifs025": 0.42,
    "gfs_seamless": 0.22,
    "icon_seamless": 0.18,
    "best_match":    0.18,
}

# Trading parameters
MIN_EDGE       = 0.08    # 8% minimum edge to enter
MIN_AGREEMENT  = 3       # at least 3/4 models must agree on direction
MAX_POSITION   = 0.05    # 5% of bankroll per market
MAX_CITY_EXPOSURE = 0.10 # 10% of bankroll per city
KELLY_FRACTION = 0.25    # quarter-Kelly for safety
MIN_VOLUME     = 300     # minimum market volume
MAX_VOLUME     = 50000   # max volume (above this, market too efficient)
BANKROLL       = 1000    # starting paper bankroll (USDC)

# Sweet spot: only trade markets closing in this window
SWEET_SPOT_MIN_H = 6     # don't trade if less than 6h to close
SWEET_SPOT_MAX_H = 96    # don't trade if more than 96h to close

# ── endpoints ────────────────────────────────────────────────────────────────

GAMMA_MARKETS  = "https://gamma-api.polymarket.com/markets"
OM_FORECAST    = "https://api.open-meteo.com/v1/forecast"

# ── city data ────────────────────────────────────────────────────────────────

CITY_COORDS = {
    "london":      (51.5074,  -0.1278),  "paris":      (48.8566,   2.3522),
    "berlin":      (52.5200,  13.4050),  "madrid":     (40.4168,  -3.7038),
    "rome":        (41.9028,  12.4964),  "amsterdam":  (52.3676,   4.9041),
    "brussels":    (50.8503,   4.3517),  "vienna":     (48.2082,  16.3738),
    "zurich":      (47.3769,   8.5417),  "new york":   (40.7128, -74.0060),
    "los angeles": (34.0522,-118.2437),  "chicago":    (41.8781, -87.6298),
    "miami":       (25.7617, -80.1918),  "washington": (38.9072, -77.0369),
    "toronto":     (43.6532, -79.3832),  "sydney":     (-33.8688,151.2093),
    "tokyo":       (35.6762, 139.6503),  "dubai":      (25.2048,  55.2708),
    "singapore":   ( 1.3521, 103.8198),  "denver":     (39.7392,-104.9903),
    "houston":     (29.7604, -95.3698),  "phoenix":    (33.4484,-112.0740),
    "atlanta":     (33.7490, -84.3880),  "seattle":    (47.6062,-122.3321),
    "boston":       (42.3601, -71.0589),  "san francisco": (37.7749,-122.4194),
    "dallas":      (32.7767, -96.7970),  "mumbai":     (19.0760,  72.8777),
    "bangkok":     (13.7563, 100.5018),  "hong kong":  (22.3193, 114.1694),
    "seoul":       (37.5665, 126.9780),  "mexico city": (19.4326, -99.1332),
    "sao paulo":   (-23.5505,-46.6333),  "cairo":      (30.0444,  31.2357),
}

ABOVE_KW   = ["exceed","above","over ","higher than","more than",
               "at least","reach or exceed","or higher","or above"]
BELOW_KW   = ["below","under ","less than","colder than",
               "no more than","or lower","or below"]
WEATHER_KW = ["temperature","fahrenheit","celsius","degrees","high of",
              "low of","\u00b0f","\u00b0c","\u00b0","weather","temp ",
              "highest temperature","high in","the high in"]


# ── parsing ──────────────────────────────────────────────────────────────────

def find_city(q):
    ql = q.lower()
    for city, (lat, lon) in CITY_COORDS.items():
        if city in ql:
            return city, lat, lon
    return None


def parse_threshold(question):
    """Returns (threshold_c, market_type, half_width_c) or None."""
    q = question.lower()

    # Bracket: "between X-Y°F"
    m_f = re.search(r"between\s+(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?)"
                    r"\s*\u00b0?\s*f(?:ahrenheit)?", q)
    m_c = re.search(r"between\s+(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?)"
                    r"\s*\u00b0?\s*c(?:elsius)?", q)
    if m_f:
        lo, hi = float(m_f.group(1)), float(m_f.group(2))
        return (round(((lo+hi)/2-32)*5/9, 4), "bracket",
                round(((hi-lo)/2+0.5)*(5/9), 4))
    if m_c:
        lo, hi = float(m_c.group(1)), float(m_c.group(2))
        return ((lo+hi)/2, "bracket", round((hi-lo)/2+0.5, 4))

    # Single threshold
    if   any(k in q for k in ABOVE_KW):   mt = "above"
    elif any(k in q for k in BELOW_KW):   mt = "below"
    elif "or higher" in q or "or above" in q: mt = "above"
    elif "or lower" in q or "or below" in q: mt = "below"
    else: return None

    m = re.search(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*\u00b0?\s*f(?:ahrenheit)?", q)
    if m:
        return (round((float(m.group(1))-32)*5/9, 4), mt, round(0.5*5/9, 4))
    m = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*\u00b0?\s*c(?:elsius)?", q)
    if m: return (float(m.group(1)), mt, 0.5)
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*degrees?", q)
    if m: return (float(m.group(1)), mt, 0.5)
    return None


def parse_date(question, fallback_iso):
    """Extract the target date from the question text."""
    q = question.lower()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", question)
    if m: return m.group(1)

    # Extract year from fallback
    fb_year = None
    if fallback_iso:
        ym = re.search(r"(\d{4})", fallback_iso)
        if ym: fb_year = int(ym.group(1))

    months = dict(january=1,february=2,march=3,april=4,may=5,june=6,
                  july=7,august=8,september=9,october=10,november=11,december=12)
    for name, num in months.items():
        pos = q.find(name)
        if pos == -1: continue
        dm = re.search(r"\b(\d{1,2})\b", q[pos+len(name):])
        if not dm: continue
        day = int(dm.group(1))
        if not 1 <= day <= 31: continue
        yr = fb_year or datetime.now(timezone.utc).year
        try:
            return datetime(yr, num, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
        except ValueError: continue
    return fallback_iso[:10] if fallback_iso else None


def get_market_price(market):
    """Get current YES price from market data."""
    prices = market.get("outcomePrices") or []
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except: prices = []
    if prices:
        try:
            p = float(prices[0])
            if 0.001 < p < 0.999:  # not yet resolved
                return p
        except: pass

    # Try bestBid/bestAsk midpoint
    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    if bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if 0 < b < 1 and 0 < a < 1:
                return (b + a) / 2
        except: pass

    ltp = market.get("lastTradePrice")
    if ltp is not None:
        try:
            p = float(ltp)
            if 0.001 < p < 0.999:
                return p
        except: pass

    return None


# ── probability model ────────────────────────────────────────────────────────

def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_yes(fc, thr, mt, sigma, hw=0.5):
    """Compute P(YES) given forecast, threshold, market type, and uncertainty."""
    s = max(sigma, 0.1)  # floor at 0.1°C
    if mt == "above":
        return max(0.01, min(0.99, 1 - norm_cdf((thr - fc) / s)))
    if mt == "below":
        return max(0.01, min(0.99, norm_cdf((thr - fc) / s)))
    # bracket
    return max(0.01, min(0.99,
        norm_cdf((thr + hw - fc) / s) - norm_cdf((thr - hw - fc) / s)))


# ── forecast fetching ────────────────────────────────────────────────────────

def fetch_forecasts(lat, lon, target_date, models=None):
    """
    Fetch current forecasts from Open-Meteo for a specific date.
    Returns {model_name: {"temp_max_c": float, "sigma_c": float}} or empty.
    """
    if models is None:
        models = MODELS

    results = {}
    for model in models:
        try:
            r = requests.get(OM_FORECAST, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "timezone": "UTC",
                "models": model,
                "start_date": target_date,
                "end_date": target_date,
            }, timeout=15)

            if r.status_code == 429:
                time.sleep(2)
                r = requests.get(OM_FORECAST, params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max", "timezone": "UTC",
                    "models": model,
                    "start_date": target_date, "end_date": target_date,
                }, timeout=15)

            if r.status_code != 200:
                continue

            data = r.json().get("daily", {})
            # Find the temperature key (varies by model)
            tkey = next((k for k in data if "temperature_2m_max" in k), None)
            temps = data.get(tkey, []) if tkey else []
            if temps and temps[0] is not None:
                results[model] = {"temp_max_c": temps[0]}

        except Exception as e:
            print(f"  [forecast/{model}] {e}", flush=True)

        time.sleep(0.1)  # rate limit courtesy

    # Compute per-model sigma from spread
    if len(results) >= 2:
        vals = [r["temp_max_c"] for r in results.values()]
        mean = sum(vals) / len(vals)
        spread = max(vals) - min(vals)
        # Sigma estimate: based on model spread + base uncertainty
        for model in results:
            diff = abs(results[model]["temp_max_c"] - mean)
            results[model]["sigma_c"] = max(0.3, spread * 0.4 + diff * 0.3)
    else:
        for model in results:
            results[model]["sigma_c"] = 1.5  # high uncertainty with single model

    return results


def compute_ensemble(forecasts, thr, mt, hw):
    """
    Compute weighted ensemble probability and model agreement.
    Returns (p_ensemble, n_agree_direction, per_model_probs, ensemble_fc_c, model_spread_c)
    """
    if not forecasts:
        return None, 0, {}, None, None

    # Weighted ensemble forecast temperature
    total_w = 0
    weighted_fc = 0
    for model, data in forecasts.items():
        w = MODEL_WEIGHTS.get(model, 0.1)
        weighted_fc += data["temp_max_c"] * w
        total_w += w

    if total_w == 0:
        return None, 0, {}, None, None

    ens_fc = weighted_fc / total_w

    # Model spread (for uncertainty)
    temps = [d["temp_max_c"] for d in forecasts.values()]
    model_spread = max(temps) - min(temps) if len(temps) >= 2 else 2.0

    # Ensemble sigma: base from model spread, increases with fewer models
    ens_sigma = max(0.3, model_spread * 0.5 + 0.2 * (4 - len(forecasts)))

    # Ensemble probability
    p_ens = p_yes(ens_fc, thr, mt, ens_sigma, hw)

    # Per-model probabilities
    per_model = {}
    for model, data in forecasts.items():
        sigma = data.get("sigma_c", 1.0)
        p = p_yes(data["temp_max_c"], thr, mt, sigma, hw)
        per_model[model] = {
            "temp_c": data["temp_max_c"],
            "sigma_c": sigma,
            "p_yes": round(p, 4),
            "direction": "YES" if p > 0.5 else "NO",
        }

    # Count agreement
    ens_dir = "YES" if p_ens > 0.5 else "NO"
    n_agree = sum(1 for m in per_model.values() if m["direction"] == ens_dir)

    return round(p_ens, 4), n_agree, per_model, round(ens_fc, 2), round(model_spread, 2)


# ── Kelly criterion ──────────────────────────────────────────────────────────

def kelly_size(p_model, p_market, direction, bankroll,
               fraction=KELLY_FRACTION, max_pct=MAX_POSITION):
    """
    Compute Kelly-optimal position size.

    For a binary outcome market:
      If buying YES at price p_market:
        b = (1/p_market) - 1  (payout odds)
        kelly = (p_model * b - (1 - p_model)) / b

      If buying NO at price (1 - p_market):
        b = (1/(1-p_market)) - 1
        kelly = ((1-p_model) * b - p_model) / b

    Returns position size in USDC (0 if negative edge).
    """
    if direction == "YES":
        price = p_market
        p_win = p_model
    else:
        price = 1 - p_market
        p_win = 1 - p_model

    if price <= 0.01 or price >= 0.99:
        return 0

    b = (1 / price) - 1  # payout odds
    if b <= 0:
        return 0

    k = (p_win * b - (1 - p_win)) / b
    if k <= 0:
        return 0

    # Apply fraction and cap
    size = bankroll * k * fraction
    size = min(size, bankroll * max_pct)
    size = max(size, 0)

    return round(size, 2)


# ── market fetching ──────────────────────────────────────────────────────────

def fetch_open_weather_markets():
    """Fetch currently OPEN weather markets from Polymarket."""
    print("[scan] Fetching open weather markets...", flush=True)

    collected = []
    now = datetime.now(timezone.utc)

    for offset in range(0, 2000, 100):
        try:
            r = requests.get(GAMMA_MARKETS, params={
                "closed": "false",
                "active": "true",
                "limit": 100,
                "offset": offset,
            }, timeout=20)
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
        except Exception as e:
            print(f"  [gamma] error: {e}", flush=True)
            break

        for m in batch:
            q = (m.get("question", "") or "").lower()
            if not any(kw in q for kw in WEATHER_KW):
                continue
            if not find_city(q):
                continue
            if parse_threshold(q) is None:
                continue

            vol = float(m.get("volume", 0) or 0)
            if vol < MIN_VOLUME:
                continue

            end_iso = m.get("endDateIso") or m.get("endDate") or ""
            if not end_iso:
                continue
            try:
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            except:
                continue

            # Check sweet spot window
            hours_to_close = (end_dt - now).total_seconds() / 3600
            if hours_to_close < SWEET_SPOT_MIN_H or hours_to_close > SWEET_SPOT_MAX_H:
                continue

            # Must have a tradeable price
            price = get_market_price(m)
            if price is None:
                continue

            collected.append({
                "raw": m,
                "price": price,
                "hours_to_close": round(hours_to_close, 1),
                "end_dt": end_dt,
                "volume": vol,
            })

        if len(batch) < 100:
            break
        time.sleep(0.05)

    print(f"  -> {len(collected)} open weather markets in sweet spot window", flush=True)
    return collected


# ── trade logging ────────────────────────────────────────────────────────────

TRADES_FILE = Path(__file__).parent / "trades.jsonl"
STATE_FILE  = Path(__file__).parent / "bot_state.json"


def load_state():
    """Load bot state (bankroll, positions, etc.)."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "bankroll": BANKROLL,
        "positions": {},  # market_id -> {direction, size, entry_price, ...}
        "total_trades": 0,
        "total_pnl": 0,
        "started": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str),
                          encoding="utf-8")


def log_trade(trade):
    """Append a trade to the JSONL log."""
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trade, default=str) + "\n")


def load_trades():
    """Load all trades from the log."""
    if not TRADES_FILE.exists():
        return []
    trades = []
    for line in TRADES_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                trades.append(json.loads(line))
            except:
                pass
    return trades


# ── position management ──────────────────────────────────────────────────────

def check_resolved_positions(state):
    """Check if any open positions have resolved. Update PnL."""
    if not state["positions"]:
        return state

    resolved_ids = []
    for market_id, pos in state["positions"].items():
        try:
            r = requests.get(f"{GAMMA_MARKETS}", params={
                "id": market_id, "limit": 1
            }, timeout=10)
            if r.status_code != 200:
                continue
            markets = r.json()
            if not markets:
                continue
            m = markets[0]
            if not m.get("closed"):
                continue

            # Market is closed — determine outcome
            prices = m.get("outcomePrices") or []
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: continue
            if not prices:
                continue

            try:
                yes_price = float(prices[0])
            except:
                continue

            outcome = "YES" if yes_price > 0.5 else "NO"
            won = (outcome == pos["direction"])

            if won:
                pnl = pos["size"] * ((1 / pos["entry_price"]) - 1)
            else:
                pnl = -pos["size"]

            pnl = round(pnl, 2)
            state["bankroll"] += pos["size"] + pnl  # return stake + profit/loss
            state["total_pnl"] += pnl

            log_trade({
                "type": "RESOLVE",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_id": market_id,
                "question": pos.get("question", ""),
                "direction": pos["direction"],
                "entry_price": pos["entry_price"],
                "size": pos["size"],
                "outcome": outcome,
                "won": won,
                "pnl": pnl,
                "bankroll_after": round(state["bankroll"], 2),
            })

            resolved_ids.append(market_id)
            print(f"  [resolved] {'WIN' if won else 'LOSS'} {pos['direction']} "
                  f"@ {pos['entry_price']:.3f} -> {outcome} | "
                  f"PnL: ${pnl:+.2f} | Bankroll: ${state['bankroll']:.2f}",
                  flush=True)

        except Exception as e:
            print(f"  [resolve-check] {market_id}: {e}", flush=True)

    for mid in resolved_ids:
        del state["positions"][mid]

    return state


# ── main scan cycle ──────────────────────────────────────────────────────────

def scan_cycle(dry_run=False):
    """Run one full scan cycle."""
    state = load_state()
    now = datetime.now(timezone.utc)

    print(f"\n{'='*70}", flush=True)
    print(f"  WEATHER BOT SCAN  |  {now.strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
    print(f"  Bankroll: ${state['bankroll']:.2f}  |  "
          f"Open positions: {len(state['positions'])}  |  "
          f"Total trades: {state['total_trades']}", flush=True)
    print(f"{'='*70}", flush=True)

    # Check resolved positions
    if state["positions"]:
        print("\n[check] Checking resolved positions...", flush=True)
        state = check_resolved_positions(state)

    # Fetch open markets
    markets = fetch_open_weather_markets()

    if not markets:
        print("[scan] No weather markets in sweet spot window.", flush=True)
        save_state(state)
        return state

    # Compute city exposure
    city_exposure = defaultdict(float)
    for pos in state["positions"].values():
        city_exposure[pos.get("city", "")] += pos["size"]

    # Analyze each market
    signals = []
    for mkt in markets:
        m = mkt["raw"]
        q = m.get("question", "")
        market_id = str(m.get("id", ""))

        # Skip if already have position
        if market_id in state["positions"]:
            continue

        loc = find_city(q)
        tinfo = parse_threshold(q)
        if not loc or not tinfo:
            continue

        city, lat, lon = loc
        thr, mt, hw = tinfo
        end_iso = m.get("endDateIso") or m.get("endDate") or ""
        target_date = parse_date(q, end_iso)
        if not target_date:
            continue

        p_market = mkt["price"]

        # Fetch forecasts
        forecasts = fetch_forecasts(lat, lon, target_date)
        if len(forecasts) < 2:
            continue

        # Compute ensemble
        p_ens, n_agree, per_model, ens_fc, spread = compute_ensemble(
            forecasts, thr, mt, hw)

        if p_ens is None:
            continue

        # Determine direction and edge
        direction = "YES" if p_ens > p_market else "NO"
        edge = abs(p_ens - p_market)

        # Entry criteria
        if edge < MIN_EDGE:
            continue
        if n_agree < MIN_AGREEMENT:
            continue
        if mkt["volume"] > MAX_VOLUME:
            continue

        # Kelly sizing
        size = kelly_size(p_ens, p_market, direction, state["bankroll"])
        if size < 1:  # minimum $1 trade
            continue

        # Check city exposure
        if city_exposure[city] + size > state["bankroll"] * MAX_CITY_EXPOSURE:
            size = max(0, state["bankroll"] * MAX_CITY_EXPOSURE - city_exposure[city])
            if size < 1:
                continue

        # Check bankroll
        if size > state["bankroll"] * 0.5:  # never risk more than 50% total
            continue

        entry_price = p_market if direction == "YES" else (1 - p_market)

        signals.append({
            "market_id": market_id,
            "question": q,
            "city": city,
            "target_date": target_date,
            "hours_to_close": mkt["hours_to_close"],
            "volume": mkt["volume"],
            "p_market": p_market,
            "p_ensemble": p_ens,
            "edge": round(edge, 4),
            "direction": direction,
            "n_agree": n_agree,
            "n_models": len(forecasts),
            "ensemble_fc_c": ens_fc,
            "model_spread_c": spread,
            "per_model": per_model,
            "kelly_size": size,
            "entry_price": entry_price,
        })

    # Sort by edge (best first)
    signals.sort(key=lambda x: x["edge"], reverse=True)

    if signals:
        print(f"\n[signals] {len(signals)} trade signals found:", flush=True)
        for s in signals:
            print(f"  {s['direction']:>3} {s['city']:<14} {s['target_date']} "
                  f"| edge={s['edge']:.1%} agree={s['n_agree']}/{s['n_models']} "
                  f"| P_mkt={s['p_market']:.3f} P_ens={s['p_ensemble']:.3f} "
                  f"| ${s['kelly_size']:.2f} "
                  f"| close in {s['hours_to_close']:.0f}h "
                  f"| spread={s['model_spread_c']:.1f}C",
                  flush=True)
    else:
        print("\n[signals] No trade signals this cycle.", flush=True)

    # Execute trades (paper)
    if not dry_run:
        for s in signals:
            if state["bankroll"] < s["kelly_size"]:
                continue

            state["bankroll"] -= s["kelly_size"]
            state["total_trades"] += 1

            state["positions"][s["market_id"]] = {
                "direction": s["direction"],
                "size": s["kelly_size"],
                "entry_price": s["entry_price"],
                "p_ensemble": s["p_ensemble"],
                "p_market": s["p_market"],
                "edge": s["edge"],
                "city": s["city"],
                "target_date": s["target_date"],
                "question": s["question"],
                "entered_at": now.isoformat(),
                "hours_to_close": s["hours_to_close"],
            }

            city_exposure[s["city"]] += s["kelly_size"]

            trade_record = {
                "type": "ENTRY",
                "timestamp": now.isoformat(),
                "market_id": s["market_id"],
                "question": s["question"],
                "city": s["city"],
                "target_date": s["target_date"],
                "direction": s["direction"],
                "entry_price": s["entry_price"],
                "p_market": s["p_market"],
                "p_ensemble": s["p_ensemble"],
                "edge": s["edge"],
                "size": s["kelly_size"],
                "n_agree": s["n_agree"],
                "n_models": s["n_models"],
                "ensemble_fc_c": s["ensemble_fc_c"],
                "model_spread_c": s["model_spread_c"],
                "per_model": s["per_model"],
                "hours_to_close": s["hours_to_close"],
                "bankroll_after": round(state["bankroll"], 2),
            }
            log_trade(trade_record)

            print(f"  [TRADE] {s['direction']} {s['city']} {s['target_date']} "
                  f"@ {s['entry_price']:.3f} | ${s['kelly_size']:.2f} "
                  f"| Bankroll: ${state['bankroll']:.2f}", flush=True)

    save_state(state)
    return state


# ── reporting ────────────────────────────────────────────────────────────────

def print_report():
    """Print paper trading performance report."""
    state = load_state()
    trades = load_trades()

    entries = [t for t in trades if t["type"] == "ENTRY"]
    resolves = [t for t in trades if t["type"] == "RESOLVE"]

    W = 70
    print("=" * W)
    print("  WEATHER BOT — PAPER TRADING REPORT")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * W)

    print(f"\n  Starting bankroll:  ${BANKROLL:.2f}")
    print(f"  Current bankroll:   ${state['bankroll']:.2f}")
    print(f"  Total PnL:          ${state['total_pnl']:+.2f} "
          f"({state['total_pnl']/BANKROLL*100:+.1f}%)")
    print(f"  Open positions:     {len(state['positions'])}")
    print(f"  Resolved trades:    {len(resolves)}")
    print(f"  Total entries:      {len(entries)}")

    if resolves:
        wins = sum(1 for r in resolves if r.get("won"))
        losses = len(resolves) - wins
        total_pnl = sum(r.get("pnl", 0) for r in resolves)
        avg_pnl = total_pnl / len(resolves)
        win_rate = wins / len(resolves)

        print(f"\n  Win/Loss:           {wins}W / {losses}L ({win_rate:.1%})")
        print(f"  Avg PnL/trade:      ${avg_pnl:+.2f}")

        if wins > 0:
            avg_win = sum(r["pnl"] for r in resolves if r.get("won")) / wins
            print(f"  Avg winning trade:  ${avg_win:+.2f}")
        if losses > 0:
            avg_loss = sum(r["pnl"] for r in resolves if not r.get("won")) / losses
            print(f"  Avg losing trade:   ${avg_loss:+.2f}")

        # By city
        city_pnl = defaultdict(lambda: {"pnl": 0, "n": 0, "wins": 0})
        for r in resolves:
            c = r.get("city", "unknown") if "city" in r else \
                state["positions"].get(r["market_id"], {}).get("city", "unknown")
            city_pnl[c]["pnl"] += r.get("pnl", 0)
            city_pnl[c]["n"] += 1
            if r.get("won"): city_pnl[c]["wins"] += 1

        if city_pnl:
            print(f"\n  {'City':<16} {'Trades':>6} {'WinRate':>7} {'PnL':>8}")
            print("  " + "-" * 40)
            for city in sorted(city_pnl.keys()):
                cp = city_pnl[city]
                wr = cp["wins"] / cp["n"] if cp["n"] > 0 else 0
                print(f"  {city:<16} {cp['n']:>6} {wr:>6.0%} ${cp['pnl']:>7.2f}")

    # Open positions
    if state["positions"]:
        print(f"\n  OPEN POSITIONS:")
        print(f"  {'Dir':>3} {'City':<14} {'Date':<12} {'Entry':>6} "
              f"{'Size':>7} {'Edge':>6} {'Close':>6}")
        print("  " + "-" * 58)
        for mid, pos in state["positions"].items():
            print(f"  {pos['direction']:>3} {pos['city']:<14} "
                  f"{pos['target_date']:<12} {pos['entry_price']:>6.3f} "
                  f"${pos['size']:>6.2f} {pos['edge']:>5.1%} "
                  f"{pos['hours_to_close']:>5.0f}h")

    # Recent trades
    if entries:
        print(f"\n  RECENT ENTRIES (last 10):")
        for t in entries[-10:]:
            status = ""
            resolve = next((r for r in resolves
                           if r.get("market_id") == t.get("market_id")), None)
            if resolve:
                status = f"  -> {'WIN' if resolve['won'] else 'LOSS'} ${resolve['pnl']:+.2f}"
            print(f"  {t['timestamp'][:16]} {t['direction']:>3} {t.get('city','?'):<12} "
                  f"edge={t['edge']:.1%} ${t['size']:.2f}{status}")

    print("\n" + "=" * W)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Polymarket Weather Trading Bot")
    ap.add_argument("--loop", action="store_true",
                    help="Run continuously every 5 minutes")
    ap.add_argument("--interval", type=int, default=300,
                    help="Loop interval in seconds (default: 300 = 5min)")
    ap.add_argument("--report", action="store_true",
                    help="Print paper trading report and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan and show signals but don't log trades")
    ap.add_argument("--bankroll", type=float, default=None,
                    help="Set initial bankroll (only on first run)")
    args = ap.parse_args()

    if args.report:
        print_report()
        return

    # Initialize bankroll if specified
    if args.bankroll:
        state = load_state()
        if state["total_trades"] == 0:
            state["bankroll"] = args.bankroll
            save_state(state)
            print(f"[init] Bankroll set to ${args.bankroll:.2f}", flush=True)

    if args.loop:
        print(f"[bot] Starting continuous scan (every {args.interval}s)...",
              flush=True)
        while True:
            try:
                scan_cycle(dry_run=args.dry_run)
            except KeyboardInterrupt:
                print("\n[bot] Stopped by user.", flush=True)
                break
            except Exception as e:
                print(f"\n[bot] Error in scan cycle: {e}", flush=True)
                import traceback
                traceback.print_exc()

            print(f"\n[bot] Next scan in {args.interval}s "
                  f"({datetime.now(timezone.utc).strftime('%H:%M UTC')})...",
                  flush=True)
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n[bot] Stopped by user.", flush=True)
                break
    else:
        scan_cycle(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
