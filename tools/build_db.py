"""Build an enriched DuckDB table from the raw pull (memory-safe for ~30M rows)."""
import sys, duckdb, pandas as pd, numpy as np
from zoneinfo import ZoneInfo
sys.path.insert(0, "tools")
from tz_map import TZ

D = sys.argv[1] if len(sys.argv) > 1 else "data"
DB = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/w.duckdb"
FEE_RATE = 0.05

ev = pd.read_csv(f"{D}/events.csv", dtype={"event_id": str})
mk = pd.read_csv(f"{D}/markets.csv", dtype={"event_id": str})
ev["city"] = ev["series"].str.replace("-daily-weather", "", regex=False)
unk = sorted(set(ev.city) - set(TZ))
print("unmapped tz:", unk)
ev = ev[(ev.clean_resolution == 1) & ev.city.isin(TZ.keys())].copy()


def bound(r, add):
    d = pd.Timestamp(r.event_date).to_pydatetime().replace(tzinfo=ZoneInfo(TZ[r.city]))
    return int((d + pd.Timedelta(days=add)).timestamp())


ev["sod"] = ev.apply(lambda r: bound(r, 0), axis=1)
ev["eod"] = ev.apply(lambda r: bound(r, 1), axis=1)
ev["open_ts"] = pd.to_datetime(ev.start, utc=True).astype("int64") // 10**9
mk["c14"] = mk.condition_id.str[:14]
mk["ord"] = mk.groupby("event_id").cumcount()
winord = mk[mk.yes_won == 1].groupby("event_id").ord.first().rename("win_ord")
mk = mk.merge(winord, on="event_id", how="left")

con = duckdb.connect(DB)
con.execute("PRAGMA memory_limit='5GB'; PRAGMA threads=2;")
con.register("ev_df", ev[["event_id", "city", "event_date", "sod", "eod", "open_ts", "volume", "n_markets"]])
con.register("mk_df", mk[["c14", "event_id", "bracket", "yes_won", "ord", "win_ord"]])
con.execute("CREATE OR REPLACE TABLE ev AS SELECT * FROM ev_df")
con.execute("CREATE OR REPLACE TABLE mk AS SELECT * FROM mk_df")
con.execute(f"""
CREATE OR REPLACE TABLE t AS
SELECT r.wallet, r.event_id, r.condition_id, r.outcome_index::TINYINT AS oi, r.side::TINYINT AS side,
       r.size::DOUBLE AS size, r.price::DOUBLE AS price, r.ts::BIGINT AS ts, r.taker::TINYINT AS taker,
       e.city, e.event_date, e.eod, e.sod, e.open_ts,
       m.yes_won, m.ord, abs(m.ord - m.win_ord) AS dist,
       CASE WHEN (r.outcome_index=0 AND m.yes_won=1) OR (r.outcome_index=1 AND m.yes_won=0) THEN 1.0 ELSE 0.0 END AS win_tok
FROM read_csv('{D}/trades/*.csv.gz', header=true,
     columns={{'wallet':'VARCHAR','event_id':'VARCHAR','condition_id':'VARCHAR','outcome_index':'INTEGER',
              'side':'INTEGER','size':'DOUBLE','price':'DOUBLE','ts':'BIGINT','taker':'INTEGER','tx':'VARCHAR'}}) r
JOIN mk m ON m.c14 = r.condition_id
JOIN ev e ON e.event_id = r.event_id
""")
con.execute(f"""
CREATE OR REPLACE TABLE t AS
SELECT *, price*size AS notional,
       side*(win_tok-price)*size AS gross,
       CASE WHEN taker=1 THEN size*{FEE_RATE}*price*(1-price) ELSE 0 END AS fee,
       side*(win_tok-price)*size - CASE WHEN taker=1 THEN size*{FEE_RATE}*price*(1-price) ELSE 0 END AS pnl,
       (eod - ts)/3600.0 AS h_eod,
       CASE WHEN oi=0 AND side=1 THEN 'Buy YES' WHEN oi=1 AND side=1 THEN 'Buy NO'
            WHEN oi=0 AND side=-1 THEN 'Sell YES' ELSE 'Sell NO' END AS act,
       ((ts % 86400)//3600)::INTEGER AS hour_utc
FROM t
""")
n, g, nt = con.execute("SELECT count(*), sum(gross), sum(notional) FROM t").fetchone()
print(f"rows {n:,}  zero-sum check gross={g:,.2f}  notional={nt:,.0f}")
print(con.execute("SELECT min(event_date), max(event_date), count(DISTINCT event_id), count(DISTINCT city), count(DISTINCT wallet) FROM t").fetchall())
