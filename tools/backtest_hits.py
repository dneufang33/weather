"""Backtest: Polymarket hit-price markets (oil, gold, silver) vs a touch model fed with
(a) realized volatility of the last 20 trading days and (b) CBOE implied volatility indices.

Reads data/hits/*, writes out_hits/*.csv and prints summary tables.
"""
import gzip, json, math, os, sys
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd
from scipy.stats import norm

D, OUT = "data/hits", sys.argv[1] if len(sys.argv) > 1 else "out_hits"
os.makedirs(OUT, exist_ok=True)
SYM = {"WTI": ("CL=F", "^OVX", 1.0), "XAU": ("GC=F", "^GVZ", 1.00059), "XAG": ("SI=F", "^VXSLV", 0.99806)}
HALF_SPREAD = {"WTI": 0.01, "XAU": 0.025, "XAG": 0.02}
FEE_RATE = 0.04                         # assumed taker fee rate for this category: fee = rate * p * (1-p)
HRS_YEAR = 23 * 5 * 52

mk = pd.DataFrame(json.load(open(f"{D}/markets.json")))
pr = pd.read_csv(f"{D}/prices.csv.gz")
yh = json.load(open(f"{D}/yahoo.json"))
mk["t_end"] = pd.to_datetime(mk.end, utc=True, format="ISO8601").map(lambda d: int(d.timestamp()))
mk["t_start"] = pd.to_datetime(mk.start, utc=True, format="ISO8601").map(lambda d: int(d.timestamp()))
# month markets end 04:59 UTC (next day) -> last session closes 21/22 UTC the day before
mk.loc[pd.to_datetime(mk.end, utc=True, format="ISO8601").dt.hour < 12, "t_end"] -= 7 * 3600

# ---- trading-hour clock (CME Globex: closed 17-18 ET daily, Fri 17 ET - Sun 18 ET) ----
t0, t1 = int(mk.t_start.min()) - 86400 * 40, int(mk.t_end.max()) + 86400
grid = np.arange(t0 // 600 * 600, t1 + 600, 600)
et = pd.to_datetime(grid, unit="s", utc=True).tz_convert(ZoneInfo("America/New_York"))
wd, hr = et.weekday.values, et.hour.values
closed = (hr == 17) | ((wd == 4) & (hr >= 17)) | (wd == 5) | ((wd == 6) & (hr < 18))
cum = np.concatenate([[0], np.cumsum(~closed) / 6.0])   # trading hours up to grid point i


def th(a, b):
    ia = np.clip(((a - grid[0]) // 600).astype(int), 0, len(grid))
    ib = np.clip(((b - grid[0]) // 600).astype(int), 0, len(grid))
    return cum[ib] - cum[ia]


# ---- underlying, realized and implied variance per trading hour ----
under = {}
for A, (fut, ivx, basis) in SYM.items():
    s = pd.DataFrame(yh[fut], columns=["t", "S"]).drop_duplicates("t").sort_values("t")
    s["S"] = s.S / basis
    s["t"] = s.t + 3600                      # close known at end of bar
    s["r2"] = np.log(s.S).diff() ** 2
    s.loc[s.t.diff() > 3700, "r2"] = np.nan                 # only true 1h returns
    s["var_rv"] = s.r2.rolling(460, min_periods=300).mean()   # ~20 trading days
    v = pd.DataFrame(yh[ivx], columns=["t", "iv"]).drop_duplicates("t").sort_values("t")
    v["var_iv"] = (v.iv / 100) ** 2 / HRS_YEAR
    v["t"] = v.t + 3600
    under[A] = (s[["t", "S", "var_rv"]], v[["t", "var_iv", "iv"]])


def p_hit(S, K, up, var_h, hours):
    d = np.where(up, np.log(K / S), np.log(S / K))
    sd = np.sqrt(np.maximum(var_h * np.maximum(hours, 1e-6), 1e-12))
    p = np.minimum(1.0, 2 * norm.sf(d / sd))
    return np.where(d <= 0, 1.0, p)


rows = []
for A in SYM:
    s, v = under[A]
    m = mk[mk.asset == A]
    x = pr[pr.cid.isin(m.cid)].merge(m[["cid", "K", "dirn", "yes_won", "t_end", "t_start", "weekly", "event"]], on="cid")
    x = x[(x.t >= x.t_start) & (x.t < x.t_end)].sort_values("t")
    x = pd.merge_asof(x, s.rename(columns={"t": "t_s"}), left_on="t", right_on="t_s", direction="backward", tolerance=4 * 3600)
    x = pd.merge_asof(x.sort_values("t"), v.rename(columns={"t": "t_v"}), left_on="t", right_on="t_v",
                      direction="backward", tolerance=4 * 86400)
    x = x.dropna(subset=["S", "var_rv", "var_iv"])
    x["hours"] = th(x.t.values, x.t_end.values)
    up = (x.dirn == "HIGH").values
    x["m_rv"] = p_hit(x.S.values, x.K.values, up, x.var_rv.values, x.hours.values)
    x["m_iv"] = p_hit(x.S.values, x.K.values, up, x.var_iv.values, x.hours.values)
    x["asset"] = A
    rows.append(x)
X = pd.concat(rows, ignore_index=True)
# usable points: still open, not already touched by the model's price, uncertain, > 6 trading hours left
X = X[(X.p > 0.02) & (X.p < 0.98) & (X.m_iv < 0.999) & (X.hours > 6)].copy()
X["h_left"] = pd.cut(X.hours, [6, 24, 72, 1000], labels=["<1 Tag", "1-3 Tage", ">3 Tage"])
X["rv_ann"] = np.sqrt(X.var_rv * HRS_YEAR)

print("points", len(X), "markets", X.cid.nunique(), "events", X.event.nunique())

# ---- 1. how far apart ----
X["g_iv"] = X.m_iv - X.p
X["g_rv"] = X.m_rv - X.p
gap = X.groupby(["asset", "weekly"]).agg(n=("p", "size"), markets=("cid", "nunique"), iv=("iv", "mean"),
                                       rv=("rv_ann", lambda s: s.mean() * 100), gap_iv=("g_iv", "mean"),
                                       absgap_iv=("g_iv", lambda s: s.abs().mean()), gap_rv=("g_rv", "mean"),
                                       absgap_rv=("g_rv", lambda s: s.abs().mean()))
print("\n== Abstand Modell - Polymarket (Preis-Einheiten)\n", gap.round(3).to_string())
gap.to_csv(f"{OUT}/gap.csv")
print(X.groupby(["asset", "dirn"]).agg(gap_iv=("g_iv", "mean"), gap_rv=("g_rv", "mean"), n=("p", "size")).round(3).to_string())

# ---- 2. who predicts better (one sample per market per 6 hours to reduce overlap) ----
X["slot"] = X.t // (6 * 3600)
S = X.groupby(["cid", "slot"]).last().reset_index()
for c in ["p", "m_iv", "m_rv"]:
    S["b_" + c] = (S[c] - S.yes_won) ** 2
br = S.groupby(["asset", "h_left"], observed=True).agg(n=("p", "size"), polymarket=("b_p", "mean"),
                                                      model_iv=("b_m_iv", "mean"), model_rv=("b_m_rv", "mean"))
print("\n== Brier (kleiner = besser)\n", br.round(4).to_string())
print(S.groupby("asset").agg(polymarket=("b_p", "mean"), model_iv=("b_m_iv", "mean"), model_rv=("b_m_rv", "mean")).round(4))
br.to_csv(f"{OUT}/brier.csv")

# calibration of polymarket price
S["pb"] = pd.cut(S.p, [0, .05, .1, .2, .35, .5, .65, .8, .9, .95, 1])
cal = S.groupby(["asset", "pb"], observed=True).agg(n=("p", "size"), price=("p", "mean"), won=("yes_won", "mean"),
                                                   m_iv=("m_iv", "mean"))
print("\n== Kalibrierung Polymarket\n", cal.round(3).to_string())
cal.to_csv(f"{OUT}/calibration.csv")

# ---- 3. trading rule: first time |model_iv - p| > thr with > 24h left, hold to resolution ----
def rule(model, thr, min_h=24, min_age=0):
    Y = X[(X.hours > min_h) & (X.t - X.t_start >= min_age)].sort_values("t")
    Y = Y[(Y[model] - Y.p).abs() > thr]
    first = Y.groupby("cid").first().reset_index()
    side = np.sign(first[model] - first.p)              # +1 buy YES, -1 buy NO
    hs = first.asset.map(HALF_SPREAD)
    entry = np.where(side > 0, first.p + hs, (1 - first.p) + hs)
    payoff = np.where(side > 0, first.yes_won, 1 - first.yes_won)
    fee = FEE_RATE * entry * (1 - entry)
    first["side"], first["gross"], first["net"] = side, payoff - entry, payoff - entry - fee
    first["mid_edge"] = np.where(side > 0, first.yes_won - first.p, first.p - first.yes_won)
    return first


res = []
for model, thr, age in [(m_, t_, a_) for m_ in ["m_iv", "m_rv"] for t_ in [0.05, 0.10] for a_ in [0, 86400, 3 * 86400]]:
        f = rule(model, thr, min_age=age)
        for A, g in list(f.groupby("asset")) + [("ALLE", f)]:
            ev = g.groupby("event").net.sum()
            se = ev.std(ddof=1) * math.sqrt(len(ev)) / max(len(g), 1) if len(ev) > 1 else float("nan")
            res.append({"modell": model, "schwelle": thr, "min_alter_h": age // 3600, "asset": A, "trades": len(g), "events": g.event.nunique(),
                        "kauf_yes": int((g.side > 0).sum()), "treffer_mid_c": g.mid_edge.mean() * 100,
                        "netto_c": g.net.mean() * 100, "se_c": se * 100, "netto_summe_$100": g.net.sum() * 100})
R = pd.DataFrame(res)
print("\n== Handelsregel (je Trade 1 Share, gehalten bis Aufloesung, Cent pro Share)\n", R.round(2).to_string())
R.to_csv(f"{OUT}/rule.csv", index=False)
f = rule("m_iv", 0.05)
print("\n== Regel IV 5c nach Richtung/Typ\n", f.groupby(["asset", "dirn", "side"]).agg(n=("net", "size"), netto_c=("net", lambda s: s.mean() * 100)).round(2).to_string())
print(f.groupby(["asset", "weekly"]).agg(n=("net", "size"), netto_c=("net", lambda s: s.mean() * 100)).round(2).to_string())
f["month"] = pd.to_datetime(f.t, unit="s").dt.strftime("%Y-%m")
print(f.groupby(["asset", "month"]).agg(n=("net", "size"), netto_c=("net", lambda s: s.mean() * 100)).round(1).unstack(0).to_string())
f.to_csv(f"{OUT}/trades_iv5.csv", index=False)

# ---- robustness ----
print("\n==== ROBUSTHEIT (Einstieg fruehestens 24h nach Marktstart) ====")
def summarize(f, label):
    f = f.copy(); f["month"] = pd.to_datetime(f.t, unit="s").dt.strftime("%Y-%m")
    mo = f.groupby("month").net.mean()
    print(f"{label:38s} trades {len(f):5d}  netto {f.net.mean()*100:6.2f} c  Monate im Plus {int((mo>0).sum())}/{len(mo)}  "
          f"Kauf-YES {int((f.side>0).sum())}  netto Kauf-YES {f[f.side>0].net.mean()*100:6.2f} c  Verkauf {f[f.side<0].net.mean()*100:6.2f} c")
    return f
for hs_mult, lab in [(1, "Spread wie gemessen"), (2, "doppelter Spread")]:
    for thr in (0.05, 0.10):
        HALF_SPREAD_OLD = dict(HALF_SPREAD)
        for k in HALF_SPREAD: HALF_SPREAD[k] = HALF_SPREAD_OLD[k] * hs_mult
        f = rule("m_iv", thr, min_age=86400)
        summarize(f, f"IV-Modell, Schwelle {int(thr*100)}c, {lab}")
        for A, g in f.groupby("asset"): summarize(g, f"   {A}")
        HALF_SPREAD.update(HALF_SPREAD_OLD)
# naive benchmark: no model, always sell YES (buy NO) the first time 0.10 < p < 0.90 after 24h
Y = X[(X.hours > 24) & (X.t - X.t_start >= 86400) & (X.p > 0.10) & (X.p < 0.90)].sort_values("t")
fn = Y.groupby("cid").first().reset_index()
hs = fn.asset.map(HALF_SPREAD); entry = (1 - fn.p) + hs
fn["side"] = -1; fn["net"] = (1 - fn.yes_won) - entry - FEE_RATE * entry * (1 - entry)
summarize(fn, "OHNE Modell: immer YES verkaufen")
for A, g in fn.groupby("asset"): summarize(g, f"   {A}")
for (A, dn), g in fn.groupby(["asset", "dirn"]): summarize(g, f"   {A} {dn}")
# does the model add anything beyond 'sell'? within sells, model-flagged vs not
f = rule("m_iv", 0.05, min_age=86400)
j = fn.merge(f[["cid", "side"]].rename(columns={"side": "model_side"}), on="cid", how="left")
for lab, g in [("Modell sagt verkaufen", j[j.model_side < 0]), ("Modell sagt kaufen", j[j.model_side > 0]), ("Modell neutral", j[j.model_side.isna()])]:
    print(f"   naiver Verkauf, {lab:22s} n={len(g):4d} netto {g.net.mean()*100:6.2f} c")

print("\n==== Verlauf und Kapazitaet (IV-Modell, 10c, ab 24h) ====")
f = rule("m_iv", 0.10, min_age=86400); f["month"] = pd.to_datetime(f.t, unit="s").dt.strftime("%Y-%m")
mo = f.groupby(["asset", "month"]).agg(n=("net", "size"), cent=("net", lambda s: s.mean() * 100), summe_1share=("net", "sum")).round(2)
print(mo.unstack(0).to_string())
f["hold_days"] = (f.t_end - f.t) / 86400
print(f.groupby("asset").agg(hold_days=("hold_days", "median"), entry_p=("p", "median"), win_rate=("net", lambda s: (s > 0).mean()),
                             worst=("net", "min"), best=("net", "max")).round(2).to_string())
f.to_csv(f"{OUT}/trades_iv10_age24.csv", index=False)
