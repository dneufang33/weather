"""Skill classification + trading-pattern analysis on the DuckDB table built by build_db.py."""
import json, os, sys, time
from math import erf, sqrt
import duckdb, numpy as np, pandas as pd

DB = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/w.duckdb"
OUT = sys.argv[2] if len(sys.argv) > 2 else "out"
os.makedirs(OUT, exist_ok=True)
T0 = time.time()
con = duckdb.connect(DB)
con.execute("PRAGMA memory_limit='5GB'; PRAGMA threads=2;")
rng = np.random.default_rng(7)
q = lambda s: con.execute(s).df()
log = lambda *a: print(f"[{time.time()-T0:6.0f}s]", *a, flush=True)

dates = q("SELECT DISTINCT event_date FROM t ORDER BY 1").event_date.tolist()
MID = dates[len(dates) // 2]
log("dates", dates[0], dates[-1], "split", MID)

# ---------- wallet x event pnl ----------
we = q(f"""SELECT wallet, event_id, event_date < '{MID}' AS in_a, sum(gross) AS pnl, sum(pnl) AS net
           FROM t GROUP BY ALL""")
log("wallet-events", len(we))

N_SIM = 2000


def sign_test(df, min_events):
    cnt = df.groupby("wallet").size()
    df = df[df.wallet.isin(cnt[cnt >= min_events].index)]
    out = {}
    for w, x in df.groupby("wallet").pnl:
        x = x.values
        n = len(x)
        obs = x.sum()
        ge, done = 0, 0
        while done < N_SIM:
            b = min(N_SIM - done, max(1, 4_000_000 // n))
            s = rng.integers(0, 2, size=(b, n), dtype=np.int8).astype(np.float32) * 2 - 1
            ge += int((s @ x.astype(np.float32) >= obs - 1e-6).sum())
            done += b
        out[w] = ((ge + 1) / (N_SIM + 1), obs, n)
    r = pd.DataFrame.from_dict(out, orient="index", columns=["p", "gross_t", "n_events"])
    r["pnl"] = df.groupby("wallet").net.sum().reindex(r.index)
    return r


MIN_EV = 20
full = sign_test(we, MIN_EV)
log("full tested", len(full))
A = sign_test(we[we.in_a], 10)
B = sign_test(we[~we.in_a], 10)
log("halves tested", len(A), len(B))


def cls(r):
    if r.p < 0.05 and r.pnl > 0:
        return "skilled_winner"
    if r.p > 0.95:
        return "skilled_loser"
    return "lucky_winner" if r.pnl > 0 else "lucky_loser"


full["cls"] = full.apply(cls, axis=1)
ps = np.sort(full.p.values)
m_ = len(ps)
ok = ps <= np.arange(1, m_ + 1) / m_ * 0.10
full["bh10"] = full.p <= (ps[ok].max() if ok.any() else -1)

# calibration-null robustness at wallet x token x event net position
cz = q("""
WITH pos AS (
  SELECT wallet, event_id, condition_id, oi, sum(side*size) AS qn, sum(side*size*price) AS c,
         any_value(win_tok) AS win, sum(fee) AS fee
  FROM t GROUP BY ALL)
SELECT wallet, sum(qn*win - c - fee) AS g,
       sum(CASE WHEN abs(qn)>1e-9 THEN qn*qn*greatest(least(c/qn,0.999),0.001)*(1-greatest(least(c/qn,0.999),0.001)) ELSE 0 END) AS v
FROM pos GROUP BY wallet""").set_index("wallet")
cz["z_cal"] = cz.g / np.sqrt(cz.v.clip(lower=1e-9))
cz["p_cal"] = cz.z_cal.apply(lambda z: 0.5 * (1 - erf(z / sqrt(2))))
full = full.join(cz[["z_cal", "p_cal"]])

both = A.join(B, lsuffix="_A", rsuffix="_B", how="inner")
both["skA"] = both.p_A < 0.05
luckyA = (~both.skA) & (both.pnl_A > 0)
persist = {
    "wallets_in_both_halves": int(len(both)), "skilled_in_A": int(both.skA.sum()),
    "skilledA_skilled_in_B": int((both.skA & (both.p_B < 0.05)).sum()),
    "skilledA_profitable_in_B": int((both.skA & (both.pnl_B > 0)).sum()),
    "base_rate_skilled_B": float((both.p_B < 0.05).mean()),
    "base_rate_profitable_B": float((both.pnl_B > 0).mean()),
    "pnl_B_of_skilledA": float(both[both.skA].pnl_B.sum()),
    "pnl_A_of_skilledA": float(both[both.skA].pnl_A.sum()),
    "luckyA_n": int(luckyA.sum()), "luckyA_profitable_in_B": int((luckyA & (both.pnl_B > 0)).sum()),
    "pnl_B_of_luckyA": float(both[luckyA].pnl_B.sum()),
    "skilled_loserA_n": int((both.p_A > 0.95).sum()),
    "skilled_loserA_losing_in_B": int(((both.p_A > 0.95) & (both.pnl_B < 0)).sum()),
}
both.to_csv(f"{OUT}/persistence_wallets.csv")
# decile persistence
both["decA"] = pd.qcut(both.pnl_A.rank(method="first"), 10, labels=False)
dec = both.groupby("decA").agg(n=("pnl_B", "size"), mean_pnl_A=("pnl_A", "mean"), mean_pnl_B=("pnl_B", "mean"),
                               share_prof_B=("pnl_B", lambda s: (s > 0).mean()))
dec.to_csv(f"{OUT}/persistence_deciles.csv")
log("persistence", persist)

full = full.join(both[["pnl_A", "pnl_B", "p_A", "p_B"]], how="left")
full["core"] = (full.cls == "skilled_winner") & (full.pnl_A > 0) & (full.pnl_B > 0) & (full.p_cal < 0.05)

# ---------- wallet table + groups ----------
wf = q("""SELECT wallet, sum(notional) notional, sum(gross) gross, sum(fee) fees, sum(pnl) pnl, count(*) fills,
          count(DISTINCT event_id) events, count(DISTINCT city) cities, count(DISTINCT event_date) n_days,
          1 - sum(notional*taker)/sum(notional) maker_share, median(notional) med_fill,
          sum(notional*h_eod)/sum(notional) avg_h, median(h_eod) med_h,
          sum(CASE WHEN side=-1 THEN notional ELSE 0 END)/sum(notional) sell_share
          FROM t GROUP BY wallet""").set_index("wallet")
wf["roi"] = wf.pnl / wf.notional
wf = wf.join(full.drop(columns=["pnl"]))
wf["grp"] = wf.cls.fillna("untested")
wf.loc[wf.core == True, "grp"] = "core_skilled"
wf.to_csv(f"{OUT}/wallets.csv")
con.register("wg", wf.reset_index()[["wallet", "grp"]])
con.execute("CREATE OR REPLACE TABLE wg_t AS SELECT * FROM wg")
con.execute("""CREATE OR REPLACE VIEW tg AS
  SELECT t.*, CASE WHEN w.grp IN ('core_skilled','skilled_winner') THEN 'skilled_all' ELSE w.grp END AS g2, w.grp
  FROM t JOIN wg_t w USING(wallet)""")
GR = ["core_skilled", "skilled_winner", "lucky_winner", "lucky_loser", "skilled_loser", "untested"]

summ = wf.groupby("grp").agg(wallets=("pnl", "size"), pnl=("pnl", "sum"), gross=("gross", "sum"), fees=("fees", "sum"),
                             notional=("notional", "sum"), med_events=("events", "median"),
                             med_cities=("cities", "median"), med_days=("n_days", "median"),
                             med_maker_share=("maker_share", "median"), med_fill=("med_fill", "median"),
                             med_avg_h=("avg_h", "median"), med_roi=("roi", "median"),
                             share_profitable=("pnl", lambda s: (s > 0).mean())).reindex(GR)
summ["roi"] = summ.pnl / summ.notional
summ.to_csv(f"{OUT}/groups.csv")
log("groups\n", summ[["wallets", "pnl", "notional", "roi", "med_maker_share", "med_avg_h"]])

HB = """CASE WHEN h_eod<0 THEN 'a_nach Tagesende' WHEN h_eod<6 THEN 'b_0-6h' WHEN h_eod<12 THEN 'c_6-12h'
   WHEN h_eod<18 THEN 'd_12-18h' WHEN h_eod<24 THEN 'e_18-24h' WHEN h_eod<36 THEN 'f_24-36h'
   WHEN h_eod<48 THEN 'g_36-48h' WHEN h_eod<72 THEN 'h_48-72h' ELSE 'i_>72h' END"""
PB = """CASE WHEN price<0.02 THEN 'a_<2c' WHEN price<0.05 THEN 'b_2-5c' WHEN price<0.10 THEN 'c_5-10c'
   WHEN price<0.20 THEN 'd_10-20c' WHEN price<0.35 THEN 'e_20-35c' WHEN price<0.50 THEN 'f_35-50c'
   WHEN price<0.65 THEN 'g_50-65c' WHEN price<0.80 THEN 'h_65-80c' WHEN price<0.90 THEN 'i_80-90c'
   WHEN price<0.95 THEN 'j_90-95c' WHEN price<0.98 THEN 'k_95-98c' ELSE 'l_>98c' END"""


def by(expr, name, extra_where="1=1"):
    df = q(f"""SELECT g2 AS grp, {expr} AS k, sum(notional) notional, sum(pnl) pnl, sum(gross) gross, count(*) fills
               FROM tg WHERE {extra_where} GROUP BY ALL
               UNION ALL
               SELECT grp, {expr}, sum(notional), sum(pnl), sum(gross), count(*) FROM tg
               WHERE grp='core_skilled' AND {extra_where} GROUP BY ALL
               UNION ALL
               SELECT 'ALL', {expr}, sum(notional), sum(pnl), sum(gross), count(*) FROM tg WHERE {extra_where} GROUP BY ALL""")
    df["share"] = df.notional / df.groupby("grp").notional.transform("sum")
    df["roi"] = df.pnl / df.notional
    df["gross_roi"] = df.gross / df.notional
    df.sort_values(["grp", "k"]).to_csv(f"{OUT}/by_{name}.csv", index=False)
    return df


by(HB, "horizon"); by(PB, "price"); by("act", "action"); by("taker", "taker"); by("hour_utc", "hour_utc")
by("least(dist,6)", "dist")
by(f"{HB} || ' | ' || CASE WHEN taker=1 THEN 'taker' ELSE 'maker' END", "horizon_role")
by(f"{HB} || ' | ' || {PB}", "horizon_price")
by("CASE WHEN h_eod BETWEEN 0 AND 24 THEN (24 - ceil(h_eod))::INT ELSE -1 END", "local_hour_obsday")
by("CASE WHEN notional<10 THEN 'a_<$10' WHEN notional<50 THEN 'b_10-50' WHEN notional<200 THEN 'c_50-200' WHEN notional<1000 THEN 'd_200-1k' ELSE 'e_>1k' END", "ticket")
log("group tables done")

# weekly consistency
wk = q("""SELECT wallet, date_trunc('week', event_date::DATE) wk, sum(pnl) pnl FROM tg
          WHERE grp IN ('core_skilled','skilled_winner','lucky_winner') GROUP BY ALL""")
wk = wk.merge(wf[["grp"]].reset_index(), on="wallet")
cons = wk.groupby(["grp", "wallet"]).pnl.apply(lambda s: (s > 0).mean()).groupby("grp").describe()
cons.to_csv(f"{OUT}/weekly_consistency.csv")
grpweek = q("""SELECT g2, date_trunc('week', event_date::DATE) wk, sum(pnl) pnl, sum(notional) notional
               FROM tg GROUP BY ALL ORDER BY 1,2""")
grpweek.to_csv(f"{OUT}/group_week.csv", index=False)

# market-wide structural edge (taker flow) and calibration
grid = q(f"""SELECT {HB} hb, {PB} pb, sum(notional) notional, sum(gross) gross, sum(pnl) pnl, count(*) fills
             FROM t WHERE taker=1 GROUP BY ALL""")
grid["gross_roi"] = grid.gross / grid.notional
grid["net_roi"] = grid.pnl / grid.notional
grid.sort_values(["hb", "pb"]).to_csv(f"{OUT}/taker_grid.csv", index=False)
cal = q(f"""SELECT {HB} hb, {PB} pb, sum(price*size)/sum(size) avg_price, sum(win_tok*size)/sum(size) win_rate,
            sum(size) shares, count(*) n FROM t WHERE side=1 GROUP BY ALL""")
cal["gap"] = cal.win_rate - cal.avg_price
cal.sort_values(["hb", "pb"]).to_csv(f"{OUT}/calibration_h_p.csv", index=False)
cal0 = q(f"""SELECT {PB} pb, sum(price*size)/sum(size) avg_price, sum(win_tok*size)/sum(size) win_rate, sum(size) shares
             FROM t WHERE side=1 GROUP BY ALL ORDER BY 1""")
cal0["gap"] = cal0.win_rate - cal0.avg_price
cal0.to_csv(f"{OUT}/calibration.csv", index=False)

# cities
city = q("""SELECT city, sum(notional) notional, count(DISTINCT event_id) events,
            sum(CASE WHEN g2='skilled_all' THEN notional END) sk_notional,
            sum(CASE WHEN g2='skilled_all' THEN pnl END) sk_pnl,
            sum(CASE WHEN taker=1 THEN gross END)/sum(CASE WHEN taker=1 THEN notional END) taker_gross_roi,
            sum(CASE WHEN taker=0 THEN pnl END) maker_pnl
            FROM tg GROUP BY city ORDER BY notional DESC""")
city["sk_share"] = city.sk_notional / city.notional
city["sk_roi"] = city.sk_pnl / city.sk_notional
city["vol_per_event"] = city.notional / city.events
city.to_csv(f"{OUT}/city.csv", index=False)

# archetypes among skilled
ar = q("""SELECT wallet, sum(notional) n,
          sum(CASE WHEN h_eod>24 THEN notional ELSE 0 END)/sum(notional) early,
          sum(CASE WHEN h_eod BETWEEN 0 AND 24 THEN notional ELSE 0 END)/sum(notional) obsday,
          sum(CASE WHEN h_eod<6 THEN notional ELSE 0 END)/sum(notional) late,
          sum(CASE WHEN h_eod<0 THEN notional ELSE 0 END)/sum(notional) post,
          sum(CASE WHEN price>=0.9 THEN notional ELSE 0 END)/sum(notional) hi_price,
          sum(CASE WHEN price<=0.1 THEN notional ELSE 0 END)/sum(notional) lo_price,
          1-sum(notional*taker)/sum(notional) maker,
          sum(CASE WHEN side=-1 THEN notional ELSE 0 END)/sum(notional) sells,
          sum(CASE WHEN act='Buy NO' THEN notional ELSE 0 END)/sum(notional) buy_no
          FROM tg WHERE g2='skilled_all' GROUP BY wallet""").set_index("wallet")
ar = ar.join(wf[["pnl", "gross", "fees", "roi", "events", "cities", "grp", "p", "p_cal", "pnl_A", "pnl_B", "med_fill"]])


def label(r):
    if r.maker > 0.6:
        return "1 Market Maker"
    if r.hi_price > 0.5 and (r.late > 0.5):
        return "2 Late Sweeper"
    if r.early > 0.5:
        return "3 Frueh-Positionierer"
    if r.obsday > 0.5:
        return "4 Intraday am Beobachtungstag"
    return "5 Gemischt"


ar["archetype"] = ar.apply(label, axis=1)
ar.sort_values("pnl", ascending=False).to_csv(f"{OUT}/skilled_archetypes.csv")
arch = ar.groupby("archetype").agg(wallets=("pnl", "size"), core=("grp", lambda s: (s == "core_skilled").sum()),
                                   pnl=("pnl", "sum"), fees=("fees", "sum"), notional=("n", "sum"),
                                   med_roi=("roi", "median"), med_events=("events", "median"),
                                   med_cities=("cities", "median"), med_fill=("med_fill", "median"),
                                   early=("early", "median"), late=("late", "median"), maker=("maker", "median"),
                                   hi_price=("hi_price", "median"), lo_price=("lo_price", "median"),
                                   buy_no=("buy_no", "median"), sells=("sells", "median"))
arch["roi"] = arch.pnl / arch.notional
arch.to_csv(f"{OUT}/archetype_summary.csv")
log("archetypes\n", arch[["wallets", "core", "pnl", "roi"]])

sk = wf[wf.grp.isin(["core_skilled", "skilled_winner"])].sort_values("pnl", ascending=False)
sk.head(30).join(ar[["archetype", "early", "late", "hi_price", "lo_price", "maker"]]).to_csv(f"{OUT}/top_skilled.csv")
toppnl = wf.sort_values("pnl", ascending=False).head(100)
meta = {
    "rows": int(con.execute("SELECT count(*) FROM t").fetchone()[0]),
    "events": int(con.execute("SELECT count(DISTINCT event_id) FROM t").fetchone()[0]),
    "cities": int(con.execute("SELECT count(DISTINCT city) FROM t").fetchone()[0]),
    "wallets": int(len(wf)), "date_from": dates[0], "date_to": dates[-1], "split": MID,
    "total_notional": float(wf.notional.sum()), "total_fees_est": float(wf.fees.sum()),
    "tested_wallets": int(len(full)), "class_counts": full.cls.value_counts().to_dict(),
    "bh10_count": int(full.bh10.sum()), "core_count": int(full.core.sum()),
    "skilled_also_cal_sig": int(((full.cls == "skilled_winner") & (full.p_cal < 0.05)).sum()),
    "persistence": persist,
    "top100_pnl_share_skilled": float(toppnl.grp.isin(["core_skilled", "skilled_winner"]).mean()),
    "top100_pnl_share_untested": float((toppnl.grp == "untested").mean()),
    "skilled_pnl_share_of_positive_pnl": float(sk.pnl.sum() / wf[wf.pnl > 0].pnl.sum()),
    "core_pnl_share_of_positive_pnl": float(wf[wf.grp == "core_skilled"].pnl.sum() / wf[wf.pnl > 0].pnl.sum()),
    "top10_skilled_share_of_skilled_pnl": float(sk.head(10).pnl.sum() / sk.pnl.sum()),
    "maker_pnl": float(con.execute("SELECT sum(pnl) FROM t WHERE taker=0").fetchone()[0]),
    "taker_pnl": float(con.execute("SELECT sum(pnl) FROM t WHERE taker=1").fetchone()[0]),
    "taker_gross": float(con.execute("SELECT sum(gross) FROM t WHERE taker=1").fetchone()[0]),
}
json.dump(meta, open(f"{OUT}/meta.json", "w"), indent=1, default=float)
log(json.dumps(meta, indent=1, default=float))
