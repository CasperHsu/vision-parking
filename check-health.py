#!/usr/bin/env python3
"""視界停車：資料健康檢查（在 VPS 上執行 sudo check-health.py [小時數]）

回答一個問題：「數字沒動，是真的沒車位，還是資料卡住了？」
分四種狀態：有變動／整段滿位／純機車場／⚠️疑似凍結
並檢查市府端 UPDATETIME 是否停止推進（這是分辨上游卡住的關鍵）
"""
import json, sqlite3, sys, time
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))
DB = "/var/lib/parking/stats.db"
SRC = "/var/www/parking/parkinfo.json"
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0


def main():
    names = {}
    try:
        for x in json.load(open(SRC)):
            names[str(x["PARKNO"]).zfill(3)] = x.get("PARKINGNAME", "")[:16]
    except Exception:
        pass

    con = sqlite3.connect(DB)
    has_upd = "upd" in [r[1] for r in con.execute("PRAGMA table_info(snap)")]
    since = int(time.time()) - int(HOURS * 3600)
    cols = "parkno,ts,free,total" + (",upd" if has_upd else "")
    rows = con.execute(f"SELECT {cols} FROM snap WHERE ts>=? ORDER BY parkno,ts", (since,)).fetchall()
    if not rows:
        print("這段期間沒有資料——先確認 cron 是否在跑：sudo crontab -l")
        return

    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)

    t0 = min(r[1] for r in rows)
    t1 = max(r[1] for r in rows)
    span = (t1 - t0) / 3600
    expected = int(span * 6) + 1  # 每 10 分鐘一筆
    print(f"觀測 {datetime.fromtimestamp(t0,TW):%m/%d %H:%M} → {datetime.fromtimestamp(t1,TW):%m/%d %H:%M}"
          f"（{span:.1f} 小時）")
    got = max(len(v) for v in by.values())
    print(f"快照筆數 {got}／預期約 {expected}"
          + ("　✅ 收集正常" if got >= expected * 0.9 else "　⚠️ 有缺漏，cron 可能中斷過"))
    print()

    moving, full, moto, frozen = [], [], [], []
    for pk, seq in by.items():
        vals = [r[2] for r in seq]
        tot = seq[-1][3]
        nm = names.get(pk, "?")
        if len(set(vals)) > 1:
            moving.append(pk)
        elif tot == 0:
            moto.append(pk)
        elif set(vals) == {0}:
            full.append((pk, nm, tot))
        else:
            frozen.append((pk, nm, vals[-1], tot, len(vals)))

    print(f"合計 {len(by)} 場")
    print(f"  ✅ 有變動        {len(moving)} 場")
    print(f"  🅿️ 整段顯示 0    {len(full)} 場")
    print(f"  🏍 純機車場      {len(moto)} 場（無汽車位，本來就恆為 0）")
    print(f"  ⚠️ 疑似凍結      {len(frozen)} 場")

    if full:
        print("\n🅿️ 整段顯示 0（可能真的滿，也可能是感測器沒回報——格數越多越可疑）：")
        for pk, nm, tot in sorted(full, key=lambda x: -x[2]):
            flag = "　← 格數多卻整段 0，建議留意" if tot >= 200 else ""
            print(f"   {pk} {nm}（{tot} 格）{flag}")

    if frozen:
        print("\n⚠️ 疑似凍結（有空位但數字完全不動）：")
        for pk, nm, v, tot, n in sorted(frozen, key=lambda x: -x[4]):
            print(f"   {pk} {nm} 連續 {n} 筆都是 {v}/{tot}")

    # 市府端是否還在寫入
    if has_upd:
        print("\n--- 市府端寫入狀況 ---")
        last = con.execute("SELECT ts,upd FROM snap WHERE upd IS NOT NULL ORDER BY ts DESC LIMIT 400").fetchall()
        if last:
            newest_ts = max(r[0] for r in last)
            ups = [r[1] for r in last if r[0] == newest_ts]
            def parse(s):
                try:
                    return datetime.fromisoformat(s).replace(tzinfo=TW)
                except Exception:
                    return None
            ds = [parse(u) for u in ups]
            ds = [d for d in ds if d]
            if ds:
                newest = max(ds)
                age = (datetime.now(TW) - newest).total_seconds() / 60
                verdict = "✅ 正常" if age <= 15 else ("⚠️ 落後" if age <= 40 else "🔴 明顯卡住")
                print(f"   市府最新寫入 {newest:%m/%d %H:%M}（{age:.0f} 分前）　{verdict}")
                print("   正常約 1〜3 分鐘寫一次；超過 15 分鐘代表市府端暫停更新，不是本站故障")
    else:
        print("\n（尚未收集 UPDATETIME 欄位，明天起的資料才有市府端寫入分析）")

    print("\n結論：", end="")
    if len(moving) >= len(by) * 0.7:
        print("整體資料流動正常。看到某場數字不動，多半是那一場真的滿位或本來就沒車位。")
    else:
        print("流動的場數偏低，建議檢查市府端寫入狀況與 cron。")


if __name__ == "__main__":
    main()
