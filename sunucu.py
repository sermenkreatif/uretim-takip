#!/usr/bin/env python3
"""3D Üretim Takip — Raspberry Pi yerel sunucusu.

Yalnızca Python standart kütüphanesini kullanır (qrcode paketi varsa QR üretir).
Veriler SQLite'ta, fotoğraflar diskte tutulur. İnternet gerektirmez.
Giriş: kişiye özel PIN. Roller: yonetici, personel, izleyici.
"""
import hashlib, hmac, io, json, os, re, secrets, sqlite3, subprocess, threading, time, uuid
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

KOK = os.path.dirname(os.path.abspath(__file__))
VERI = os.environ.get("URETIM_VERI", os.path.join(KOK, "veri"))
PORT = int(os.environ.get("URETIM_PORT", "80"))
FOTO = os.path.join(VERI, "foto")
DB_YOL = os.path.join(VERI, "uretim.db")
SURUM_KOD = "1.1.0"
KOLEKSIYONLAR = {"stok", "isler", "ayar"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FOTO_RE = re.compile(r"^[a-f0-9]{32}$")
PIN_RE = re.compile(r"^[0-9]{4,8}$")
ROLLER = ("yonetici", "personel", "izleyici")
MAX_FOTO = 25 * 1024 * 1024
MAX_JSON = 512 * 1024
MAX_LOGO = 3 * 1024 * 1024
LOGO_TURLERI = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
OTURUM_SURE = 30 * 24 * 3600          # telefonlarda 30 gün açık kalır
HATALI_SINIR, KILIT_SURE = 5, 300      # 5 yanlış PIN -> 5 dakika bekleme
CEREZ = "ut"

os.makedirs(FOTO, exist_ok=True)
kilit = threading.Lock()
hatali = {}            # anahtar -> [sayı, kilit_bitis]
hatali_kilit = threading.Lock()


class baglan:
    """Bağlantıyı açar; blok sonunda hata yoksa kaydeder, her durumda kapatır."""
    def __enter__(self):
        self.c = sqlite3.connect(DB_YOL, timeout=10)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=FULL")
        return self.c

    def __exit__(self, tur, *_):
        try:
            if tur is None:
                self.c.commit()
            else:
                self.c.rollback()
        finally:
            self.c.close()
        return False


def simdi_ms():
    return int(time.time() * 1000)


def kur_db():
    with kilit, baglan() as c:
        c.execute("CREATE TABLE IF NOT EXISTS belge (kol TEXT NOT NULL, id TEXT NOT NULL, veri TEXT NOT NULL, PRIMARY KEY (kol, id))")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS kullanici (id TEXT PRIMARY KEY, ad TEXT NOT NULL, rol TEXT NOT NULL, "
                  "pin TEXT NOT NULL, tuz TEXT NOT NULL, aktif INTEGER NOT NULL DEFAULT 1, olusturma INTEGER, son_giris INTEGER)")
        kolonlar = [r[1] for r in c.execute("PRAGMA table_info(kullanici)")]
        if "telefon" not in kolonlar:
            c.execute("ALTER TABLE kullanici ADD COLUMN telefon TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS oturum (token TEXT PRIMARY KEY, kullanici TEXT NOT NULL, olusturma INTEGER, son INTEGER)")
        c.execute("INSERT OR IGNORE INTO meta VALUES ('surum','0')")
        if not c.execute("SELECT 1 FROM belge WHERE kol='ayar' AND id='genel'").fetchone():
            ilk = {"yazicilar": ["Bambu Lab H2S Combo", "Bambu Lab H2D Combo",
                                 "Bambu Lab H2D Combo + 40 W Lazer", "Phrozen Sonic Mega 8K V2"]}
            c.execute("INSERT INTO belge VALUES ('ayar','genel',?)", (json.dumps(ilk, ensure_ascii=False),))


# ---------------- kullanıcı / oturum ----------------
def pin_ozet(pin, tuz):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(tuz), 120_000).hex()


def kullanici_sayisi(c):
    return c.execute("SELECT COUNT(*) FROM kullanici").fetchone()[0]


def telefon_temizle(t):
    """0532 123 45 67, +90 532..., 5321234567 -> 5321234567. Geçersizse None."""
    d = re.sub(r"\D", "", str(t or ""))
    if d.startswith("90") and len(d) == 12:
        d = d[2:]
    if d.startswith("0") and len(d) == 11:
        d = d[1:]
    return d if re.match(r"^5\d{9}$", d) else None


def kullanici_ekle(c, ad, rol, pin, telefon):
    tuz = secrets.token_hex(16)
    i = "k" + secrets.token_hex(6)
    c.execute("INSERT INTO kullanici (id, ad, rol, pin, tuz, aktif, olusturma, telefon) VALUES (?,?,?,?,?,1,?,?)",
              (i, ad, rol, pin_ozet(pin, tuz), tuz, simdi_ms(), telefon))
    return i


def oturum_ac(c, kid):
    t = secrets.token_urlsafe(32)
    n = int(time.time())
    c.execute("INSERT INTO oturum VALUES (?,?,?,?)", (t, kid, n, n))
    c.execute("UPDATE kullanici SET son_giris=? WHERE id=?", (simdi_ms(), kid))
    c.execute("DELETE FROM oturum WHERE son < ?", (n - OTURUM_SURE,))
    return t


def kilitli_mi(anahtar):
    with hatali_kilit:
        k = hatali.get(anahtar)
        if k and k[1] > time.time():
            return int(k[1] - time.time()) + 1
    return 0


def hatali_say(anahtar):
    # Numara başına 5, cihaz (IP) başına 15 yanlış deneme. Pi ekranı herkesin ortak cihazı
    # olduğu için IP sınırı daha geniş: bir kişinin yanlışları diğerlerini kilitlemesin.
    sinir = HATALI_SINIR * 3 if anahtar.startswith("ip:") else HATALI_SINIR
    with hatali_kilit:
        k = hatali.setdefault(anahtar, [0, 0])
        k[0] += 1
        if k[0] >= sinir:
            k[0], k[1] = 0, time.time() + KILIT_SURE


def hatali_sifirla(anahtar):
    with hatali_kilit:
        hatali.pop(anahtar, None)


def ad_temizle(ad):
    ad = re.sub(r"\s+", " ", str(ad or "")).strip()
    return ad[:40]


# ---------------- veri ----------------
def surum_arttir(c):
    c.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k='surum'")


def surum_oku():
    with baglan() as c:
        return int(c.execute("SELECT v FROM meta WHERE k='surum'").fetchone()[0])


def hepsi():
    with baglan() as c:
        out = {k: [] for k in KOLEKSIYONLAR}
        for kol, i, v in c.execute("SELECT kol, id, veri FROM belge"):
            d = json.loads(v); d["id"] = i
            out.setdefault(kol, []).append(d)
        out["surum"] = int(c.execute("SELECT v FROM meta WHERE k='surum'").fetchone()[0])
    out["ayar"] = next((d for d in out["ayar"] if d["id"] == "genel"), None)
    return out


def birlestir(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            birlestir(a[k], v)
        else:
            a[k] = v
    return a


def stok_degis(stok_id, fark, kim):
    with kilit, baglan() as c:
        r = c.execute("SELECT veri FROM belge WHERE kol='stok' AND id=?", (stok_id,)).fetchone()
        if not r:
            return None
        d = json.loads(r[0])
        d["kalanGr"] = round((float(d.get("kalanGr") or 0) + float(fark)) * 10) / 10
        d["guncelleme"] = simdi_ms()
        d["degistiren"] = kim
        c.execute("UPDATE belge SET veri=? WHERE kol='stok' AND id=?", (json.dumps(d, ensure_ascii=False), stok_id))
        surum_arttir(c)
        return d["kalanGr"]


def sistem_dosya(ad):
    try:
        with open(os.path.join(VERI, ad), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def wifi_bilgi():
    d = sistem_dosya("wifi.json") or {}
    return {"ssid": d.get("ssid", ""), "sifre": d.get("sifre", ""), "adres": d.get("adres", "http://10.42.0.1")}


def qr_svg(metin):
    try:
        import qrcode, qrcode.image.svg
        img = qrcode.make(metin, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2)
        b = io.BytesIO(); img.save(b)
        return b.getvalue()
    except Exception:
        return None


def saat_esitle(istemci_ms):
    """Pi'de pil yoksa saat kayar; telefondan gelen saatle düzelt."""
    try:
        fark = abs(time.time() * 1000 - float(istemci_ms))
        if fark > 120_000:
            subprocess.run(["sudo", "-n", "/usr/local/bin/uretim-saat", str(int(float(istemci_ms) / 1000))],
                           timeout=10, capture_output=True)
            return True
    except Exception:
        pass
    return False


def vitrin():
    """Pi ekranındaki salt okunur pano: stok, renkler, yazıcı durumu. Para bilgisi içermez."""
    h = hepsi()
    stok = [{k: d.get(k) for k in ("id", "malzeme", "marka", "renk", "renkHex", "cap", "kalanGr", "baslangicGr", "kritikGr")}
            for d in h["stok"]]
    aktif = [{k: d.get(k) for k in ("ad", "yazici", "baslangic", "tahminiSure", "adet")}
             for d in h["isler"] if d.get("durum") == "baski"]
    sira = sum(1 for d in h["isler"] if d.get("durum") == "sira")
    yaz = (h["ayar"] or {}).get("yazicilar") or []
    w = wifi_bilgi()
    return {"stok": stok, "aktif": aktif, "sira": sira, "yazicilar": yaz, "surum": h["surum"],
            "ssid": w["ssid"], "sifre": w["sifre"], "adres": w["adres"], "saat": simdi_ms()}


def disk_durum():
    try:
        s = os.statvfs(VERI)
        foto = sum(os.path.getsize(os.path.join(FOTO, f)) for f in os.listdir(FOTO) if f.endswith(".jpg"))
        return {"bos": s.f_bavail * s.f_frsize, "toplam": s.f_blocks * s.f_frsize, "foto": foto}
    except Exception:
        return None


# Giriş yapmadan açılabilen yollar
ACIK = {"/", "/index.html", "/logo", "/api/oturum", "/api/giris", "/api/kurulum", "/api/kurulum-dosya", "/api/cikis"}
# Pi'nin kendi ekranı (vitrin) girişsiz görebilir; Wi-Fi'deki cihazlar bu adreslerden gelemez
YEREL = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
VITRIN_YOLLARI = {"/api/vitrin", "/qr.svg"}
# Yalnızca yöneticinin yapabildiği işlemler (yöntem, yol deseni)
YONETICI = [
    (None, re.compile(r"^/api/kullanici(lar)?(/.*)?$")),
    (None, re.compile(r"^/api/(guncelle|wifi-sifre|logo)$")),
    ("PUT", re.compile(r"^/api/belge/ayar/")), ("PATCH", re.compile(r"^/api/belge/ayar/")), ("DELETE", re.compile(r"^/api/belge/ayar/")),
]
# İzleyicinin de yapabildiği yazma işlemleri
IZLEYICI_YAZMA = {"/api/cikis", "/api/pin-degistir", "/api/saat"}


class Isleyici(BaseHTTPRequestHandler):
    server_version = "UretimTakip"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- yardımcılar
    def gonder(self, kod, govde=b"", tur="application/json; charset=utf-8", ek=None):
        if isinstance(govde, (dict, list)) or govde is None:
            govde = json.dumps(govde, ensure_ascii=False).encode("utf-8")
        elif isinstance(govde, str):
            govde = govde.encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", tur)
        self.send_header("Content-Length", str(len(govde)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (ek or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(govde)

    def hata(self, kod, mesaj, **ek):
        d = {"hata": mesaj}; d.update(ek)
        self.gonder(kod, d)

    def oku(self, sinir):
        n = int(self.headers.get("Content-Length") or 0)
        if n > sinir:
            raise ValueError("cok_buyuk")
        return self.rfile.read(n) if n else b""

    def json_oku(self):
        b = self.oku(MAX_JSON)
        d = json.loads(b or b"{}")
        if not isinstance(d, dict):
            raise ValueError("nesne_degil")
        return d

    def dosya(self, yol, tur, onbellek="no-cache"):
        try:
            with open(yol, "rb") as f:
                b = f.read()
        except FileNotFoundError:
            return self.hata(404, "bulunamadi")
        self.gonder(200, b, tur, {"Cache-Control": onbellek})

    def ip(self):
        return self.client_address[0]

    def token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        m = c.get(CEREZ)
        return m.value if m else None

    def ben(self):
        """Geçerli oturumun kullanıcısı (aktif değilse None)."""
        if hasattr(self, "_ben"):
            return self._ben
        self._ben = None
        t = self.token()
        if t:
            n = int(time.time())
            with baglan() as c:
                r = c.execute("SELECT k.id, k.ad, k.rol, o.son FROM oturum o JOIN kullanici k ON k.id=o.kullanici "
                              "WHERE o.token=? AND k.aktif=1", (t,)).fetchone()
                if r and r[3] >= n - OTURUM_SURE:
                    if n - r[3] > 300:
                        c.execute("UPDATE oturum SET son=? WHERE token=?", (n, t))
                    self._ben = {"id": r[0], "ad": r[1], "rol": r[2]}
        return self._ben

    def cerez_yaz(self, token, sure):
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (CEREZ, token, sure)}

    def yetki(self, yontem, yol):
        """Erişim denetimi. True -> devam; False -> yanıt gönderildi."""
        # Aynı bağlantıdan gelen her istekte oturum yeniden okunur (HTTP/1.1 bağlantı yeniden kullanımı)
        self.__dict__.pop("_ben", None)
        if yol in ACIK or yol.startswith("/fonts/"):
            return True
        if yol in VITRIN_YOLLARI and yontem == "GET" and self.ip() in YEREL:
            return True
        b = self.ben()
        if not b:
            self.hata(401, "oturum")
            return False
        yazma = yontem in ("POST", "PUT", "PATCH", "DELETE")
        if b["rol"] == "izleyici" and yazma and yol not in IZLEYICI_YAZMA:
            self.hata(403, "yetki")
            return False
        if b["rol"] != "yonetici":
            for y, desen in YONETICI:
                if (y is None or y == yontem) and desen.match(yol):
                    self.hata(403, "yetki")
                    return False
        return True

    # ---- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path); p = u.path
        if not self.yetki("GET", p):
            return
        if p in ("/", "/index.html"):
            return self.dosya(os.path.join(KOK, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/fonts/") and re.match(r"^/fonts/[a-z0-9-]+\.woff2$", p):
            return self.dosya(os.path.join(KOK, p.lstrip("/")), "font/woff2", "public, max-age=31536000, immutable")
        if p == "/logo":
            for uz, tur in LOGO_TURLERI.items():
                yol = os.path.join(VERI, "logo." + uz)
                if os.path.exists(yol):
                    with open(yol, "rb") as f:
                        veri = f.read()
                    return self.gonder(200, veri, tur, {"Cache-Control": "no-cache",
                                                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'"})
            return self.dosya(os.path.join(KOK, "logo-varsayilan.png"), "image/png")
        if p == "/api/oturum":
            b = self.ben()
            if b:
                return self.gonder(200, {"giris": True, "ben": b})
            with baglan() as c:
                kurulum = kullanici_sayisi(c) == 0
            return self.gonder(200, {"giris": False, "kurulumGerekli": kurulum})
        if p.startswith("/foto/"):
            i = p[6:]
            if not FOTO_RE.match(i):
                return self.hata(404, "bulunamadi")
            return self.dosya(os.path.join(FOTO, i + ".jpg"), "image/jpeg", "private, max-age=31536000, immutable")
        if p == "/api/vitrin":
            return self.gonder(200, vitrin())
        if p == "/api/surum":
            return self.gonder(200, {"surum": surum_oku()})
        if p == "/api/hepsi":
            return self.gonder(200, hepsi())
        if p == "/api/sistem":
            w = wifi_bilgi()
            return self.gonder(200, {
                "ssid": w["ssid"], "sifre": w["sifre"], "adres": w["adres"], "surumKod": SURUM_KOD,
                "yedek": sistem_dosya("yedek-durum.json"), "guncelleme": sistem_dosya("guncelleme-durum.json"),
                "saat": simdi_ms(), "disk": disk_durum(),
                "ozelLogo": any(os.path.exists(os.path.join(VERI, "logo." + u)) for u in LOGO_TURLERI),
                "logo": True,
            })
        if p == "/api/kullanicilar":
            with baglan() as c:
                L = [{"id": r[0], "ad": r[1], "rol": r[2], "aktif": bool(r[3]), "sonGiris": r[4], "telefon": r[5]}
                     for r in c.execute("SELECT id, ad, rol, aktif, son_giris, telefon FROM kullanici ORDER BY aktif DESC, ad")]
            return self.gonder(200, L)
        if p == "/qr.svg":
            q = parse_qs(u.query)
            w = wifi_bilgi()
            if q.get("t", [""])[0] == "wifi":
                metin = "WIFI:T:WPA;S:%s;P:%s;;" % (w["ssid"].replace(";", r"\;"), w["sifre"].replace(";", r"\;"))
            else:
                metin = w["adres"]
            svg = qr_svg(metin)
            if not svg:
                return self.hata(404, "qr_yok")
            return self.gonder(200, svg, "image/svg+xml", {"Cache-Control": "no-store"})
        m = re.match(r"^/api/belge/([a-z]+)/([^/]+)$", p)
        if m:
            kol, i = m.groups()
            if kol not in KOLEKSIYONLAR or not ID_RE.match(i):
                return self.hata(400, "gecersiz_yol")
            with baglan() as c:
                r = c.execute("SELECT veri FROM belge WHERE kol=? AND id=?", (kol, i)).fetchone()
            return self.gonder(200, json.loads(r[0]) if r else None)
        self.hata(404, "bulunamadi")

    # ---- yazma
    def do_PUT(self):
        p = urlparse(self.path).path
        if self.yetki("PUT", p):
            self.yaz("set", p)

    def do_PATCH(self):
        p = urlparse(self.path).path
        if self.yetki("PATCH", p):
            self.yaz("update", p)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if not self.yetki("DELETE", p):
            return
        m = re.match(r"^/api/foto/([a-f0-9]{32})$", p)
        if m:
            try:
                os.remove(os.path.join(FOTO, m.group(1) + ".jpg")); silindi = True
            except FileNotFoundError:
                silindi = False
            return self.gonder(200, {"deleted": silindi})
        if p == "/api/logo":
            for eski in LOGO_TURLERI:
                try:
                    os.remove(os.path.join(VERI, "logo." + eski))
                except FileNotFoundError:
                    pass
            with kilit, baglan() as c:
                surum_arttir(c)
            return self.gonder(200, {"tamam": True})
        m = re.match(r"^/api/kullanici/([A-Za-z0-9]+)$", p)
        if m:
            return self.kullanici_sil(m.group(1))
        self.yaz("delete", p)

    def yaz(self, islem, p):
        m = re.match(r"^/api/belge/([a-z]+)/([^/]+)$", p)
        if not m:
            return self.hata(404, "bulunamadi")
        kol, i = m.groups()
        if kol not in KOLEKSIYONLAR or not ID_RE.match(i):
            return self.hata(400, "gecersiz_yol")
        try:
            veri = self.json_oku() if islem != "delete" else None
        except Exception:
            return self.hata(400, "gecersiz_veri")
        kim = self.ben()["ad"]
        if veri is not None:
            for k in ("id", "olusturan", "olusturanZaman"):
                veri.pop(k, None)
            veri["degistiren"] = kim
        with kilit, baglan() as c:
            r = c.execute("SELECT veri FROM belge WHERE kol=? AND id=?", (kol, i)).fetchone()
            eski = json.loads(r[0]) if r else None
            if islem == "delete":
                c.execute("DELETE FROM belge WHERE kol=? AND id=?", (kol, i))
            elif islem == "set":
                if eski:
                    veri["olusturan"] = eski.get("olusturan")
                    veri["olusturanZaman"] = eski.get("olusturanZaman")
                else:
                    veri["olusturan"], veri["olusturanZaman"] = kim, simdi_ms()
                c.execute("INSERT OR REPLACE INTO belge VALUES (?,?,?)", (kol, i, json.dumps(veri, ensure_ascii=False)))
            else:
                if not eski:
                    return self.hata(404, "belge_yok")
                c.execute("UPDATE belge SET veri=? WHERE kol=? AND id=?",
                          (json.dumps(birlestir(eski, veri), ensure_ascii=False), kol, i))
            surum_arttir(c)
        self.gonder(200, {"tamam": True})

    def do_POST(self):
        p = urlparse(self.path).path
        if not self.yetki("POST", p):
            return
        if p == "/api/foto":
            try:
                b = self.oku(MAX_FOTO)
            except ValueError:
                return self.hata(413, "cok_buyuk")
            if not b.startswith(b"\xff\xd8"):
                return self.hata(415, "jpeg_degil")
            i = uuid.uuid4().hex
            gecici = os.path.join(FOTO, i + ".tmp")
            with open(gecici, "wb") as f:
                f.write(b); f.flush(); os.fsync(f.fileno())
            os.replace(gecici, os.path.join(FOTO, i + ".jpg"))
            return self.gonder(200, {"id": i, "url": "/foto/" + i, "sizeBytes": len(b)})
        if p == "/api/logo":
            tur = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            uz = {v: k for k, v in LOGO_TURLERI.items()}.get(tur)
            if not uz:
                return self.hata(415, "logo_turu")
            try:
                b = self.oku(MAX_LOGO)
            except ValueError:
                return self.hata(413, "cok_buyuk")
            if not b:
                return self.hata(400, "gecersiz_veri")
            for eski in LOGO_TURLERI:
                try:
                    os.remove(os.path.join(VERI, "logo." + eski))
                except FileNotFoundError:
                    pass
            with open(os.path.join(VERI, "logo." + uz), "wb") as f:
                f.write(b)
            with kilit, baglan() as c:
                surum_arttir(c)
            return self.gonder(200, {"tamam": True})
        try:
            d = self.json_oku()
        except Exception:
            return self.hata(400, "gecersiz_veri")

        # ---- giriş / çıkış / kurulum
        if p == "/api/kurulum":
            ad, pin, tel = ad_temizle(d.get("ad")), str(d.get("pin") or ""), telefon_temizle(d.get("telefon"))
            if not ad or not PIN_RE.match(pin) or not tel:
                return self.hata(400, "gecersiz_veri")
            with kilit, baglan() as c:
                if kullanici_sayisi(c) > 0:
                    return self.hata(409, "kurulum_yapilmis")
                kid = kullanici_ekle(c, ad, "yonetici", pin, tel)
                t = oturum_ac(c, kid)
            return self.gonder(200, {"giris": True, "ben": {"id": kid, "ad": ad, "rol": "yonetici"}}, ek=self.cerez_yaz(t, OTURUM_SURE))
        if p == "/api/kurulum-dosya":
            # İlk kurulumda kişi listesini tek seferde yükler. PIN'ler dosyada yalnızca özet olarak bulunur.
            L = d.get("kullanicilar")
            if not isinstance(L, list) or not 1 <= len(L) <= 50:
                return self.hata(400, "gecersiz_veri")
            temiz = []
            for k in L:
                ad, tel, rol = ad_temizle(k.get("ad")), telefon_temizle(k.get("telefon")), k.get("rol")
                oz, tuz = str(k.get("pinOzet") or ""), str(k.get("tuz") or "")
                if not ad or not tel or rol not in ROLLER or not re.match(r"^[0-9a-f]{64}$", oz) or not re.match(r"^[0-9a-f]{32}$", tuz):
                    return self.hata(400, "gecersiz_veri")
                temiz.append((ad, tel, rol, oz, tuz))
            if not any(t[2] == "yonetici" for t in temiz) or len({t[1] for t in temiz}) != len(temiz):
                return self.hata(400, "gecersiz_veri")
            with kilit, baglan() as c:
                if kullanici_sayisi(c) > 0:
                    return self.hata(409, "kurulum_yapilmis")
                for ad, tel, rol, oz, tuz in temiz:
                    c.execute("INSERT INTO kullanici (id, ad, rol, pin, tuz, aktif, olusturma, telefon) VALUES (?,?,?,?,?,1,?,?)",
                              ("k" + secrets.token_hex(6), ad, rol, oz, tuz, simdi_ms(), tel))
            return self.gonder(200, {"eklenen": len(temiz)})
        if p == "/api/giris":
            tel, pin = telefon_temizle(d.get("telefon")), str(d.get("pin") or "")
            anahtarlar = ("t:" + (tel or "-"), "ip:" + self.ip())
            for anahtar in anahtarlar:
                kalan = kilitli_mi(anahtar)
                if kalan:
                    return self.hata(429, "kilitli", saniye=kalan)
            r = None
            if tel:
                with baglan() as c:
                    r = c.execute("SELECT id, ad, rol, pin, tuz FROM kullanici WHERE telefon=? AND aktif=1", (tel,)).fetchone()
            # Numara kayıtlı değilse de aynı yanıt: kimin kayıtlı olduğu dışarıdan anlaşılmasın
            if not r or not PIN_RE.match(pin) or not hmac.compare_digest(pin_ozet(pin, r[4]), r[3]):
                for anahtar in anahtarlar:
                    hatali_say(anahtar)
                return self.hata(401, "pin")
            for anahtar in anahtarlar:
                hatali_sifirla(anahtar)
            with kilit, baglan() as c:
                t = oturum_ac(c, r[0])
            return self.gonder(200, {"giris": True, "ben": {"id": r[0], "ad": r[1], "rol": r[2]}}, ek=self.cerez_yaz(t, OTURUM_SURE))
        if p == "/api/cikis":
            t = self.token()
            if t:
                with kilit, baglan() as c:
                    c.execute("DELETE FROM oturum WHERE token=?", (t,))
            return self.gonder(200, {"tamam": True}, ek=self.cerez_yaz("", 0))
        if p == "/api/pin-degistir":
            b = self.ben()
            eski, yeni = str(d.get("eski") or ""), str(d.get("yeni") or "")
            if not PIN_RE.match(yeni):
                return self.hata(400, "pin_bicim")
            with kilit, baglan() as c:
                r = c.execute("SELECT pin, tuz FROM kullanici WHERE id=?", (b["id"],)).fetchone()
                if not hmac.compare_digest(pin_ozet(eski, r[1]), r[0]):
                    return self.hata(401, "pin")
                tuz = secrets.token_hex(16)
                c.execute("UPDATE kullanici SET pin=?, tuz=? WHERE id=?", (pin_ozet(yeni, tuz), tuz, b["id"]))
                c.execute("DELETE FROM oturum WHERE kullanici=? AND token<>?", (b["id"], self.token()))
            return self.gonder(200, {"tamam": True})

        # ---- kullanıcı yönetimi (yalnız yönetici)
        if p == "/api/kullanici":
            return self.kullanici_kaydet(None, d)
        m = re.match(r"^/api/kullanici/([A-Za-z0-9]+)$", p)
        if m:
            return self.kullanici_kaydet(m.group(1), d)
        if p == "/api/wifi-sifre":
            s = str(d.get("sifre") or "")
            if not re.match(r"^[A-Za-z0-9]{8,32}$", s):
                return self.hata(400, "sifre_bicim")
            try:
                subprocess.Popen(["sudo", "-n", "/usr/local/bin/uretim-wifi", s],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                return self.hata(500, "baslatilamadi")
            return self.gonder(200, {"basladi": True})

        # ---- veri işlemleri
        if p == "/api/stok-degis":
            sid = str(d.get("stok", ""))
            if not ID_RE.match(sid):
                return self.hata(400, "gecersiz_yol")
            try:
                fark = float(d.get("fark") or 0)
            except Exception:
                return self.hata(400, "gecersiz_veri")
            return self.gonder(200, {"kalanGr": stok_degis(sid, fark, self.ben()["ad"])})
        if p == "/api/saat":
            return self.gonder(200, {"duzeltildi": saat_esitle(d.get("ms", 0))})
        if p == "/api/guncelle":
            try:
                subprocess.Popen(["sudo", "-n", "/usr/local/bin/uretim-guncelle"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return self.gonder(200, {"basladi": True})
            except Exception:
                return self.hata(500, "baslatilamadi")
        if p == "/api/yedekle":
            try:
                r = subprocess.run(["python3", os.path.join(KOK, "yedekle.py")], timeout=600, capture_output=True, text=True)
                return self.gonder(200, sistem_dosya("yedek-durum.json") or {"durum": "bilinmiyor", "cikti": r.stdout[-500:]})
            except Exception:
                return self.hata(500, "yedek_basarisiz")
        self.hata(404, "bulunamadi")

    def kullanici_kaydet(self, kid, d):
        ad = ad_temizle(d.get("ad"))
        rol = d.get("rol")
        pin = str(d.get("pin") or "")
        tel = telefon_temizle(d.get("telefon"))
        if not tel:
            return self.hata(400, "telefon_bicim")
        if not ad or rol not in ROLLER or (pin and not PIN_RE.match(pin)) or (kid is None and not pin):
            return self.hata(400, "gecersiz_veri")
        ben = self.ben()
        with kilit, baglan() as c:
            if kid is None:
                if c.execute("SELECT 1 FROM kullanici WHERE ad=? COLLATE NOCASE", (ad,)).fetchone():
                    return self.hata(409, "ayni_ad")
                if c.execute("SELECT 1 FROM kullanici WHERE telefon=?", (tel,)).fetchone():
                    return self.hata(409, "ayni_telefon")
                kid = kullanici_ekle(c, ad, rol, pin, tel)
            else:
                r = c.execute("SELECT rol, aktif FROM kullanici WHERE id=?", (kid,)).fetchone()
                if not r:
                    return self.hata(404, "bulunamadi")
                if c.execute("SELECT 1 FROM kullanici WHERE ad=? COLLATE NOCASE AND id<>?", (ad, kid)).fetchone():
                    return self.hata(409, "ayni_ad")
                if c.execute("SELECT 1 FROM kullanici WHERE telefon=? AND id<>?", (tel, kid)).fetchone():
                    return self.hata(409, "ayni_telefon")
                aktif = 1 if d.get("aktif", True) else 0
                if kid == ben["id"] and (rol != "yonetici" or not aktif):
                    return self.hata(409, "kendini_kilitleme")
                if r[0] == "yonetici" and (rol != "yonetici" or not aktif):
                    n = c.execute("SELECT COUNT(*) FROM kullanici WHERE rol='yonetici' AND aktif=1").fetchone()[0]
                    if n <= 1:
                        return self.hata(409, "son_yonetici")
                c.execute("UPDATE kullanici SET ad=?, rol=?, aktif=?, telefon=? WHERE id=?", (ad, rol, aktif, tel, kid))
                if pin:
                    tuz = secrets.token_hex(16)
                    c.execute("UPDATE kullanici SET pin=?, tuz=? WHERE id=?", (pin_ozet(pin, tuz), tuz, kid))
                if pin or not aktif or rol != r[0]:
                    c.execute("DELETE FROM oturum WHERE kullanici=? AND token<>?", (kid, self.token()))
        hatali_sifirla("t:" + tel)
        return self.gonder(200, {"id": kid})

    def kullanici_sil(self, kid):
        ben = self.ben()
        if kid == ben["id"]:
            return self.hata(409, "kendini_kilitleme")
        with kilit, baglan() as c:
            r = c.execute("SELECT rol FROM kullanici WHERE id=?", (kid,)).fetchone()
            if not r:
                return self.gonder(200, {"tamam": True})
            c.execute("DELETE FROM oturum WHERE kullanici=?", (kid,))
            c.execute("DELETE FROM kullanici WHERE id=?", (kid,))
        return self.gonder(200, {"tamam": True})


if __name__ == "__main__":
    kur_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Isleyici)
    srv.daemon_threads = True
    print("3D Üretim Takip %s port %d, veri: %s" % (SURUM_KOD, PORT, VERI), flush=True)
    srv.serve_forever()
