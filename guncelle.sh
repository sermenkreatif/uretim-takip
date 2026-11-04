#!/bin/bash
# 3D Üretim Takip — güncelleme. Arayüzdeki "Güncellemeleri al" düğmesi çalıştırır (root).
# Pi'nin kendi Wi-Fi'sini kısa süre kapatır, kayıtlı bir Wi-Fi'ye (telefon hotspot'u) bağlanır,
# yeni dosyaları indirir, kontrol eder, yerine koyar ve kendi ağını yeniden açar.
set -u
# Sunucu servisi yeniden başlarken bu betik de ölmesin: kendi ayrı biriminde çalış
if [ -z "${URETIM_AYRIK:-}" ]; then
  exec systemd-run --quiet --collect --unit="uretim-guncelle-$(date +%s)" --setenv=URETIM_AYRIK=1 /usr/local/bin/uretim-guncelle
fi
KOK=/opt/uretim-takip
DEPO=$(cat "$KOK/depo")
VERI=$(cat "$KOK/veri-yolu")
SAHIP=$(stat -c %U "$VERI")
KILIT=/run/uretim-guncelle.lock

exec 9>"$KILIT"; flock -n 9 || exit 0

durum(){
  python3 - "$1" "$2" > "$VERI/guncelleme-durum.json" <<'EOF'
import json, sys, time
print(json.dumps({"zaman": int(time.time()*1000), "durum": sys.argv[1], "mesaj": sys.argv[2]}, ensure_ascii=False))
EOF
  chown "$SAHIP:$SAHIP" "$VERI/guncelleme-durum.json"
}
# Güncelleme bitince: Pi internetteyse (hotspot) öyle kalsın; değilse kendi ağını aç
agi_ac(){ internet || nmcli con up uretim-ap >/dev/null 2>&1 || true; }
internet(){ curl -fsI --max-time 6 "$DEPO/dosyalar.txt" >/dev/null 2>&1; }

durum calisiyor "Güncelleme başladı, internete bağlanılıyor."
# Pi zaten internetteyse (telefon hotspot'una bağlı) ağa dokunma
BAGLANDI=0
internet && BAGLANDI=1
[ $BAGLANDI = 0 ] && { nmcli con down uretim-ap >/dev/null 2>&1 || true; sleep 3; }

# Kayıtlı Wi-Fi ağlarını (hotspot dahil) sırayla dene
[ $BAGLANDI = 0 ] && while IFS=: read -r ad tur; do
  [ "$tur" = "802-11-wireless" ] || continue
  [ "$ad" = "uretim-ap" ] && continue
  nmcli --wait 25 con up id "$ad" >/dev/null 2>&1 || continue
  for i in $(seq 1 10); do internet && { BAGLANDI=1; break; }; sleep 2; done
  [ $BAGLANDI = 1 ] && break
done < <(nmcli -t -f NAME,TYPE con show)

if [ $BAGLANDI = 0 ]; then
  agi_ac
  durum hata "İnternete çıkılamadı. Telefonun hotspot'u açık ve Pi'nin tanıdığı ağ mıydı?"
  exit 1
fi

GECICI=$(mktemp -d)
HATA=""
curl -fsS --max-time 30 -o "$GECICI/dosyalar.txt" "$DEPO/dosyalar.txt" || HATA="dosya listesi indirilemedi"
if [ -z "$HATA" ]; then
  while read -r f; do
    [ -z "$f" ] && continue
    mkdir -p "$GECICI/$(dirname "$f")"
    curl -fsS --max-time 60 -o "$GECICI/$f" "$DEPO/$f" && [ -s "$GECICI/$f" ] || { HATA="$f indirilemedi"; break; }
  done < "$GECICI/dosyalar.txt"
fi
[ -z "$HATA" ] && { python3 -m py_compile "$GECICI/sunucu.py" "$GECICI/yedekle.py" 2>/dev/null || HATA="indirilen kod bozuk"; }
[ -z "$HATA" ] && { bash -n "$GECICI/guncelle.sh" || HATA="indirilen güncelleme betiği bozuk"; }

agi_ac

if [ -n "$HATA" ]; then
  rm -rf "$GECICI"
  durum hata "Güncelleme yapılmadı: $HATA. Eski sürüm çalışmaya devam ediyor."
  exit 1
fi

rm -rf "$KOK.onceki"; cp -a "$KOK" "$KOK.onceki"
rm -rf "$GECICI/__pycache__"
cp -r "$GECICI"/. "$KOK"/
chmod +x "$KOK"/*.sh
install -m 755 "$KOK/guncelle.sh" /usr/local/bin/uretim-guncelle
install -m 755 "$KOK/uretim-saat.sh" /usr/local/bin/uretim-saat
install -m 755 "$KOK/uretim-wifi.sh" /usr/local/bin/uretim-wifi
echo "$SAHIP ALL=(root) NOPASSWD: /usr/local/bin/uretim-guncelle, /usr/local/bin/uretim-saat, /usr/local/bin/uretim-wifi" > /etc/sudoers.d/uretim-takip.yeni
chmod 440 /etc/sudoers.d/uretim-takip.yeni
if visudo -cf /etc/sudoers.d/uretim-takip.yeni >/dev/null; then mv /etc/sudoers.d/uretim-takip.yeni /etc/sudoers.d/uretim-takip; else rm -f /etc/sudoers.d/uretim-takip.yeni; fi
rm -rf "$GECICI"
systemctl restart uretim-takip
sleep 3
if curl -fs http://localhost/api/oturum >/dev/null; then
  durum tamam "Güncellendi."
else
  rm -rf "$KOK"; mv "$KOK.onceki" "$KOK"; systemctl restart uretim-takip
  durum hata "Yeni sürüm açılmadı, önceki sürüme geri dönüldü."
fi
