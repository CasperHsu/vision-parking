#!/usr/bin/env bash
# 在 DigitalOcean VPS（Ubuntu/Debian）上一鍵架設「市府周邊停車位」頁面
# 用法：把 hsinchu-parking.html 和本檔一起上傳到 VPS 同一個資料夾，然後：
#   sudo bash deploy-vps.sh
# 完成後用瀏覽器開 http://<VPS的IP>/parking/ 即可（手機加到主畫面）
set -euo pipefail

WEBROOT=/var/www/parking
API="https://hispark.hccg.gov.tw/OpenData/GetParkInfo"

# 1) nginx 靜態站
apt-get update -qq && apt-get install -y -qq nginx curl >/dev/null
mkdir -p "$WEBROOT"
cp "$(dirname "$0")/hsinchu-parking.html" "$WEBROOT/index.html"
# App 圖示與 manifest（存在才複製）
for f in favicon.ico favicon-32.png apple-touch-icon.png icon-192.png icon-512.png manifest.webmanifest; do
  if [ -f "$(dirname "$0")/$f" ]; then cp "$(dirname "$0")/$f" "$WEBROOT/$f"; fi
done

# 已有設定檔就不覆寫（2026-09-19 起 HTTPS/網域/轉址由 certbot 管理，重跑部署不可蓋掉）
if [ -f /etc/nginx/sites-available/parking ]; then
  echo "（保留既有 nginx 設定，未覆寫）"
else
cat >/etc/nginx/sites-available/parking <<'NGINX'
server {
    listen 80;
    server_name _;
    location /parking/ {
        alias /var/www/parking/;
        index index.html;
        add_header Cache-Control "no-store";
    }
}
NGINX
fi
ln -sf /etc/nginx/sites-available/parking /etc/nginx/sites-enabled/parking
rm -f /etc/nginx/sites-enabled/default   # 預設站台是 default_server，會攔走所有請求害 /parking/ 變 404
nginx -t && systemctl reload nginx

# 2) 防火牆：確保 80 有開（只加規則；不主動 enable ufw，避免鎖住 SSH）
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp >/dev/null 2>&1 || true
fi

# 3) 每分鐘把官方 JSON 抓到本機（頁面優先讀這個檔，不經第三方中繼）
cat >/usr/local/bin/fetch-parkinfo.sh <<EOF
#!/usr/bin/env bash
# cron 每分鐘啟動一次，內部抓 3 次、間隔 20 秒 → 快取最久 20 秒新
for i in 1 2 3; do
  tmp=\$(mktemp)
  if curl -sS -m 15 -o "\$tmp" "$API" && python3 -c "import json,sys; json.load(open('\$tmp'))" 2>/dev/null; then
    chmod 644 "\$tmp"   # mktemp 預設 600，nginx 會讀不到
    mv "\$tmp" "$WEBROOT/parkinfo.json"
  else
    rm -f "\$tmp"
  fi
  if [ "\$i" -lt 3 ]; then sleep 20; fi   # 用 if 寫法確保腳本結尾 exit 0
done
EOF
chmod +x /usr/local/bin/fetch-parkinfo.sh
/usr/local/bin/fetch-parkinfo.sh
( { crontab -l 2>/dev/null | grep -v fetch-parkinfo.sh; } || true ; echo "* * * * * /usr/local/bin/fetch-parkinfo.sh" ) | crontab -

# 4) TDX 加值資料（YouBike／路況快報／CCTV）——需要 /etc/parking-tdx.env（TDX_ID、TDX_SECRET）
if [ -f /etc/parking-tdx.env ] && [ -f "$(dirname "$0")/fetch-tdx.py" ]; then
  cp "$(dirname "$0")/fetch-tdx.py" /usr/local/bin/fetch-tdx.py
  chmod +x /usr/local/bin/fetch-tdx.py
  /usr/local/bin/fetch-tdx.py || true
  ( { crontab -l 2>/dev/null | grep -v fetch-tdx.py; } || true ; echo "* * * * * /usr/local/bin/fetch-tdx.py >/dev/null 2>&1" ) | crontab -
else
  echo "（略過 TDX 加值資料：缺 /etc/parking-tdx.env 或 fetch-tdx.py）"
fi

# 5) 歷史統計：每 2 分鐘記錄快照到 SQLite、每小時彙整 stats.json（不呼叫外部 API）
if [ -f "$(dirname "$0")/log-parkstats.py" ]; then
  cp "$(dirname "$0")/log-parkstats.py" /usr/local/bin/log-parkstats.py
  chmod +x /usr/local/bin/log-parkstats.py
  mkdir -p /var/lib/parking
  /usr/local/bin/log-parkstats.py || true
  ( { crontab -l 2>/dev/null | grep -v log-parkstats.py; } || true ; echo "*/2 * * * * /usr/local/bin/log-parkstats.py >/dev/null 2>&1" ) | crontab -
fi

# 6) 學院即時天氣：每 10 分鐘抓 Open-Meteo（免金鑰）存 weather.json
if [ -f "$(dirname "$0")/fetch-weather.py" ]; then
  cp "$(dirname "$0")/fetch-weather.py" /usr/local/bin/fetch-weather.py
  chmod +x /usr/local/bin/fetch-weather.py
  /usr/local/bin/fetch-weather.py || true
  ( { crontab -l 2>/dev/null | grep -v fetch-weather.py; } || true ; echo "*/10 * * * * /usr/local/bin/fetch-weather.py >/dev/null 2>&1" ) | crontab -
fi

# 7) 訪客長期統計：每小時把 nginx 紀錄萃取成每日彙總（log 只留 14 天，這裡永久保存）
for f in log-visitors.py visitor-report.py; do
  if [ -f "$(dirname "$0")/$f" ]; then
    cp "$(dirname "$0")/$f" /usr/local/bin/$f
    chmod +x /usr/local/bin/$f
  fi
done
if [ -f /usr/local/bin/log-visitors.py ]; then
  /usr/local/bin/log-visitors.py || true
  ( { crontab -l 2>/dev/null | grep -v log-visitors.py; } || true ; echo "7 * * * * /usr/local/bin/log-visitors.py >/dev/null 2>&1" ) | crontab -
fi

echo "完成。開啟 https://visionecoparking.visioneco.tw/"
