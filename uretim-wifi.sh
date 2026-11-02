#!/bin/bash
# Pi'nin yayınladığı Wi-Fi ağının şifresini değiştirir. Bağlı tüm cihazlar düşer, yeni şifreyle bağlanmaları gerekir.
set -u
[[ "${1:-}" =~ ^[A-Za-z0-9]{8,32}$ ]] || exit 1
VERI=$(cat /opt/uretim-takip/veri-yolu)
SAHIP=$(stat -c %U "$VERI")
SSID=$(nmcli -g 802-11-wireless.ssid con show uretim-ap 2>/dev/null || echo 3D-URETIM)
sleep 2   # yanıt telefona ulaşsın
nmcli con modify uretim-ap wifi-sec.psk "$1" || exit 1
python3 - "$SSID" "$1" > "$VERI/wifi.json" <<'PY'
import json, sys
print(json.dumps({"ssid": sys.argv[1], "sifre": sys.argv[2], "adres": "http://10.42.0.1"}, ensure_ascii=False))
PY
chown "$SAHIP:$SAHIP" "$VERI/wifi.json"
nmcli con up uretim-ap >/dev/null 2>&1 || true
