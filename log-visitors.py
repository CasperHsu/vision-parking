#!/usr/bin/env python3
"""視界停車：訪客長期統計
每小時執行，把 nginx 紀錄萃取成每日彙總存進 SQLite，
這樣即使 nginx log 被輪替刪除，歷史使用數據仍然保留。

隱私考量：不儲存原始 IP，只存加鹽雜湊（僅用於辨識「同一人是否回訪」）。
"""
import gzip, hashlib, os, re, sqlite3, sys
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))
DB = "/var/lib/parking/visitors.db"
SALT_FILE = "/etc/parking-visitor-salt"
# nginx 保留 14 天（logrotate rotate 14 + compress），全部掃進來
import glob
LOGS = sorted(glob.glob("/var/log/nginx/access.log*"))
# Casper 自己的網路（開發測試流量）分開計算，不混進真實使用者
OWN_IPS = {"114.43.100.227"}

BOT = re.compile(
    r"bot|spider|crawl|slurp|curl|wget|python|go-http|java/|okhttp|zgrab|masscan|"
    r"scanner|censys|expanse|internetmeasurement|l9explore|laravelbounty|research|"
    r"facebookexternalhit|headlesschrome|playwright|monitoring|uptime|probe",
    re.I)
LINE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')


def salt():
    if os.path.exists(SALT_FILE):
        return open(SALT_FILE, "rb").read().strip()
    s = os.urandom(24).hex().encode()
    with open(SALT_FILE, "wb") as f:
        f.write(s)
    os.chmod(SALT_FILE, 0o600)
    return s


def device(ua):
    u = ua.lower()
    for k, n in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
                 ("macintosh", "Mac"), ("windows", "Windows")):
        if k in u:
            return n
    return "其他"


def main():
    os.makedirs("/var/lib/parking", exist_ok=True)
    SALT = salt()
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS daily(
      d TEXT PRIMARY KEY, visitors INT, pageloads INT, requests INT,
      iphone INT, android INT, ipad INT, mac INT, windows INT, other INT,
      weather INT, stats INT, youbike INT, pwa INT, own_requests INT);
    CREATE TABLE IF NOT EXISTS visitors(
      vid TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, days INT, requests INT);
    CREATE TABLE IF NOT EXISTS visitor_days(vid TEXT, d TEXT, PRIMARY KEY(vid, d));
    """)

    # 第一階段：找出「真的把網頁跑起來」的 IP（有抓 parkinfo.json）
    real_ips = set()
    for path in LOGS:
        for p in (path,):
            if not os.path.exists(p):
                continue
            op = gzip.open if p.endswith(".gz") else open
            with op(p, "rt", errors="ignore") as f:
                for line in f:
                    m = LINE.match(line)
                    if m and "parkinfo.json" in m.group(4) and not BOT.search(m.group(6)):
                        real_ips.add(m.group(1))

    day = {}          # 日期 -> 統計
    seen = set()      # (vid, 日期)
    for path in LOGS:
        for p in (path,):
            if not os.path.exists(p):
                continue
            op = gzip.open if p.endswith(".gz") else open
            with op(p, "rt", errors="ignore") as f:
                for line in f:
                    m = LINE.match(line)
                    if not m:
                        continue
                    ip, ts, meth, url, code, ua = m.groups()
                    try:
                        d = datetime.strptime(ts.split()[0], "%d/%b/%Y:%H:%M:%S")
                    except ValueError:
                        continue
                    ds = d.strftime("%Y-%m-%d")
                    rec = day.setdefault(ds, {"vids": set(), "pageloads": 0, "requests": 0,
                                              "dev": {}, "weather": 0, "stats": 0,
                                              "youbike": 0, "pwa": 0, "own": 0})
                    # 功能使用次數（不分人）
                    if "weather.json" in url: rec["weather"] += 1
                    elif "stats.json" in url: rec["stats"] += 1
                    elif "youbike.json" in url: rec["youbike"] += 1
                    elif "apple-touch-icon" in url: rec["pwa"] += 1

                    if ip in OWN_IPS:
                        rec["own"] += 1
                        continue
                    if BOT.search(ua) or code.startswith(("4", "5")):
                        continue
                    # 只認「真的把網頁跑起來」的請求
                    if ip not in real_ips:
                        continue   # 掃描機器人常用正常 UA 打首頁，會灌水
                    if "parkinfo.json" not in url:
                        if url in ("/", "/index.html") and meth == "GET":
                            rec["pageloads"] += 1
                        continue
                    vid = hashlib.sha256(SALT + ip.encode()).hexdigest()[:16]
                    rec["vids"].add(vid)
                    rec["requests"] += 1
                    rec["dev"][device(ua)] = rec["dev"].get(device(ua), 0) + 1
                    seen.add((vid, ds))

    for ds, r in day.items():
        dv = r["dev"]
        con.execute("""INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(d) DO UPDATE SET
              visitors=max(visitors,excluded.visitors), pageloads=max(pageloads,excluded.pageloads),
              requests=max(requests,excluded.requests), iphone=max(iphone,excluded.iphone),
              android=max(android,excluded.android), ipad=max(ipad,excluded.ipad),
              mac=max(mac,excluded.mac), windows=max(windows,excluded.windows),
              other=max(other,excluded.other), weather=max(weather,excluded.weather),
              stats=max(stats,excluded.stats), youbike=max(youbike,excluded.youbike),
              pwa=max(pwa,excluded.pwa), own_requests=max(own_requests,excluded.own_requests)""",
            (ds, len(r["vids"]), r["pageloads"], r["requests"],
             dv.get("iPhone",0), dv.get("Android",0), dv.get("iPad",0), dv.get("Mac",0),
             dv.get("Windows",0), dv.get("其他",0),
             r["weather"], r["stats"], r["youbike"], r["pwa"], r["own"]))

    for vid, ds in seen:
        con.execute("INSERT OR IGNORE INTO visitor_days VALUES(?,?)", (vid, ds))
    con.execute("""INSERT INTO visitors(vid, first_seen, last_seen, days, requests)
        SELECT vid, min(d), max(d), count(*), 0 FROM visitor_days GROUP BY vid
        ON CONFLICT(vid) DO UPDATE SET
          first_seen=min(first_seen, excluded.first_seen),
          last_seen=max(last_seen, excluded.last_seen), days=excluded.days""")
    con.commit()
    con.close()
    print(f"已更新 {len(day)} 天的統計")


if __name__ == "__main__":
    main()
