#!/usr/bin/env python3
# 視界停車歷史統計：cron 每 10 分鐘執行
#   1) 把 /var/www/parking/parkinfo.json 快照寫進 SQLite（不呼叫任何外部 API）
#   2) stats.json 超過 1 小時就重新彙整：近 28 天、平日(wd)/週末(we)×24 小時的平均剩餘車位
#      格式：{"days":28,"stats":{"004":{"wd":[[平均,樣本數]×24],"we":[...]}}}，樣本不足的格子為 null
import json, os, sqlite3, time
from datetime import datetime
from zoneinfo import ZoneInfo

SRC = "/var/www/parking/parkinfo.json"
DB = "/var/lib/parking/stats.db"
OUT = "/var/www/parking/stats.json"
TZ = ZoneInfo("Asia/Taipei")
KEEP_DAYS, AGG_DAYS = 60, 28

os.makedirs("/var/lib/parking", exist_ok=True)
con = sqlite3.connect(DB)
con.execute("CREATE TABLE IF NOT EXISTS snap(ts INTEGER, parkno TEXT, free INTEGER, total INTEGER)")
con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snap(ts)")

now = int(time.time())
try:
    data = json.load(open(SRC))
except Exception:
    data = []
rows = []
for x in data:
    try:
        rows.append((now, str(x["PARKNO"]).zfill(3),
                     int(x.get("FREEQUANTITY") or 0), int(x.get("TOTALQUANTITY") or 0)))
    except Exception:
        pass
if rows:
    con.executemany("INSERT INTO snap VALUES(?,?,?,?)", rows)
con.execute("DELETE FROM snap WHERE ts < ?", (now - KEEP_DAYS * 86400,))
con.commit()

def stale(p, sec):
    try:
        return time.time() - os.path.getmtime(p) > sec
    except OSError:
        return True

if stale(OUT, 3500):
    agg = {}
    q = con.execute("SELECT ts,parkno,free FROM snap WHERE ts>=? AND total>0", (now - AGG_DAYS * 86400,))
    for ts, parkno, free in q:
        dt = datetime.fromtimestamp(ts, TZ)
        kind = "we" if dt.weekday() >= 5 else "wd"
        a = agg.setdefault(parkno, {"wd": [[0, 0] for _ in range(24)],
                                    "we": [[0, 0] for _ in range(24)]})
        cell = a[kind][dt.hour]
        cell[0] += free
        cell[1] += 1
    out = {}
    for pk, a in agg.items():
        out[pk] = {k: [[round(c[0] / c[1]), c[1]] if c[1] else None for c in a[k]]
                   for k in ("wd", "we")}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "days": AGG_DAYS, "stats": out}, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)

con.close()
