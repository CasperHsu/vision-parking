#!/usr/bin/env python3
"""視界停車：訪客統計報表　用法 sudo visitor-report.py [天數，預設 30]"""
import json, sqlite3, sys
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
con = sqlite3.connect("/var/lib/parking/visitors.db")
cols = [c[1] for c in con.execute("PRAGMA table_info(daily)")]
rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM daily ORDER BY d DESC LIMIT ?", (N,))][::-1]
if not rows:
    print("尚無資料"); raise SystemExit
names = {}
try:
    for x in json.load(open("/var/www/parking/parkinfo.json")):
        names[str(x["PARKNO"]).zfill(3)] = x["PARKINGNAME"].replace("停車場", "")
except Exception:
    pass
new = [r for r in rows if r.get("basis") == "tw"]
legacy = [r for r in rows if r.get("basis") != "tw"]

print("═" * 60)
print(f"  視界停車 · 使用統計　　產生時間 {datetime.now(TW):%Y-%m-%d %H:%M}")
print("═" * 60)
print(f"\n【每日明細】最近 {len(rows)} 天（台灣時間）\n")
print("  日期          訪客  開啟  導航點擊")
print("  " + "─" * 40)
for r in rows:
    wd = "一二三四五六日"[datetime.strptime(r["d"], "%Y-%m-%d").weekday()]
    opens = r["opens"] if r.get("basis") == "tw" and r.get("opens") else r["pageloads"]
    nav = r["nav"] if r.get("basis") == "tw" else None
    mark = " " if r.get("basis") == "tw" else "*"
    print(f" {mark}{r['d']}({wd})  {r['visitors']:>3}  {opens:>4}  {('—' if nav is None else nav):>5}  {'█' * min(r['visitors'], 30)}")
tot = sum(r["visitors"] for r in rows)
print(f"\n  合計訪客人次 {tot}，平均每天 {tot/len(rows):.1f}")

uniq, = con.execute("SELECT count(*) FROM visitors").fetchone()
r2, = con.execute("SELECT count(*) FROM visitors WHERE days>=2").fetchone()
r3, = con.execute("SELECT count(*) FROM visitors WHERE days>=3").fetchone()
print(f"\n【回訪】累計不重複使用者 {uniq} 人｜回訪 ≥2 天 {r2} 人" +
      (f"（{r2/uniq*100:.0f}%）" if uniq else "") + f"｜≥3 天 {r3} 人")

print("\n【裝置】（查詢次數）")
tr = sum(r["requests"] for r in rows) or 1
for label, k in (("iPhone", "iphone"), ("Android", "android"), ("iPad", "ipad"),
                 ("Mac", "mac"), ("Windows", "windows"), ("其他", "other")):
    v = sum(r[k] or 0 for r in rows)
    if v:
        print(f"  {label:<8}{v:>5}　{'█' * max(1, v * 30 // tr)}")

if new:
    # 點擊類事件 2026-10-16 才開始送，之前的日子沒有這些數字
    ev = [r for r in new if (r["opens"] or 0) or (r["nav"] or 0)]
    since = ev[0]["d"] if ev else "2026-10-16"
    print(f"\n【互動】（點擊紀錄 {since} 起，只算真實訪客）")
    print(f"  網頁開啟　　　　 {sum(r['opens'] or 0 for r in new)} 次（其中從手機主畫面開 {sum(r['app_opens'] or 0 for r in new)} 次）")
    print(f"  點導航　　　　　 {sum(r['nav'] or 0 for r in new)} 次")
    print(f"  點 LINE 回饋　　 {sum(r['line'] or 0 for r in new)} 次")
    print(f"  點「擴大範圍」　 {sum(r['grow'] or 0 for r in new)} 次")
    print(f"  天氣／預測載入　 {sum(r['weather'] or 0 for r in new)}／{sum(r['stats'] or 0 for r in new)} 次")
    top = con.execute("SELECT parkno, sum(n) FROM nav_lots WHERE d>=? GROUP BY parkno ORDER BY 2 DESC LIMIT 5",
                      (since,)).fetchall()
    if top:
        print("  最常被導航的停車場：" + "、".join(f"{names.get(p, p)} {n} 次" for p, n in top))
own = sum(r["own_requests"] or 0 for r in rows)
if own:
    print(f"\n  （自家網路 {own} 次請求已排除）")
if legacy:
    print(f"\n  * {legacy[0]['d']}～{legacy[-1]['d']} 為舊口徑：日期以 UTC 切、當時尚無點擊紀錄")
print()
