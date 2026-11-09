"""Skill classification + trading-pattern analysis for Polymarket daily temperature markets.

Reads data/events.csv, data/markets.csv, data/trades/*.csv.gz
Writes out/*.json, out/*.csv with all tables used in the report.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

D = sys.argv[1] if len(sys.argv) > 1 else "data"
OUT = sys.argv[2] if len(sys.argv) > 2 else "out"
os.makedirs(OUT, exist_ok=True)
rng = np.random.default_rng(7)
FEE_RATE = 0.05  # weather taker feeRate; fee = C * rate * p * (1-p); makers pay 0

TZ = {
    "amsterdam": "Europe/Amsterdam", "ankara": "Europe/Istanbul", "atlanta": "America/New_York",
    "austin": "America/Chicago", "beijing": "Asia/Shanghai", "buenos-aires": "America/Argentina/Buenos_Aires",
    "busan": "Asia/Seoul", "cape-town": "Africa/Johannesburg", "chengdu": "Asia/Shanghai",
    "chicago": "America/Chicago", "chongqing": "Asia/Shanghai", "dallas": "America/Chicago",
    "denver": "America/Denver", "guangzhou": "Asia/Shanghai", "helsinki": "Europe/Helsinki",
    "houston": "America/Chicago", "istanbul": "Europe/Istanbul", "jeddah": "Asia/Riyadh",
    "karachi": "Asia/Karachi", "kuala-lumpur": "Asia/Kuala_Lumpur", "los-angeles": "America/Los_Angeles",
    "lucknow": "Asia/Kolkata", "manila": "Asia/Manila", "mexico-city": "America/Mexico_City",
    "miami": "America/New_York", "moscow": "Europe/Moscow", "nyc": "America/New_York",
    "panama": "America/Panama", "qingdao": "Asia/Shanghai", "san-francisco": "America/Los_Angeles",
    "sao-paulo": "America/Sao_Paulo", "seattle": "America/Los_Angeles", "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "taipei": "Asia/Taipei", "tokyo": "Asia/Tokyo", "toronto": "America/Toronto",
    "wellington": "Pacific/Auckland", "wuhan": "Asia/Shanghai", "london": "Europe/London",
    "paris": "Europe/Paris", "madrid": "Europe/Madrid", "berlin": "Europe/Berlin", "munich": "Europe/Berlin",
    "milan": "Europe/Rome", "rome": "Europe/Rome", "warsaw": "Europe/Warsaw", "lagos": "Africa/Lagos",
    "tel-aviv": "Asia/Jerusalem", "hong-kong": "Asia/Hong_Kong", "dubai": "Asia/Dubai",
    "delhi": "Asia/Kolkata", "new-delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "phoenix": "America/Phoenix", "boston": "America/New_York", "washington": "America/New_York",
    "philadelphia": "America/New_York", "las-vegas": "America/Los_Angeles", "vancouver": "America/Vancouver",
    "montreal": "America/Toronto", "lima": "America/Lima", "bogota": "America/Bogota", "santiago": "America/Santiago",
    "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi", "riyadh": "Asia/Riyadh", "tehran": "Asia/Tehran",
    "hanoi": "Asia/Bangkok", "ho-chi-minh-city": "Asia/Ho_Chi_Minh", "osaka": "Asia/Tokyo",
    "dhaka": "Asia/Dhaka", "kolkata": "Asia/Kolkata", "chennai": "Asia/Kolkata", "hangzhou": "Asia/Shanghai",
    "nanjing": "Asia/Shanghai", "xian": "Asia/Shanghai", "tianjin": "Asia/Shanghai", "kyiv": "Europe/Kyiv",
    "athens": "Europe/Athens", "lisbon": "Europe/Lisbon", "dublin": "Europe/Dublin", "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo", "copenhagen": "Europe/Copenhagen", "vienna": "Europe/Vienna", "zurich": "Europe/Zurich",
    "brussels": "Europe/Brussels", "prague": "Europe/Prague", "budapest": "Europe/Budapest",
}

# ---------------- load ----------------
ev = pd.read_csv(f"{D}/events.csv", dtype={"event_id": str})
mk = pd.read_csv(f"{D}/markets.csv", dtype={"event_id": str})
files = sorted(glob.glob(f"{D}/trades/*.csv.gz"))
t = pd.concat([pd.read_csv(f, dtype={"event_id": str}) for f in files], ignore_index=True)
print("rows", len(t), "files", len(files))

ev["city"] = ev["series"].str.replace("-daily-weather", "", regex=False)
unk = sorted(set(ev.city) - set(TZ))
if unk:
    print("WARN unmapped tz:", unk)
ev = ev[ev.clean_resolution == 1]


def eod_utc(r):
    tz = ZoneInfo(TZ.get(r.city, "UTC"))
    d = pd.Timestamp(r.event_date).to_pydatetime().replace(tzinfo=tz)
    return int((d + pd.Timedelta(days=1)).timestamp())


def sod_utc(r):
    tz = ZoneInfo(TZ.get(r.city, "UTC"))
    d = pd.Timestamp(r.event_date).to_pydatetime().replace(tzinfo=tz)
    return int(d.timestamp())


ev["eod"] = ev.apply(eod_utc, axis=1)          # end of local observation day
ev["sod"] = ev.apply(sod_utc, axis=1)          # start of local observation day
ev["open_ts"] = pd.to_datetime(ev.start, utc=True).astype("int64") // 10**9
ev["close_ts"] = pd.to_datetime(ev.closed_time, utc=True, errors="coerce").astype("int64") // 10**9

mk["c14"] = mk.condition_id.str[:14]
t = t.merge(mk[["c14", "yes_won", "bracket"]], left_on="condition_id", right_on="c14", how="inner")
t = t.merge(ev[["event_id", "city", "event_date", "eod", "sod", "open_ts", "close_ts"]], on="event_id", how="inner")

t["win_tok"] = (((t.outcome_index == 0) & (t.yes_won == 1)) | ((t.outcome_index == 1) & (t.yes_won == 0))).astype(float)
t["notional"] = t.price * t["size"]
t["gross"] = t.side * (t.win_tok - t.price) * t["size"]
t["fee"] = np.where(t.taker == 1, t["size"] * FEE_RATE * t.price * (1 - t.price), 0.0)
t["pnl"] = t.gross - t.fee
t["h_eod"] = (t.eod - t.ts) / 3600.0          # hours before end of local observation day
t["h_sod"] = (t.sod - t.ts) / 3600.0
t["life"] = ((t.ts - t.open_ts) / (t.eod - t.open_ts)).clip(0, 1.2)
t["hour_utc"] = (t.ts % 86400) // 3600
t["local_hour"] = np.floor(24 - t.h_eod)  # local clock hour on observation day (0..23 if on obs day)

print("check zero-sum gross:", round(t.gross.sum(), 2), "notional", round(t.notional.sum()))


def hb(h):
    return pd.cut(h, [-1e9, 0, 6, 12, 18, 24, 36, 48, 72, 1e9],
                  labels=["nach Tagesende", "0-6h", "6-12h", "12-18h", "18-24h", "24-36h", "36-48h", "48-72h", ">72h"])


t["hbin"] = hb(t.h_eod)
t["pbin"] = pd.cut(t.price, [0, .02, .05, .10, .20, .35, .50, .65, .80, .90, .95, .98, 1.0001],
                   labels=["<2c", "2-5c", "5-10c", "10-20c", "20-35c", "35-50c", "50-65c", "65-80c", "80-90c",
                           "90-95c", "95-98c", ">98c"], include_lowest=True)
t["act"] = np.select([(t.side == 1) & (t.outcome_index == 0), (t.side == 1) & (t.outcome_index == 1),
                      (t.side == -1) & (t.outcome_index == 0)], ["Buy YES", "Buy NO", "Sell YES"], "Sell NO")

# ---------------- split ----------------
dates = sorted(ev.event_date.unique())
mid = dates[len(dates) // 2]
t["half"] = np.where(t.event_date < mid, "A", "B")
print("dates", dates[0], "->", dates[-1], "split at", mid)

# ---------------- sign randomization ----------------
N_SIM = 5000


def sign_test(df, min_events):
    we = df.groupby(["wallet", "event_id"]).pnl.sum().reset_index()
    cnt = we.groupby("wallet").size()
    keep = cnt[cnt >= min_events].index
    we = we[we.wallet.isin(keep)]
    res = {}
    for w, g in we.groupby("wallet"):
        x = g.pnl.values
        obs = x.sum()
        n = len(x)
        ge = 0
        done = 0
        while done < N_SIM:
            b = min(N_SIM - done, max(1, 2_000_000 // n))
            s = rng.integers(0, 2, size=(b, n), dtype=np.int8) * 2 - 1
            ge += int(((s * x).sum(axis=1) >= obs - 1e-9).sum())
            done += b
        res[w] = ((ge + 1) / (N_SIM + 1), obs, n)
    return pd.DataFrame.from_dict(res, orient="index", columns=["p", "pnl", "n_events"])


MIN_EV = 20
full = sign_test(t, MIN_EV)
print("tested wallets", len(full))


def cls(r):
    if r.p < 0.05:
        return "skilled_winner"
    if r.p > 0.95:
        return "skilled_loser"
    return "lucky_winner" if r.pnl > 0 else "lucky_loser"


full["cls"] = full.apply(cls, axis=1)
# Benjamini-Hochberg
ps = full.p.sort_values()
m_ = len(ps)
bh = ps.values <= (np.arange(1, m_ + 1) / m_) * 0.10
cut = ps.values[bh].max() if bh.any() else -1
full["bh10"] = full.p <= cut

# Out-of-sample persistence
A = sign_test(t[t.half == "A"], 10)
B = sign_test(t[t.half == "B"], 10)
both = A.join(B, lsuffix="_A", rsuffix="_B", how="inner")
both["skA"] = both.p_A < 0.05
persist = {
    "wallets_in_both_halves": int(len(both)),
    "skilled_in_A": int(both.skA.sum()),
    "of_those_skilled_in_B": int((both.skA & (both.p_B < 0.05)).sum()),
    "of_those_profitable_in_B": int((both.skA & (both.pnl_B > 0)).sum()),
    "base_rate_skilled_B": float((both.p_B < 0.05).mean()),
    "base_rate_profitable_B": float((both.pnl_B > 0).mean()),
    "pnl_B_of_A_skilled": float(both[both.skA].pnl_B.sum()),
    "pnl_B_of_A_lucky_winners": float(both[(~both.skA) & (both.pnl_A > 0)].pnl_B.sum()),
    "n_A_lucky_winners": int(((~both.skA) & (both.pnl_A > 0)).sum()),
    "lucky_winners_A_profitable_B": int(((~both.skA) & (both.pnl_A > 0) & (both.pnl_B > 0)).sum()),
}
full = full.join(both[["pnl_A", "pnl_B", "p_A", "p_B"]], how="left")
full["core"] = (full.cls == "skilled_winner") & (full.pnl_A > 0) & (full.pnl_B > 0)

# ---------------- wallet features ----------------
t["grp"] = t.wallet.map(full.cls).fillna("untested")
t.loc[t.wallet.isin(full[full.core].index), "grp2"] = "core"
g = t.groupby("wallet")
wf = pd.DataFrame({
    "notional": g.notional.sum(), "gross": g.gross.sum(), "fees": g.fee.sum(), "pnl": g.pnl.sum(),
    "fills": g.size(), "events": g.event_id.nunique(), "cities": g.city.nunique(),
    "days_active": g.event_date.nunique(),
    "maker_share": 1 - (t.notional * t.taker).groupby(t.wallet).sum() / g.notional.sum(),
    "med_fill_usd": g.notional.median(),
    "med_h_eod": g.h_eod.median(),
    "first_ts": g.ts.min(), "last_ts": g.ts.max(),
})
wf["roi"] = wf.pnl / wf.notional
wf = wf.join(full, rsuffix="_test")
wf["grp"] = wf.cls.fillna("untested")
wf.loc[wf.core == True, "grp"] = "core_skilled"
wf.to_csv(f"{OUT}/wallets.csv")

GROUPS = ["core_skilled", "skilled_winner", "lucky_winner", "lucky_loser", "skilled_loser", "untested"]
t["G"] = t.wallet.map(wf.grp)


def by(col, extra=None):
    rows = []
    for gname in GROUPS:
        s = t[t.G == gname] if gname != "skilled_winner" else t[t.G.isin(["skilled_winner", "core_skilled"])]
        if not len(s):
            continue
        a = s.groupby(col, observed=True).agg(notional=("notional", "sum"), pnl=("pnl", "sum"),
                                             gross=("gross", "sum"), fills=("notional", "size"))
        a["share"] = a.notional / a.notional.sum()
        a["roi"] = a.pnl / a.notional
        a["group"] = gname
        rows.append(a.reset_index())
    return pd.concat(rows)


tables = {}
for col in ["hbin", "pbin", "act", "taker", "hour_utc", "half"]:
    tables[col] = by(col)
    tables[col].to_csv(f"{OUT}/by_{col}.csv", index=False)

# market-wide structural edge: taker flow by horizon x price
tk = t[t.taker == 1]
grid = tk.groupby(["hbin", "pbin"], observed=True).agg(notional=("notional", "sum"), gross=("gross", "sum"),
                                                       pnl=("pnl", "sum"), fills=("notional", "size")).reset_index()
grid["gross_roi"] = grid.gross / grid.notional
grid["net_roi"] = grid.pnl / grid.notional
grid.to_csv(f"{OUT}/taker_grid.csv", index=False)

# calibration: realized win rate of token vs price (all buys, taker side)
buys = t[(t.side == 1)]
cal = buys.groupby("pbin", observed=True).apply(
    lambda s: pd.Series({"avg_price": np.average(s.price, weights=s["size"]),
                         "win_rate": np.average(s.win_tok, weights=s["size"]), "shares": s["size"].sum()}))
cal.to_csv(f"{OUT}/calibration.csv")
calh = buys.groupby(["hbin", "pbin"], observed=True).apply(
    lambda s: pd.Series({"avg_price": np.average(s.price, weights=s["size"]),
                         "win_rate": np.average(s.win_tok, weights=s["size"]), "shares": s["size"].sum()})).reset_index()
calh.to_csv(f"{OUT}/calibration_by_h.csv", index=False)

# city table
city = t.groupby("city").agg(notional=("notional", "sum"), events=("event_id", "nunique")).reset_index()
skc = t[t.G.isin(["skilled_winner", "core_skilled"])].groupby("city").agg(sk_notional=("notional", "sum"),
                                                                          sk_pnl=("pnl", "sum")).reset_index()
city = city.merge(skc, on="city", how="left")
city["sk_share"] = city.sk_notional / city.notional
city["sk_roi"] = city.sk_pnl / city.sk_notional
mkp = t[t.taker == 1].groupby("city").agg(tk_gross=("gross", "sum"), tk_not=("notional", "sum")).reset_index()
city = city.merge(mkp, on="city")
city["taker_gross_roi"] = city.tk_gross / city.tk_not
city["avg_daily_volume"] = city.notional / city.events
city.sort_values("notional", ascending=False).to_csv(f"{OUT}/city.csv", index=False)

# within-event bracket rank: favorite at the time? use price rank proxy by distance to winning bracket
mk2 = mk.copy()
mk2["order"] = mk2.groupby("event_id").cumcount()
win_idx = mk2[mk2.yes_won == 1].set_index("event_id").order
t = t.merge(mk2[["c14", "order"]], left_on="condition_id", right_on="c14", how="left", suffixes=("", "_m"))
t["dist"] = (t["order"] - t.event_id.map(win_idx)).abs()
dist = by("dist")
dist = dist[dist.dist <= 6]
dist.to_csv(f"{OUT}/by_dist.csv", index=False)

# group summary
summ = wf.groupby("grp").agg(wallets=("pnl", "size"), pnl=("pnl", "sum"), gross=("gross", "sum"), fees=("fees", "sum"),
                             notional=("notional", "sum"), med_events=("events", "median"),
                             med_cities=("cities", "median"), med_maker_share=("maker_share", "median"),
                             med_fill=("med_fill_usd", "median"), med_h=("med_h_eod", "median"),
                             med_roi=("roi", "median")).reindex(GROUPS)
summ["roi"] = summ.pnl / summ.notional
summ.to_csv(f"{OUT}/groups.csv")

# concentration and top wallets
sk = wf[wf.grp.isin(["core_skilled", "skilled_winner"])].sort_values("pnl", ascending=False)
top = sk.head(25)[["pnl", "gross", "fees", "notional", "roi", "events", "cities", "maker_share", "med_fill_usd",
                   "med_h_eod", "p", "pnl_A", "pnl_B", "grp"]]
top.to_csv(f"{OUT}/top_skilled.csv")

# top-PnL overlap with skilled
toppnl = wf.sort_values("pnl", ascending=False).head(100)
lb = {"top100_pnl_skilled_share": float(toppnl.grp.isin(["core_skilled", "skilled_winner"]).mean()),
      "top100_pnl_untested_share": float((toppnl.grp == "untested").mean())}

# per-wallet behavior archetypes among skilled: dominant horizon and price regime
def arche(w):
    s = t[t.wallet == w]
    n = s.notional.sum()
    late = s[s.h_eod < 6].notional.sum() / n
    post = s[s.h_eod < 0].notional.sum() / n
    early = s[s.h_eod > 24].notional.sum() / n
    hi = s[s.price >= 0.9].notional.sum() / n
    lo = s[s.price <= 0.1].notional.sum() / n
    maker = 1 - s[s.taker == 1].notional.sum() / n
    sells = s[s.side == -1].notional.sum() / n
    return dict(early=early, late=late, post=post, hi_price=hi, lo_price=lo, maker=maker, sells=sells)


ar = pd.DataFrame({w: arche(w) for w in sk.index}).T
ar = ar.join(sk[["pnl", "roi", "notional", "events", "cities"]])


def label(r):
    if r.maker > 0.6:
        return "Market Maker (Limit Orders)"
    if r.post + (r.late if r.hi_price > 0.5 else 0) > 0.5 and r.hi_price > 0.5:
        return "Late Sweeper (fast sichere Brackets am Ende)"
    if r.early > 0.5:
        return "Frueh-Positionierer (>24h vorher, Modell-Edge)"
    if r.late > 0.5:
        return "Intraday-Trader (am Beobachtungstag)"
    return "Gemischt"


ar["archetype"] = ar.apply(label, axis=1)
ar.to_csv(f"{OUT}/skilled_archetypes.csv")
arch = ar.groupby("archetype").agg(wallets=("pnl", "size"), pnl=("pnl", "sum"), notional=("notional", "sum"),
                                   med_roi=("roi", "median"))
arch["roi"] = arch.pnl / arch.notional
arch.to_csv(f"{OUT}/archetype_summary.csv")

meta = {
    "rows": int(len(t)), "events": int(t.event_id.nunique()), "cities": int(t.city.nunique()),
    "wallets": int(t.wallet.nunique()), "date_from": dates[0], "date_to": dates[-1], "split": mid,
    "total_notional": float(t.notional.sum()), "total_fees_est": float(t.fee.sum()),
    "tested_wallets": int(len(full)), "class_counts": full.cls.value_counts().to_dict(),
    "bh10_count": int(full.bh10.sum()), "core_count": int(full.core.sum()),
    "persistence": persist, "leaderboard": lb,
    "skilled_pnl_share_of_all_positive_pnl": float(wf[wf.grp.isin(["core_skilled", "skilled_winner"])].pnl.sum()
                                                    / wf[wf.pnl > 0].pnl.sum()),
    "top10_skilled_share_of_skilled_pnl": float(sk.head(10).pnl.sum() / sk.pnl.sum()) if len(sk) else None,
    "maker_total_pnl": float(t[t.taker == 0].pnl.sum()), "taker_total_pnl": float(t[t.taker == 1].pnl.sum()),
}
json.dump(meta, open(f"{OUT}/meta.json", "w"), indent=1, default=float)
print(json.dumps(meta, indent=1, default=float))
