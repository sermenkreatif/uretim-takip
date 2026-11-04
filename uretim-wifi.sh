#!/bin/bash
# Pi'nin yayınladığı Wi-Fi ağının şifresini değiştirir. Bağlı tüm cihazlar düşer, yeni şifreyle bağlanmaları gerekir.
set -u
# "oncelik": kayıtlı Wi-Fi ağları (telefon hotspot'u) Pi'nin kendi ağından önce gelsin.
# Kayıtlı ağ çevrede yoksa Pi kendi ağını açar. Ayar zaten yapılmışsa hiçbir şey değiştirmez.
if [ "${1:-}" = "oncelik" ]; then
  [ "$(nmcli -g connection.autoconnect-priority con show uretim-ap 2>/dev/null)" = "-10" ] && exit 0
  nmcli con modify uretim-ap connection.autoconnect-priority -10 || exit 1
  while IFS=: read -r ad tur; do
    [ "$tur" = "802-11-wireless" ] && [ "$ad" != "uretim-ap" ] && nmcli con modify "$ad" connection.autoconnect-priority 50 connection.autoconnect yes
  done < <(nmcli -t -f NAME,TYPE con show)
  sleep 5   # açılış tamamlansın
  # Şu an kendi ağı açıksa kayıtlı ağlara geçmeyi dene; hiçbiri yoksa kendi ağına geri dön
  if nmcli -t -f NAME con show --active | grep -qx uretim-ap; then
    nmcli con down uretim-ap >/dev/null 2>&1
    while IFS=: read -r ad tur; do
      [ "$tur" = "802-11-wireless" ] && [ "$ad" != "uretim-ap" ] || continue
      nmcli --wait 25 con up id "$ad" >/dev/null 2>&1 && exit 0
    done < <(nmcli -t -f NAME,TYPE con show)
    nmcli con up uretim-ap >/dev/null 2>&1 || true
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
