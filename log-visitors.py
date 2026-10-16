#!/usr/bin/env python3
"""視界停車：訪客長期統計（cron 每小時）
nginx 只保留 14 天紀錄，這支把它萃取成每日彙總存進 SQLite，紀錄過期後數據仍在。

口徑（2026-10-16 起）：
- 日期一律用台灣時間
- 「真實訪客」＝有抓 parkinfo.json 或送出開啟訊號（網頁 JS 真的跑起來）、非機器人、非自家網路
- 功能次數與點擊只算真實訪客（舊版把機器人與開發測試也算進去，會高估）
- 只重算「紀錄完整涵蓋」的日子；更早的日子保留當時的數字（標記為舊口徑）
隱私：不儲存原始 IP，只存加鹽雜湊（僅用來辨識同一人是否回訪）。
"""
import glob, gzip, hashlib, os, re, sqlite3
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, parse_qs

TW = timezone(timedelta(hours=8))
DB = "/var/lib/parking/visitors.db"
SALT_FILE = "/etc/parking-visitor-salt"
LOGS = sorted(glob.glob("/var/log/nginx/access.log*"))   # logrotate 保留 14 天
OWN_IPS = {"114.43.100.227"}                              # Casper 自家網路（開發測試）
BOT = re.compile(
    r"bot|spider|crawl|slurp|curl|wget|python|go-http|java/|okhttp|zgrab|masscan|"
    r"scanner|censys|expanse|internetmeasurement|l9explore|laravelbounty|research|"
    r"facebookexternalhit|headlesschrome|playwright|monitoring|uptime|probe", re.I)
LINE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')
DEVICES = (("iphone", "iphone"), ("ipad", "ipad"), ("android", "android"),
           ("macintosh", "mac"), ("windows", "windows"))


def salt():
    if os.path.exists(SALT_FILE):
        return open(SALT_FILE, "rb").read().strip()
    s = os.urandom(24).hex().encode()
    with open(SALT_FILE, "wb") as f:
        f.write(s)
    os.chmod(SALT_FILE, 0o600)
    return s


def read_logs():
    for p in LOGS:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt", errors="ignore") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                ip, ts, meth, url, code, ua = m.groups()
                try:
                    t = datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z").astimezone(TW)
                except ValueError:
                    continue
                yield ip, t, meth, url, code, ua


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
    CREATE TABLE IF NOT EXISTS nav_lots(d TEXT, parkno TEXT, n INT, PRIMARY KEY(d, parkno));
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(daily)")}
    for col, typ in (("basis", "TEXT"), ("opens", "INT"), ("app_opens", "INT"),
                     ("nav", "INT"), ("line", "INT"), ("grow", "INT")):
        if col not in have:
            con.execute(f"ALTER TABLE daily ADD COLUMN {col} {typ}")

    rows = list(read_logs())
    if not rows:
        return
    # 紀錄最早一筆之後的第一個完整台灣日，起算重算（之前的日子紀錄不完整，保留舊數字）
    earliest = min(r[1] for r in rows)
    start = (earliest + timedelta(days=1)).date() if (earliest.hour, earliest.minute) != (0, 0) else earliest.date()
    today = datetime.now(TW).date()

    # 第一輪：認出真實訪客 IP
    real = set()
    for ip, t, meth, url, code, ua in rows:
        if ip in OWN_IPS or BOT.search(ua):
            continue
        if "parkinfo.json" in url or url.startswith("/e?k=open"):
            real.add(ip)

    day = {}
    seen, navlots = set(), {}
    for ip, t, meth, url, code, ua in rows:
        d = t.date()
        if d < start:
            continue
        r = day.setdefault(d, {"vids": set(), "pageloads": 0, "requests": 0, "dev": {},
                               "weather": 0, "stats": 0, "own": 0, "opens": 0, "app": 0,
                               "nav": 0, "line": 0, "grow": 0})
        if ip in OWN_IPS:
            r["own"] += 1
            continue
        if ip not in real or BOT.search(ua) or code.startswith(("4", "5")):
            continue
        path = urlsplit(url).path
        if path == "/e":
            q = parse_qs(urlsplit(url).query)
            k, p = q.get("k", [""])[0], q.get("p", [""])[0]
            if k == "open":
                r["opens"] += 1
                if p == "app":
                    r["app"] += 1
            elif k == "nav":
                r["nav"] += 1
                pk = p.split("-")[0].zfill(3) if p else "???"
                navlots[(d, pk)] = navlots.get((d, pk), 0) + 1
            elif k in ("line", "grow"):
                r[k] += 1
            continue
        if path in ("/", "/index.html") and meth == "GET":
            r["pageloads"] += 1
        elif path.endswith("weather.json"):
            r["weather"] += 1
        elif path.endswith("stats.json"):
            r["stats"] += 1
        elif path.endswith("parkinfo.json"):
            vid = hashlib.sha256(SALT + ip.encode()).hexdigest()[:16]
            r["vids"].add(vid)
            r["requests"] += 1
            dev = next((n for k, n in DEVICES if k in ua.lower()), "other")
            r["dev"][dev] = r["dev"].get(dev, 0) + 1
            seen.add((vid, d.isoformat()))

    s0 = start.isoformat()
    con.execute("DELETE FROM daily WHERE d >= ?", (s0,))
    con.execute("DELETE FROM visitor_days WHERE d >= ?", (s0,))
    con.execute("DELETE FROM nav_lots WHERE d >= ?", (s0,))
    d = start
    while d <= today:   # 沒人來的日子也寫一列 0，報表才看得出空窗
        r = day.get(d, {"vids": set(), "pageloads": 0, "requests": 0, "dev": {}, "weather": 0,
                        "stats": 0, "own": 0, "opens": 0, "app": 0, "nav": 0, "line": 0, "grow": 0})
        dv = r["dev"]
        con.execute("""INSERT INTO daily(d, visitors, pageloads, requests, iphone, android, ipad, mac,
                       windows, other, weather, stats, youbike, pwa, own_requests,
                       basis, opens, app_opens, nav, line, grow)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?)""",
                    (d.isoformat(), len(r["vids"]), r["pageloads"], r["requests"],
                     dv.get("iphone", 0), dv.get("android", 0), dv.get("ipad", 0), dv.get("mac", 0),
                     dv.get("windows", 0), dv.get("other", 0), r["weather"], r["stats"], r["own"],
                     "tw", r["opens"], r["app"], r["nav"], r["line"], r["grow"]))
        d += timedelta(days=1)
    con.executemany("INSERT OR IGNORE INTO visitor_days VALUES(?,?)", list(seen))
    con.executemany("INSERT INTO nav_lots VALUES(?,?,?)",
                    [(k[0].isoformat(), k[1], n) for k, n in navlots.items()])
    con.execute("DELETE FROM visitors")
    con.execute("""INSERT INTO visitors(vid, first_seen, last_seen, days, requests)
                   SELECT vid, min(d), max(d), count(*), 0 FROM visitor_days GROUP BY vid""")
    con.commit()
    con.close()
    print(f"已重算 {s0} 起共 {(today - start).days + 1} 天（更早的日子保留舊口徑）")


if __name__ == "__main__":
    main()
