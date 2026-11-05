#!/bin/bash
# Pi'nin yayınladığı Wi-Fi ağının şifresini değiştirir. Bağlı tüm cihazlar düşer, yeni şifreyle bağlanmaları gerekir.
set -u
# "oncelik": Pi yalnızca kayıtlı Wi-Fi'ye (telefon hotspot'u) bağlanır ve ona bağlanmayı hiç bırakmaz.
# Kendi ağını (3D-URETIM) artık kendiliğinden açmaz: sabah hotspot kapalıyken açılan Pi o ağda takılı kalıyordu.
# Ekran ve uygulama ağ olmadan da çalışır. Ayar zaten yapılmışsa hiçbir şey değiştirmez.
if [ "${1:-}" = "oncelik" ]; then
  if [ "$(nmcli -g connection.autoconnect con show uretim-ap 2>/dev/null)" != "no" ]; then
    nmcli con modify uretim-ap connection.autoconnect no || exit 1
    while IFS=: read -r ad tur; do
      [ "$tur" = "802-11-wireless" ] && [ "$ad" != "uretim-ap" ] && \
        nmcli con modify "$ad" connection.autoconnect yes connection.autoconnect-priority 50 connection.autoconnect-retries 0
    done < <(nmcli -t -f NAME,TYPE con show)
  fi
  sleep 5   # açılış tamamlansın
  # Kendi ağı açıksa kapat, kayıtlı ağa geç; o an yoksa NetworkManager bulana kadar kendisi denemeye devam eder
  if nmcli -t -f NAME con show --active | grep -qx uretim-ap; then
    nmcli con down uretim-ap >/dev/null 2>&1
    while IFS=: read -r ad tur; do
      [ "$tur" = "802-11-wireless" ] && [ "$ad" != "uretim-ap" ] || continue
      nmcli --wait 25 con up id "$ad" >/dev/null 2>&1 && exit 0
    done < <(nmcli -t -f NAME,TYPE con show)
  fi
  exit 0
fi
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
