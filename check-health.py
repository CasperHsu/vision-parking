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

    # 分類的關鍵：先看市府「有沒有重新寫入這一場」（upd 前進），再看數字有沒有變。
    # 有寫入＋數字一直是 0 → 那個 0 是可信的真滿位；沒寫入 → 任何數字都只是舊值，不能判斷。
    moving, truly_full, moto, sensor_stuck, no_write = [], [], [], [], []
    for pk, seq in by.items():
        vals = [r[2] for r in seq]
        tot = seq[-1][3]
        nm = names.get(pk, "?")
        upds = [r[4] for r in seq if len(r) > 4 and r[4]] if has_upd else []
        wrote = len(set(upds)) > 1 if upds else None  # None＝沒有 upd 資料可判斷

        if tot == 0:
            moto.append(pk)
        elif len(set(vals)) > 1:
            moving.append(pk)
        elif wrote is False:
            no_write.append((pk, nm, vals[-1], tot))
        elif set(vals) == {0}:
            truly_full.append((pk, nm, tot, wrote))
        else:
            sensor_stuck.append((pk, nm, vals[-1], tot, len(vals), wrote))

    print(f"合計 {len(by)} 場")
    print(f"  ✅ 有變動          {len(moving)} 場")
    print(f"  🅿️ 顯示 0 不變      {len(truly_full)} 場")
    print(f"  🏍 純機車場        {len(moto)} 場（無汽車位，本來就恆為 0）")
    print(f"  ⚠️ 有空位卻不動     {len(sensor_stuck)} 場")
    if no_write:
        print(f"  🔴 市府完全沒寫入   {len(no_write)} 場（數字是舊值，無法判斷真假）")

    if truly_full:
        print("\n🅿️ 顯示 0 且沒變：")
        for pk, nm, tot, wrote in sorted(truly_full, key=lambda x: -x[2]):
            if wrote:
                print(f"   {pk} {nm}（{tot} 格）　✅ 市府持續有在寫入 → 這個 0 可信，是真的滿了")
            elif wrote is None:
                print(f"   {pk} {nm}（{tot} 格）　❔ 無寫入時間資料，無法確認真假")

    if sensor_stuck:
        print("\n⚠️ 有空位卻完全不動（市府有寫入卻數字never變＝該場感測可能異常）：")
        for pk, nm, v, tot, n, wrote in sorted(sensor_stuck, key=lambda x: -x[4]):
            tag = "🔴 市府有在寫入卻不變，高度可疑" if wrote else "❔ 待確認"
            print(f"   {pk} {nm} 連續 {n} 筆都是 {v}/{tot}　{tag}")

    if no_write:
        print("\n🔴 這段期間市府完全沒有重新寫入（整批停擺時常見，非個別故障）：")
        for pk, nm, v, tot in no_write[:8]:
            print(f"   {pk} {nm} 停在 {v}/{tot}")
        if len(no_write) > 8:
            print(f"   ⋯⋯ 共 {len(no_write)} 場")

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
    if no_write and len(no_write) >= len(by) * 0.5:
        print("市府端整批停止寫入，本站管線正常但拿不到新資料——這段期間的數字都是舊值。")
    elif len(moving) >= len(by) * 0.7:
        print("整體資料流動正常。看到某場數字不動，多半是那一場真的滿位或本來就沒車位。")
    else:
        print("流動的場數偏低，建議檢查市府端寫入狀況與 cron。")


if __name__ == "__main__":
    main()
