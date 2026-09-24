#!/usr/bin/env python3
"""視界停車：訪客統計報表　用法 sudo visitor-report.py [天數，預設 30]"""
import sqlite3, sys
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
con = sqlite3.connect("/var/lib/parking/visitors.db")
rows = con.execute("SELECT * FROM daily ORDER BY d DESC LIMIT ?", (N,)).fetchall()
if not rows:
    print("尚無資料"); raise SystemExit
cols = [c[1] for c in con.execute("PRAGMA table_info(daily)")]
rows = [dict(zip(cols, r)) for r in rows][::-1]

print("═" * 62)
print("  視界停車 · 使用統計")
print(f"  產生時間 {datetime.now(TW):%Y-%m-%d %H:%M}")
print("═" * 62)
print(f"\n【每日明細】（最近 {len(rows)} 天）\n")
print("  日期          訪客   開啟頁面   查詢次數   主要裝置")
print("  " + "─" * 56)
for r in rows:
    devs = [("iPhone", r["iphone"]), ("Android", r["android"]), ("iPad", r["ipad"]),
            ("Mac", r["mac"]), ("Windows", r["windows"])]
    top = max(devs, key=lambda x: x[1])
    dtxt = f"{top[0]} {top[1]}" if top[1] else "—"
    wd = "一二三四五六日"[datetime.strptime(r["d"], "%Y-%m-%d").weekday()]
    print(f"  {r['d']}({wd})  {r['visitors']:>4}   {r['pageloads']:>6}   {r['requests']:>7}   {dtxt}")

tot_v = sum(r["visitors"] for r in rows)
tot_r = sum(r["requests"] for r in rows)
print(f"\n  期間合計：訪客人次 {tot_v}　查詢 {tot_r} 次　平均每天 {tot_v/len(rows):.1f} 人")

uniq, = con.execute("SELECT count(*) FROM visitors").fetchone()
ret = con.execute("SELECT count(*) FROM visitors WHERE days>1").fetchone()[0]
loyal = con.execute("SELECT count(*) FROM visitors WHERE days>=3").fetchone()[0]
print(f"\n【回訪分析】（不重複計算同一人）")
print(f"  累計不重複使用者　{uniq} 人")
print(f"  回訪 2 天以上　　 {ret} 人" + (f"（回訪率 {ret/uniq*100:.0f}%）" if uniq else ""))
print(f"  回訪 3 天以上　　 {loyal} 人　← 這是真正的忠實使用者")

print(f"\n【裝置分布】（期間累計查詢次數）")
for k, n in [("iPhone", "iphone"), ("Android", "android"), ("iPad", "ipad"),
             ("Mac", "mac"), ("Windows", "windows"), ("其他", "other")]:
    v = sum(r[n] for r in rows)
    if v:
        print(f"  {k:<8} {v:>5} 次　{'█' * min(30, max(1, v * 30 // max(1, tot_r)))}")

print(f"\n【功能使用】（期間累計）")
print(f"  天氣列被載入　　 {sum(r['weather'] for r in rows)} 次")
print(f"  歷史預測被載入　 {sum(r['stats'] for r in rows)} 次")
print(f"  YouBike 圖層　　 {sum(r['youbike'] for r in rows)} 次")
print(f"  加到手機主畫面　 {sum(r['pwa'] for r in rows)} 次")
own = sum(r["own_requests"] for r in rows)
if own:
    print(f"\n  （另有 {own} 次來自自家網路的請求，已排除在上述數字外）")
print()
