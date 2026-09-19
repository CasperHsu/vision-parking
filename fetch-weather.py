#!/usr/bin/env python3
# 視界學院即時停車系統：學院即時天氣快取
# 資料源 Open-Meteo（免金鑰、CC BY 4.0）；cron 每 10 分鐘執行，寫 weather.json 供頁面讀取
# 之後若要換中央氣象署官方資料：改 URL 與欄位對應即可，輸出格式不變
import json, os, tempfile, urllib.request
from datetime import datetime, timezone, timedelta

LAT, LNG = 24.80725, 120.96794  # 視界學院（新竹市北區中正路107號）
OUT = "/var/www/parking/weather.json"
URL = ("https://api.open-meteo.com/v1/forecast"
       f"?latitude={LAT}&longitude={LNG}"
       "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code"
       "&hourly=precipitation_probability,uv_index"
       "&forecast_days=2&timezone=Asia%2FTaipei")


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "vision-parking (github.com/CasperHsu/vision-parking)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
    cur, hh = j["current"], j["hourly"]

    # 從「現在這個小時」起算 4 個小時桶（涵蓋未來 3 小時）取最大值
    now = datetime.now(timezone(timedelta(hours=8)))
    key = now.strftime("%Y-%m-%dT%H:00")
    try:
        i = hh["time"].index(key)
    except ValueError:
        i = 0
    pop3 = max([v for v in hh["precipitation_probability"][i:i + 4] if v is not None] or [0])
    uv3 = max([v for v in hh["uv_index"][i:i + 4] if v is not None] or [0])

    out = {
        "fetched": now.isoformat(timespec="seconds"),
        "temp": cur["temperature_2m"],
        "feel": cur["apparent_temperature"],
        "rh": cur["relative_humidity_2m"],
        "rain_now": cur["precipitation"],
        "wcode": cur["weather_code"],
        "pop3": pop3,
        "uv3": round(uv3, 1),
        "src": "open-meteo",
    }
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT))
    with os.fdopen(fd, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)  # mkstemp 預設 600，nginx 會讀不到
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
