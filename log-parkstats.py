#!/usr/bin/env python3
# 視界停車歷史統計：cron 每 2 分鐘執行
#   1) 把 /var/www/parking/parkinfo.json 快照寫進 SQLite（不呼叫任何外部 API）
#   2) 超過 FINE_DAYS 天的舊資料自動稀釋成每 10 分鐘一筆（近期留高解析度供診斷，
#      舊資料只用於統計平均，不需要那麼細）
#   3) stats.json 超過 1 小時就重新彙整：近 28 天、平日(wd)/週末(we)×24 小時的平均剩餘車位
#      格式：{"days":28,"stats":{"004":{"wd":[[平均,樣本數,天數]×24],"we":[...]}}}
#      顯示門檻看「天數」而非樣本數——同一天的密集取樣不算獨立證據
import json, os, sqlite3, time
from datetime import datetime
from zoneinfo import ZoneInfo

SRC = "/var/www/parking/parkinfo.json"
WX = "/var/www/parking/weather.json"
DB = "/var/lib/parking/stats.db"
OUT = "/var/www/parking/stats.json"
TZ = ZoneInfo("Asia/Taipei")
KEEP_DAYS, AGG_DAYS = 180, 28  # 2026-09-19 Casper 拍板：存 180 天、彙整看近 28 天
FINE_DAYS = 14                 # 近 14 天保留每 2 分鐘原始解析度，更舊的稀釋成每 10 分鐘
MIN_DAYS = 2                   # 統計要顯示給使用者，至少需要幾個「不同日子」的樣本

os.makedirs("/var/lib/parking", exist_ok=True)
con = sqlite3.connect(DB)
con.execute("CREATE TABLE IF NOT EXISTS snap(ts INTEGER, parkno TEXT, free INTEGER, total INTEGER)")
con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snap(ts)")
# upd＝市府系統寫入該筆的時間；用來分辨「車位真的沒變」與「官方停止更新」
if "upd" not in [r[1] for r in con.execute("PRAGMA table_info(snap)")]:
    con.execute("ALTER TABLE snap ADD COLUMN upd TEXT")

now = int(time.time())
try:
    data = json.load(open(SRC))
except Exception:
    data = []
rows = []
for x in data:
    try:
        rows.append((now, str(x["PARKNO"]).zfill(3),
                     int(x.get("FREEQUANTITY") or 0), int(x.get("TOTALQUANTITY") or 0),
                     x.get("UPDATETIME") or None))
    except Exception:
        pass
if rows:
    con.executemany("INSERT INTO snap(ts,parkno,free,total,upd) VALUES(?,?,?,?,?)", rows)
con.execute("DELETE FROM snap WHERE ts < ?", (now - KEEP_DAYS * 86400,))
# 舊資料稀釋：超過 FINE_DAYS 天的，只留落在 10 分鐘刻度上的那些
con.execute("DELETE FROM snap WHERE ts < ? AND (ts % 600) >= 120",
            (now - FINE_DAYS * 86400,))

# 天氣快照一起存（供日後做「雨天 vs 晴天車位差多少」的關聯分析）
con.execute("CREATE TABLE IF NOT EXISTS wx(ts INTEGER, temp REAL, pop INTEGER, rain REAL, uvi REAL)")
try:
    w = json.load(open(WX))
    con.execute("INSERT INTO wx VALUES(?,?,?,?,?)",
                (now, w.get("temp"), w.get("pop3"), w.get("rain_now"), w.get("uv3")))
except Exception:
    pass
con.execute("DELETE FROM wx WHERE ts < ?", (now - KEEP_DAYS * 86400,))
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
        a = agg.setdefault(parkno, {"wd": [[0, 0, set()] for _ in range(24)],
                                    "we": [[0, 0, set()] for _ in range(24)]})
        cell = a[kind][dt.hour]
        cell[0] += free
        cell[1] += 1
        cell[2].add(dt.toordinal())  # 這格涵蓋了哪幾個不同日子
    out = {}
    for pk, a in agg.items():
        out[pk] = {k: [[round(c[0] / c[1]), c[1], len(c[2])] if c[1] else None for c in a[k]]
                   for k in ("wd", "we")}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "min_days": MIN_DAYS,
                   "days": AGG_DAYS, "stats": out}, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)

con.close()
