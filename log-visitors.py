#!/usr/bin/env python3
"""視界停車：訪客長期統計（cron 每小時）
nginx 只保留 14 天紀錄，這支把它萃取成每日彙總存進 SQLite，紀錄過期後數據仍在。

口徑（2026-10-16 起）：
- 日期一律用台灣時間
- 「真實訪客」＝有抓 parkinfo.json 或送出開啟訊號（網頁 JS 真的跑起來）、非機器人、非自家網路、非員工裝置、而且是台灣的連線
  （2026-10-23 加：Google 爬蟲、網路掃描服務會用真瀏覽器把網頁跑起來，光看「JS 有跑」會被騙；
   台灣判斷用 APNIC 公開的 IP 分配表，每 30 天自動更新，海外的另外記在 overseas 欄、不刪除）
- 員工模式：頁面網址帶 ?staff=1 開過的裝置，之後的請求都會帶 staff=1，一律不列入（記在 staff 欄）
- 功能次數與點擊只算真實訪客（舊版把機器人與開發測試也算進去，會高估）
- 只重算「紀錄完整涵蓋」的日子；更早的日子保留當時的數字（標記為舊口徑）
隱私：不儲存原始 IP，只存加鹽雜湊（僅用來辨識同一人是否回訪）。
"""
import bisect, glob, gzip, hashlib, ipaddress, os, re, sqlite3, time, urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, parse_qs

TW = timezone(timedelta(hours=8))
DB = "/var/lib/parking/visitors.db"
SALT_FILE = "/etc/parking-visitor-salt"
LOGS = sorted(glob.glob("/var/log/nginx/access.log*"))   # logrotate 保留 14 天
OWN_IPS = {"114.43.100.227"}                              # Casper 自家網路（開發測試）
OWN_FILE = "/etc/parking-own-ips"                         # 要再排除的網路，一行一個 IP
TW_CACHE = "/var/lib/parking/tw-ip-ranges.txt"
APNIC = "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest"
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


def load_own():
    ips = set(OWN_IPS)
    try:
        ips |= {l.split("#")[0].strip() for l in open(OWN_FILE) if l.split("#")[0].strip()}
    except OSError:
        pass
    return ips


def load_tw():
    """回傳 is_tw(ip) 函式；每 30 天從 APNIC 更新一次，下載失敗沿用舊表，完全沒有表就回 None（不做地區過濾）"""
    stale = not os.path.exists(TW_CACHE) or time.time() - os.path.getmtime(TW_CACHE) > 30 * 86400
    if stale:
        try:
            req = urllib.request.Request(APNIC, headers={"User-Agent": "vision-parking"})
            txt = urllib.request.urlopen(req, timeout=60).read().decode("ascii", "ignore")
            out = [f"{p[2]} {p[3]} {p[4]}" for p in (l.split("|") for l in txt.splitlines())
                   if len(p) > 6 and p[1] == "TW" and p[2] in ("ipv4", "ipv6") and p[6] in ("allocated", "assigned")]
            if len(out) > 500:   # 防呆：資料明顯不完整就不覆蓋舊表
                with open(TW_CACHE + ".tmp", "w") as f:
                    f.write("\n".join(out))
                os.replace(TW_CACHE + ".tmp", TW_CACHE)
        except Exception:
            pass
    if not os.path.exists(TW_CACHE):
        return None
    v4, v6 = [], []
    for line in open(TW_CACHE):
        kind, start, val = line.split()
        if kind == "ipv4":
            a = int(ipaddress.IPv4Address(start)); v4.append((a, a + int(val) - 1))
        else:
            v6.append(ipaddress.IPv6Network(f"{start}/{val}", strict=False))
    v4.sort(); starts = [a for a, _ in v4]

    def is_tw(ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if a.version == 4:
            x = int(a); i = bisect.bisect_right(starts, x) - 1
            return i >= 0 and x <= v4[i][1]
        return any(a in n for n in v6)
    return is_tw


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
                     ("nav", "INT"), ("line", "INT"), ("grow", "INT"),
                     ("overseas", "INT"), ("staff", "INT")):
        if col not in have:
            con.execute(f"ALTER TABLE daily ADD COLUMN {col} {typ}")

    rows = list(read_logs())
    if not rows:
        return
    # 紀錄最早一筆之後的第一個完整台灣日，起算重算（之前的日子紀錄不完整，保留舊數字）
    earliest = min(r[1] for r in rows)
    start = (earliest + timedelta(days=1)).date() if (earliest.hour, earliest.minute) != (0, 0) else earliest.date()
    today = datetime.now(TW).date()

    own = load_own()
    is_tw = load_tw()
    # 第一輪：認出真實訪客 IP（台灣的才算；海外但有把網頁跑起來的另外記）
    real, abroad = set(), set()
    for ip, t, meth, url, code, ua in rows:
        if ip in own or BOT.search(ua) or "staff=1" in url:
            continue
        if "parkinfo.json" in url or url.startswith("/e?k=open"):
            (real if (is_tw is None or is_tw(ip)) else abroad).add(ip)

    day = {}
    seen, navlots = set(), {}
    for ip, t, meth, url, code, ua in rows:
        d = t.date()
        if d < start:
            continue
        r = day.setdefault(d, {"vids": set(), "pageloads": 0, "requests": 0, "dev": {},
                               "weather": 0, "stats": 0, "own": 0, "opens": 0, "app": 0,
                               "nav": 0, "line": 0, "grow": 0, "abroad": set(), "staff": 0})
        if ip in own:
            r["own"] += 1
            continue
        if "staff=1" in url:
            r["staff"] += 1
            continue
        if ip in abroad and "parkinfo.json" in url:
            r["abroad"].add(ip)
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
                        "stats": 0, "own": 0, "opens": 0, "app": 0, "nav": 0, "line": 0, "grow": 0,
                        "abroad": set(), "staff": 0})
        dv = r["dev"]
        con.execute("""INSERT INTO daily(d, visitors, pageloads, requests, iphone, android, ipad, mac,
                       windows, other, weather, stats, youbike, pwa, own_requests,
                       basis, opens, app_opens, nav, line, grow, overseas, staff)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)""",
                    (d.isoformat(), len(r["vids"]), r["pageloads"], r["requests"],
                     dv.get("iphone", 0), dv.get("android", 0), dv.get("ipad", 0), dv.get("mac", 0),
                     dv.get("windows", 0), dv.get("other", 0), r["weather"], r["stats"], r["own"],
                     "tw", r["opens"], r["app"], r["nav"], r["line"], r["grow"],
                     len(r["abroad"]), r["staff"]))
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
