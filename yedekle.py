#!/usr/bin/env python3
"""Gece yedeği: veritabanını ve fotoğrafları Pi'ye takılı USB belleğe kopyalar.

USB bellek yoksa yalnızca SD kart üzerinde günlük bir veritabanı kopyası tutulur
(kart bozulmasına karşı korumaz, sadece yanlış silmeye karşı korur).
Sonucu veri/yedek-durum.json dosyasına yazar; arayüz bunu gösterir.
"""
import json, os, shutil, sqlite3, time, datetime

KOK = os.path.dirname(os.path.abspath(__file__))
VERI = os.environ.get("URETIM_VERI", os.path.join(KOK, "veri"))
DB_YOL = os.path.join(VERI, "uretim.db")
FOTO = os.path.join(VERI, "foto")
SAKLA_GUN = 14


def usb_bul():
    adaylar = []
    for kok in ("/media",):
        if not os.path.isdir(kok):
            continue
        for d, alt, _ in os.walk(kok):
            if d.count(os.sep) > 3:
                alt[:] = []
                continue
            if os.path.ismount(d) and d != "/media" and os.access(d, os.W_OK):
                adaylar.append(d)
    return adaylar[0] if adaylar else None


def db_kopyala(hedef):
    kaynak = sqlite3.connect(DB_YOL)
    try:
        h = sqlite3.connect(hedef)
        with h:
            kaynak.backup(h)
        h.close()
    finally:
        kaynak.close()


def eskileri_sil(klasor):
    dosyalar = sorted(f for f in os.listdir(klasor) if f.startswith("uretim-") and f.endswith(".db"))
    for f in dosyalar[:-SAKLA_GUN]:
        try:
            os.remove(os.path.join(klasor, f))
        except OSError:
            pass


def main():
    gun = datetime.date.today().isoformat()
    durum = {"zaman": int(time.time() * 1000), "hedef": None, "foto": 0, "durum": "hata", "mesaj": ""}
    try:
        yerel = os.path.join(VERI, "yedek")
        os.makedirs(yerel, exist_ok=True)
        db_kopyala(os.path.join(yerel, "uretim-%s.db" % gun))
        eskileri_sil(yerel)

        usb = usb_bul()
        if not usb:
            durum.update(durum="usb_yok", mesaj="USB bellek takılı değil; yalnızca SD kart üzerinde kopya alındı.")
        else:
            hedef = os.path.join(usb, "uretim-yedek")
            os.makedirs(os.path.join(hedef, "foto"), exist_ok=True)
            db_kopyala(os.path.join(hedef, "uretim-%s.db" % gun))
            eskileri_sil(hedef)
            yeni = 0
            for f in os.listdir(FOTO):
                if not f.endswith(".jpg"):
                    continue
                k, h = os.path.join(FOTO, f), os.path.join(hedef, "foto", f)
                if not os.path.exists(h) or os.path.getsize(h) != os.path.getsize(k):
                    shutil.copy2(k, h + ".tmp"); os.replace(h + ".tmp", h); yeni += 1
            os.sync()
            durum.update(durum="tamam", hedef=usb, foto=yeni, mesaj="USB belleğe yedeklendi.")
    except Exception as e:
        durum["mesaj"] = "Yedek alınamadı: %s" % e
    with open(os.path.join(VERI, "yedek-durum.json"), "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False)
    print(json.dumps(durum, ensure_ascii=False))


if __name__ == "__main__":
    main()
