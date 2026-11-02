#!/bin/bash
# 3D Üretim Takip — Raspberry Pi kurulumu (bir kez çalıştırılır).
# Pi internete bağlıyken (telefon hotspot'u) şu komutla:
#   wget -qO- https://raw.githubusercontent.com/sermenkreatif/uretim-takip/main/kur.sh | sudo bash
set -euo pipefail

DEPO="${DEPO:-https://raw.githubusercontent.com/sermenkreatif/uretim-takip/main}"
KOK=/opt/uretim-takip
SSID="${SSID:-3D-URETIM}"
KULLANICI="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
EV=$(getent passwd "$KULLANICI" | cut -d: -f6)
VERI="$EV/uretim-veri"

adim(){ echo; echo "==> $*"; }
[ "$(id -u)" = 0 ] || { echo "Bu betik sudo ile çalıştırılmalı."; exit 1; }
[ -n "$EV" ] && [ "$KULLANICI" != root ] || { echo "Kullanıcı bulunamadı (sudo ile, normal kullanıcıdan çalıştırın)."; exit 1; }

adim "Dosyalar indiriliyor ($DEPO)"
GECICI=$(mktemp -d)
wget -q -O "$GECICI/dosyalar.txt" "$DEPO/dosyalar.txt"
while read -r f; do
  [ -z "$f" ] && continue
  mkdir -p "$GECICI/$(dirname "$f")"
  wget -q -O "$GECICI/$f" "$DEPO/$f"
  [ -s "$GECICI/$f" ] || { echo "İndirilemedi: $f"; exit 1; }
done < "$GECICI/dosyalar.txt"
python3 -m py_compile "$GECICI/sunucu.py" "$GECICI/yedekle.py"
mkdir -p "$KOK"
cp -r "$GECICI"/. "$KOK"/
rm -rf "$KOK/__pycache__" "$GECICI"
echo "$DEPO" > "$KOK/depo"
echo "$VERI" > "$KOK/veri-yolu"
chmod +x "$KOK"/*.sh
echo "Tamam: $(wc -l < "$KOK/dosyalar.txt") dosya"

adim "Karekod paketi kuruluyor"
apt-get install -y -qq python3-qrcode >/dev/null 2>&1 || echo "Uyarı: python3-qrcode kurulamadı; karekodlar görünmez, sistem yine çalışır."

adim "Veri klasörü: $VERI"
mkdir -p "$VERI/foto"
chown -R "$KULLANICI:$KULLANICI" "$VERI"

adim "Yardımcı komutlar ve izinler"
install -m 755 "$KOK/guncelle.sh" /usr/local/bin/uretim-guncelle
install -m 755 "$KOK/uretim-saat.sh" /usr/local/bin/uretim-saat
install -m 755 "$KOK/uretim-wifi.sh" /usr/local/bin/uretim-wifi
echo "$KULLANICI ALL=(root) NOPASSWD: /usr/local/bin/uretim-guncelle, /usr/local/bin/uretim-saat, /usr/local/bin/uretim-wifi" > /etc/sudoers.d/uretim-takip
chmod 440 /etc/sudoers.d/uretim-takip
visudo -cf /etc/sudoers.d/uretim-takip >/dev/null

adim "Sunucu servisi"
cat > /etc/systemd/system/uretim-takip.service <<EOF
[Unit]
Description=3D Uretim Takip
After=network.target

[Service]
User=$KULLANICI
Environment=URETIM_VERI=$VERI
Environment=URETIM_PORT=80
ExecStart=/usr/bin/python3 $KOK/sunucu.py
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/uretim-yedek.service <<EOF
[Unit]
Description=3D Uretim Takip yedegi

[Service]
Type=oneshot
User=$KULLANICI
Environment=URETIM_VERI=$VERI
ExecStart=/usr/bin/python3 $KOK/yedekle.py
EOF
cat > /etc/systemd/system/uretim-yedek.timer <<EOF
[Unit]
Description=3D Uretim Takip yedegi (gunde iki kez)

[Timer]
OnCalendar=*-*-* 12:30:00
OnCalendar=*-*-* 17:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now uretim-takip.service uretim-yedek.timer >/dev/null
sleep 2
if curl -fs http://localhost/api/oturum >/dev/null; then echo "Sunucu çalışıyor."; else echo "HATA: sunucu yanıt vermiyor. 'journalctl -u uretim-takip -n 30' çıktısını gönderin."; exit 1; fi

adim "Pi'nin kendi Wi-Fi ağı ($SSID)"
if [ -f "$VERI/wifi.json" ]; then
  SIFRE=$(python3 -c "import json;print(json.load(open('$VERI/wifi.json'))['sifre'])")
else
  SIFRE=$(python3 -c "import secrets;print(''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(10)))")
fi
nmcli con delete uretim-ap >/dev/null 2>&1 || true
nmcli con add type wifi ifname wlan0 con-name uretim-ap autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$SIFRE" connection.autoconnect-priority 100 >/dev/null
cat > "$VERI/wifi.json" <<EOF
{"ssid": "$SSID", "sifre": "$SIFRE", "adres": "http://10.42.0.1"}
EOF
chown "$KULLANICI:$KULLANICI" "$VERI/wifi.json"
echo "Ağ tanımlandı (yeniden başlatınca açılacak)."

adim "Ekranda tam ekran açılış"
AUTO="$EV/.config/autostart"
mkdir -p "$AUTO" "$EV/.config/autostart-eski"
for f in "$AUTO"/*.desktop; do
  [ -e "$f" ] || continue
  [ "$(basename "$f")" = uretim-takip.desktop ] && continue
  mv "$f" "$EV/.config/autostart-eski/" && echo "Eski açılış kaydı kenara alındı: $(basename "$f")"
done
TARAYICI=$(command -v chromium-browser || command -v chromium || echo chromium-browser)
cat > "$AUTO/uretim-takip.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=3D Uretim Takip
Exec=sh -c 'sleep 6; $TARAYICI --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble --password-store=basic --enable-wayland-ime --check-for-update-interval=31536000 http://localhost/'
X-GNOME-Autostart-enabled=true
EOF
chown -R "$KULLANICI:$KULLANICI" "$EV/.config/autostart" "$EV/.config/autostart-eski"
command -v raspi-config >/dev/null && raspi-config nonint do_blanking 1 >/dev/null 2>&1 || true

echo
echo "================================================================"
echo " KURULUM TAMAM"
echo " Wi-Fi ağı : $SSID"
echo " Şifre     : $SIFRE"
echo " Adres     : http://10.42.0.1"
echo " Veriler   : $VERI"
echo "----------------------------------------------------------------"
echo " İlk açılışta ekran yönetici hesabı ister: adınızı, telefon"
echo " numaranızı ve 4-8 haneli bir PIN girin. Diğer kişileri sonra"
echo " dişli simgesi > Kullanıcılar bölümünden ekleyin."
echo "----------------------------------------------------------------"
echo " Pi 20 saniye sonra yeniden başlayacak. Açılınca kendi Wi-Fi"
echo " ağını yayınlar ve ekranda sistem tam ekran açılır."
echo " Bu SSH bağlantısı kopacak; bu normal."
echo "================================================================"
systemd-run --on-active=20 --unit=uretim-yeniden /bin/systemctl reboot >/dev/null
