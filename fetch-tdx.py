#!/usr/bin/env python3
# 抓 TDX YouBike 資料給「視界學院-即時停車系統」（需求驅動，省免費點數）：
#   cron 每分鐘執行，但只有「近 5 分鐘內有瀏覽器要過 youbike.json」（＝有人開著 YouBike 圖層）
#   才真的呼叫 TDX；平常閒置時完全不耗點數。
# 金鑰放 /etc/parking-tdx.env（TDX_ID=...、TDX_SECRET=...，root 600）
import json, os, re, time, math, gzip, subprocess, datetime, urllib.request, urllib.parse

ENV = "/etc/parking-tdx.env"
OUT = "/var/www/parking"
TOK = "/var/cache/parking-tdx-token.json"
USAGE = "/var/cache/parking-tdx-usage.json"
ACCESS_LOG = "/var/log/nginx/access.log"
CLAT, CLNG = 24.80725, 120.96794  # 視界學院：新竹市北區中正路107號
# 免費方案 3 點/月＝4,500 次基礎服務呼叫；上限設 4,000 次留緩衝，超過當月自動停抓
MONTHLY_CALL_BUDGET = 4000

def budget(add=0):
    """回傳當月是否還有額度；add>0 時先累加本次呼叫數"""
    mon = time.strftime("%Y-%m")
    try:
        u = json.load(open(USAGE))
        if u.get("month") != mon:
            u = {"month": mon, "calls": 0}
    except Exception:
        u = {"month": mon, "calls": 0}
    if add:
        u["calls"] += add
        json.dump(u, open(USAGE, "w"))
    return u["calls"] < MONTHLY_CALL_BUDGET

def requested_recently(pattern, minutes=5):
    """近 minutes 分鐘內 nginx access log 有無人抓過 pattern；讀不到 log 時保守回 True（照抓）"""
    try:
        out = subprocess.run(["tail", "-n", "3000", ACCESS_LOG],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True
    now = time.time()
    for line in reversed(out.splitlines()):
        if pattern not in line:
            continue
        m = re.search(r"\[([^\]]+)\]", line)
        if not m:
            return True
        try:
            t = datetime.datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S %z").timestamp()
        except ValueError:
            return True
        return (now - t) < minutes * 60
    return False

def load_env():
    d = {}
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d

def get_token(e):
    try:
        t = json.load(open(TOK))
        if t["exp"] - time.time() > 600:
            return t["access_token"]
    except Exception:
        pass
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": e["TDX_ID"], "client_secret": e["TDX_SECRET"],
    }).encode()
    req = urllib.request.Request(
        "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
    j["exp"] = time.time() + j.get("expires_in", 86400)
    json.dump(j, open(TOK, "w"))
    os.chmod(TOK, 0o600)
    return j["access_token"]

def api(tk, path):
    req = urllib.request.Request(
        "https://tdx.transportdata.tw/api/basic/" + path,
        headers={"authorization": "Bearer " + tk, "accept-encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            b = gzip.decompress(b)
    return json.loads(b)

def dist(lat, lng):
    R = 6371000
    p1, p2 = math.radians(CLAT), math.radians(lat)
    dp, dl = math.radians(lat - CLAT), math.radians(lng - CLNG)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def write(name, obj):
    p = os.path.join(OUT, name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)

def main():
    # 沒人在看 YouBike 圖層就整個跳過，一次 TDX 都不叫
    if not requested_recently("youbike.json", 5):
        return
    # 當月呼叫數到達保險絲上限就停抓，確保免費額度絕不超
    if not budget():
        return
    e = load_env()
    tk = get_token(e)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    try:
        # OData select 只拿需要的欄位，減少傳輸量（TDX 點數按次數＋流量合併計算）
        st = api(tk, "v2/Bike/Station/City/Hsinchu?%24select=StationUID,StationName,StationPosition&%24format=JSON")
        time.sleep(1)
        av = api(tk, "v2/Bike/Availability/City/Hsinchu?%24select=StationUID,AvailableRentBikes,AvailableReturnBikes,AvailableRentBikesDetail&%24format=JSON")
        am = {x.get("StationUID"): x for x in av}
        outs = []
        for s in st:
            pos = s.get("StationPosition") or {}
            lat, lng = pos.get("PositionLat"), pos.get("PositionLon")
            if lat is None or lng is None:
                continue
            d = dist(lat, lng)
            if d > 1500:
                continue
            a = am.get(s.get("StationUID"), {})
            det = a.get("AvailableRentBikesDetail") or {}
            outs.append({
                "name": ((s.get("StationName") or {}).get("Zh_tw") or "").replace("YouBike2.0_", ""),
                "lat": lat, "lng": lng, "d": round(d),
                "rent": a.get("AvailableRentBikes"), "ret": a.get("AvailableReturnBikes"),
                "ebike": det.get("ElectricBikes"),
            })
        outs.sort(key=lambda x: x["d"])
        write("youbike.json", {"updated": now, "stations": outs})
        budget(add=2)
    except Exception:
        pass

main()
