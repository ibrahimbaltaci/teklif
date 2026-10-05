#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teklif Masası veri güncelleyici.

dijitalankastre.com'un ürün API'sinden stoktaki ürünleri çeker, index.html içindeki
PRODUCTS satırını yeniler ve sw.js önbellek adını değiştirir (telefonlar yeni sürümü
kendiliğinden alır). Uygulama koduna dokunmaz; yalnızca veri satırlarını değiştirir.

Kullanım:
    python tools/guncelle.py           # siteden çek, değişiklik varsa dosyaları güncelle
    python tools/guncelle.py --kuru    # yalnızca karşılaştır, hiçbir dosyaya yazma
    python tools/guncelle.py --damga   # veri çekmeden sw.js önbellek adını index.html'e göre yenile
                                       # (uygulama kodu elle değiştirildikten sonra)

Bir şey ters giderse (site yanıt vermiyor, ürün sayısı anormal düşmüş, görseller inmiyor)
hiçbir dosyaya yazmadan hata koduyla çıkar; canlıdaki uygulama olduğu gibi kalır.
"""
import base64
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(KOK, 'index.html')
SW = os.path.join(KOK, 'sw.js')
GECMIS = os.path.join(KOK, 'tools', 'gecmis.json')
RAPOR = os.path.join(KOK, 'tools', 'SON_GUNCELLEME.md')

API = 'https://dijitalankastre.com/wp-json/wc/store/v1/products'
ALANLAR = 'id,name,sku,prices,is_in_stock,images,categories,brands,description,short_description'
UA = 'TeklifMasasi-VeriGuncelleme/1.0 (+https://github.com/ibrahimbaltaci/teklif)'

GORSEL_BOYUT = 230      # px, uzun kenar
GORSEL_KALITE = 58      # JPEG kalitesi
MADDE_LIMIT = 12        # ürün başına en fazla özellik maddesi
MADDE_BUTCE = 520       # maddelerin toplam karakter sınırı (teklif satırı taşmasın)
MADDE_UZUNLUK = 200     # tek maddenin karakter sınırı

# Güvenlik eşikleri: bunların altında kalan bir çekim "bozuk" sayılır, hiçbir şey yazılmaz
EN_AZ_URUN = 300
EN_AZ_ORAN = 0.6        # önceki ürün sayısına göre
EN_AZ_GORSEL_ORANI = 0.95

TR_SAAT = timezone(timedelta(hours=3))


# ----------------------------------------------------------------------------- yardımcılar
def tr_fold(s):
    return (s or '').casefold().replace('ı', 'i').replace('i̇', 'i')


def sku_anahtar(s):
    return re.sub(r'[^A-Z0-9ĞÜŞİÖÇI]', '', (s or '').strip().upper())


def temiz_ad(s):
    return re.sub(r'\s+', ' ', html.unescape(html.unescape(s or ''))).strip()


def http_get(url, deneme=4, zaman_asimi=90):
    """(gövde, başlıklar) döndürür; geçici hatalarda artan beklemeyle yeniden dener."""
    url = urllib.parse.quote(url, safe=':/?&=%[],+@!$\'()*;~-._')
    son = None
    for i in range(deneme):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=zaman_asimi) as r:
                return r.read(), r.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            son = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404):
                break
            time.sleep(2 + 4 * i)
    raise RuntimeError(f'İstek başarısız: {url} ({son})')


# ----------------------------------------------------------------------------- siteden çekme
def siteden_cek():
    urunler, sayfa, toplam = [], 1, None
    while True:
        govde, bas = http_get(f'{API}?per_page=100&page={sayfa}&_fields={ALANLAR}')
        if toplam is None and bas.get('X-WP-Total'):
            toplam = int(bas.get('X-WP-Total'))
        parca = json.loads(govde)
        if not isinstance(parca, list):
            raise RuntimeError(f'API beklenmeyen yanıt verdi (sayfa {sayfa})')
        if not parca:
            break
        urunler += parca
        if toplam is not None and len(urunler) >= toplam:
            break
        sayfa += 1
        if sayfa > 100:
            raise RuntimeError('Sayfalama durmadı (100+ sayfa)')
        time.sleep(0.6)
    if toplam is not None and len(urunler) != toplam:
        raise RuntimeError(f'Eksik çekim: {len(urunler)} / {toplam}')
    return urunler


# ----------------------------------------------------------------------------- özellik maddeleri
GEREKSIZ = [
    'servisini aramanız', 'servis gelmeden', 'garanti geçerli olmayacak', 'dijital ankastre',
    'dijitalankastre', 'kalite ve güvenin adı', 'mutfağınızı baştan yaratıyoruz',
    'üstün müşteri memnuniyeti', 'hasarlı olan ürünleri teslim almayınız',
    'üreticiler veya distribütörlerden alınmaktadır', 'farklılıklar olabilir',
    'sorumlu tutulamaz', 'kargo', 'tutanak',
]
YALNIZ_BASLIK = {
    'ürün özellikleri', 'teknik özellikler', 'özellikler', 'teknik çizim', 'ölçü şablonu',
    'genel ölçü', 'ölçüler', 'açıklama', 'ürün açıklaması', 'öne çıkan özellikler',
    'genel özellikler', 'teknik bilgiler', 'ürün bilgisi', 'ürün bilgileri', 'teknik detaylar',
    'ürün tanımlama', 'kapasite', 'boyutlar', 'özellikleri', 'genel bilgiler', 'teknik özellikleri',
}


def _tr_kucuk(s):
    return s.replace('İ', 'i').replace('I', 'ı').lower()


def _kisalt(satir, sinir=MADDE_UZUNLUK):
    if len(satir) <= sinir:
        return satir
    kes = satir[:sinir]
    m = max(kes.rfind('. '), kes.rfind('! '), kes.rfind('; '))
    if m >= 60:
        return kes[:m + 1]
    return kes[:kes.rfind(' ')].rstrip(' ,;:-–—') + '…'


def maddeler(aciklama, ad):
    """Ürün açıklaması HTML'ini teklif tablosundaki kısa özellik maddelerine çevirir."""
    t = aciklama or ''
    t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', t)
    t = re.sub(r'(?i)<\s*img[^>]*>', ' ', t)
    t = re.sub(r'(?is)</\s*t[dh]\s*>\s*<\s*t[dh][^>]*>', ' : ', t)   # tablo: "etiket: değer"
    isaret = '\x01'
    t = re.sub(r'(?i)<\s*li[^>]*>', '\n' + isaret, t)
    t = re.sub(r'(?i)<\s*tr[^>]*>', '\n' + isaret, t)
    t = re.sub(r'(?i)<\s*br\s*/?>', '\n', t)
    t = re.sub(r'(?i)</?\s*(p|div|h[1-6]|ul|ol|table|tbody|thead|tr|li|dt|dd|section|blockquote)[^>]*>', '\n', t)
    t = re.sub(r'<[^>]+>', '', t)
    t = html.unescape(html.unescape(t)).replace('\xa0', ' ')
    ad_k = _tr_kucuk(ad)
    liste, duz, gorulen = [], [], set()
    for ham in t.split('\n'):
        madde_mi = isaret in ham
        s = re.sub(r'\s+', ' ', ham.replace(isaret, ' ')).strip()
        s = re.sub(r'^[\s•·\-–—*▪●✓✔►»:]+', '', s).strip()
        s = re.sub(r'\s+:\s+', ': ', s)
        if len(s) < 3:
            continue
        k = _tr_kucuk(s)
        if k.rstrip(' :') in YALNIZ_BASLIK or any(g in k for g in GEREKSIZ):
            continue
        if k.rstrip(' .:') == ad_k.rstrip(' .:') or re.match(r'^https?://', k) or k in gorulen:
            continue
        gorulen.add(k)
        (liste if madde_mi else duz).append(s)
    # liste/tablo maddeleri yeterliyse onları kullan; değilse düz satırlarla tamamla
    aday = liste if len(liste) >= 3 else liste + duz
    sonuc, toplam = [], 0
    for m in aday:
        m = _kisalt(m)
        if len(sonuc) >= 3 and toplam + len(m) > MADDE_BUTCE:
            break
        sonuc.append(m)
        toplam += len(m)
        if len(sonuc) >= MADDE_LIMIT:
            break
    return sonuc


# ----------------------------------------------------------------------------- marka / kategori
MARKA_YAZIM = {'maximus': 'Maximus'}                 # sitedeki yazım → uygulamadaki yazım
ON_EK_MARKA = {'faber': 'Franke'}                    # sitede Franke markası altında satılanlar
EK_MARKALAR = ['Elica', 'Electrolux', 'Blaupunkt', 'Bien', 'Karcher', 'Simfer', 'AEG', 'Bosch',
               'Siemens', 'Grohe', 'Geberit', 'Fakir', 'Luxell', 'Samsung', 'Silverline',
               'Dominox', 'Dreame', 'Eca', 'Esty', 'Airking', 'Asil', 'Cucinox']


def marka_bul(s, ad, eski, bilinen):
    adlar = [temiz_ad(b.get('name')) for b in (s.get('brands') or []) if b.get('name')]
    adlar = [MARKA_YAZIM.get(tr_fold(x), x) for x in adlar]
    ad_f = tr_fold(ad)
    if len(adlar) == 1:
        return adlar[0]
    if len(adlar) > 1:
        if eski and eski.get('b') in adlar:
            return eski['b']
        return next((x for x in adlar if ad_f.startswith(tr_fold(x))), adlar[0])
    if eski and eski.get('b') and eski['b'] != 'Diğer':
        return eski['b']                              # sitede marka yoksa önceki değeri koru
    for x in sorted(bilinen, key=len, reverse=True):
        if ad_f.startswith(tr_fold(x) + ' '):
            return x
    for on_ek, x in ON_EK_MARKA.items():
        if ad_f.startswith(on_ek + ' '):
            return x
    return 'Diğer'


def kategori_bul(s):
    ks = [temiz_ad(c.get('name')) for c in (s.get('categories') or []) if c.get('name')]
    return ks[-1] if ks else 'Diğer Ürünler'


# ----------------------------------------------------------------------------- görseller
# Site bazı ürünlerde ana görseli logolu dekor sahnesi yapıyor; teklif tablosunda beyaz zeminli
# ürün fotoğrafı daha okunaklı. Kural: ana görsel "sahne" ise galerideki 2. görsel temiz ürün
# fotoğrafıysa onu kullan; değilse önceki görsel temizse onu koru; hiçbiri yoksa ana görseli kullan.
# Teknik çizim / enerji etiketi / ölçü tablosu "temiz ürün fotoğrafı" testini geçemez.
def gorsel_ac(url):
    from PIL import Image
    govde, _ = http_get(url, deneme=3, zaman_asimi=60)
    im = Image.open(io.BytesIO(govde))
    if im.mode in ('RGBA', 'LA', 'P'):
        im = im.convert('RGBA')
        zemin = Image.new('RGB', im.size, (255, 255, 255))
        zemin.paste(im, (0, 0), im)
        im = zemin
    else:
        im = im.convert('RGB')
    im.thumbnail((GORSEL_BOYUT, GORSEL_BOYUT))
    return im


def veri_uri(im):
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=GORSEL_KALITE, optimize=True)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def uri_ac(uri):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))).convert('RGB')


def kenar_olcum(im):
    """Dış %5'lik çerçeve için (beyaz oranı, beyaz olmayanların parlaklık sapması, ortalama doygunluk)."""
    w, h = im.size
    m = max(2, int(min(w, h) * 0.05))
    px = im.load()
    par, doy, ak, tot = [], [], 0, 0
    for y in range(h):
        kenar_satiri = y < m or y >= h - m
        for x in (range(w) if kenar_satiri else list(range(m)) + list(range(w - m, w))):
            r, g, b = px[x, y]
            tot += 1
            if min(r, g, b) >= 240:
                ak += 1
            else:
                par.append(0.299 * r + 0.587 * g + 0.114 * b)
                mx, mn = max(r, g, b), min(r, g, b)
                doy.append((mx - mn) / mx if mx else 0)
    ort = sum(par) / len(par) if par else 0
    sapma = (sum((v - ort) ** 2 for v in par) / len(par)) ** 0.5 if par else 0
    return ak / tot, sapma, (sum(doy) / len(doy) if doy else 0)


def sahne_mi(im):
    """Dekorlu ortam fotoğrafı: kenarlarda hiç beyaz zemin yok, kenarlar desenli ve renkli."""
    beyaz, sapma, doygunluk = kenar_olcum(im)
    return beyaz < 0.10 and sapma >= 40 and doygunluk >= 0.09


def temiz_mi(im):
    """Beyaz zeminde ürün fotoğrafı: kenarlar beyaz ve ortada dolu bir nesne var (çizgi/tablo değil)."""
    from PIL import ImageFilter
    if kenar_olcum(im)[0] < 0.92:
        return False
    maske = im.convert('L').point(lambda v: 255 if v < 247 else 0).filter(ImageFilter.MinFilter(5))
    hist = maske.histogram()
    return hist[255] / sum(hist) >= 0.20


def gorsel_sec(galeri, onceki):
    """(veri-URI, karar) döndürür. karar: ana | galeri | onceki | ana-sahne"""
    adres = lambda g: g.get('thumbnail') or g.get('src')
    ana = gorsel_ac(adres(galeri[0]))
    if not sahne_mi(ana):
        return veri_uri(ana), 'ana'
    if len(galeri) > 1:
        try:
            ikinci = gorsel_ac(adres(galeri[1]))
            if temiz_mi(ikinci):
                return veri_uri(ikinci), 'galeri'
        except Exception:
            pass
    if onceki and onceki.get('i'):
        try:
            if not sahne_mi(uri_ac(onceki['i'])):
                return onceki['i'], 'onceki'
        except Exception:
            pass
    return veri_uri(ana), 'ana-sahne'


# ----------------------------------------------------------------------------- index.html
def mevcut_veri():
    sayfa = open(INDEX, encoding='utf-8').read()
    satirlar = sayfa.split('\n')
    idx = [i for i, l in enumerate(satirlar) if l.startswith('const PRODUCTS=')]
    if len(idx) != 1:
        raise RuntimeError(f'index.html içinde tek bir PRODUCTS satırı bekleniyordu, {len(idx)} bulundu')
    eski = json.loads(satirlar[idx[0]][len('const PRODUCTS='):].rstrip().rstrip(';'))
    return satirlar, idx[0], eski


def veri_satiri(urunler):
    return 'const PRODUCTS=' + json.dumps(urunler, ensure_ascii=False, separators=(',', ':')) + ';'


def damgala():
    """sw.js önbellek adını index.html içeriğinin özetine bağlar: içerik değişince telefonlar yenilenir."""
    ozet = hashlib.sha1(open(INDEX, 'rb').read()).hexdigest()[:10]
    sw = open(SW, encoding='utf-8').read()
    yeni, n = re.subn(r"const CACHE = '[^']*';", f"const CACHE = 'teklif-{ozet}';", sw)
    if n != 1:
        raise RuntimeError('sw.js içinde CACHE satırı bulunamadı')
    if yeni != sw:
        open(SW, 'w', encoding='utf-8').write(yeni)
    return f'teklif-{ozet}'


# ----------------------------------------------------------------------------- ana akış
def fiyat(s):
    f = s.get('prices') or {}
    bolen = 10 ** int(f.get('currency_minor_unit') or 0)
    satis = int(f.get('price') or 0) / bolen
    liste = int(f.get('regular_price') or 0) / bolen
    return satis, (liste if liste > 0 else None)


def calistir(kuru=False):
    if not kuru:
        try:
            import PIL  # noqa: F401  (görsel küçültme için gerekli; yoksa en başta dur)
        except ImportError:
            raise RuntimeError('Pillow kurulu değil: pip install pillow')
    simdi = datetime.now(TR_SAAT)
    satirlar, veri_idx, eski = mevcut_veri()
    eski_sku = {sku_anahtar(p['c']): p for p in eski}

    site = siteden_cek()
    site = [s for s in site if s.get('is_in_stock', True)]
    bilinen = {temiz_ad(b['name']) for s in site for b in (s.get('brands') or []) if b.get('name')}
    bilinen |= {p['b'] for p in eski if p.get('b') and p['b'] != 'Diğer'} | set(EK_MARKALAR)

    yeni, indirilecek, gorulen = [], [], set()
    for s in site:
        ad = temiz_ad(s.get('name'))
        kod = (s.get('sku') or '').strip() or f"ID{s.get('id')}"
        satis, liste = fiyat(s)
        anahtar = sku_anahtar(kod)
        if not ad or satis <= 0 or anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        onceki = eski_sku.get(anahtar)
        g = (s.get('images') or [None])[0]
        g_kimlik = str(g.get('id')) if g else ''
        urun = {
            'n': ad, 'b': marka_bul(s, ad, onceki, bilinen), 'c': kod, 'k': kategori_bul(s),
            'p': satis, 'l': liste,
            'd': maddeler(s.get('description') or s.get('short_description'), ad),
            'i': '', 'u': g_kimlik,
        }
        if g and onceki and onceki.get('u') == g_kimlik and onceki.get('i'):
            urun['i'] = onceki['i']                   # görsel değişmemiş: yeniden indirme
        elif g:
            indirilecek.append((urun, s.get('images'), onceki))
        yeni.append(urun)

    # güvenlik: anormal küçük bir çekim canlı uygulamayı boşaltmasın
    if len(yeni) < max(EN_AZ_URUN, int(len(eski) * EN_AZ_ORAN)):
        raise RuntimeError(f'Ürün sayısı anormal düşük: {len(yeni)} (önceki {len(eski)}). Hiçbir şey yazılmadı.')

    basarisiz, kararlar = 0, {}
    if indirilecek and not kuru:
        def indir(kayit):
            urun, galeri, onceki = kayit
            try:
                urun['i'], karar = gorsel_sec(galeri, onceki)
                kararlar[karar] = kararlar.get(karar, 0) + 1
                return True
            except Exception as e:                    # tek görsel hatası tüm güncellemeyi durdurmasın
                if onceki and onceki.get('i'):
                    urun['i'], urun['u'] = onceki['i'], onceki.get('u', '')
                print(f"  ! görsel inmedi: {urun['c']} ({e})", file=sys.stderr)
                return False
        with ThreadPoolExecutor(max_workers=4) as havuz:
            basarisiz = sum(1 for ok in havuz.map(indir, indirilecek) if not ok)
        print('Görsel kararları:', kararlar)
        gorselli = sum(1 for u in yeni if u['i'])
        if gorselli < len(yeni) * EN_AZ_GORSEL_ORANI:
            raise RuntimeError(f'Görsellerin çoğu indirilemedi ({gorselli}/{len(yeni)}). Hiçbir şey yazılmadı.')

    yeni.sort(key=lambda u: (tr_fold(u['n']), u['c']))

    # ---- karşılaştırma
    yeni_sku = {sku_anahtar(u['c']): u for u in yeni}
    eklenen = [u for k, u in yeni_sku.items() if k not in eski_sku]
    cikan = [p for k, p in eski_sku.items() if k not in yeni_sku]
    fiyat_deg = [(eski_sku[k], u) for k, u in yeni_sku.items()
                 if k in eski_sku and abs((eski_sku[k].get('p') or 0) - u['p']) >= 0.01]

    yeni_satir = veri_satiri(yeni)
    degisti = yeni_satir != satirlar[veri_idx]
    ozet = {
        'tarih': simdi.strftime('%Y-%m-%d %H:%M'), 'toplam': len(yeni), 'onceki': len(eski),
        'eklenen': len(eklenen), 'cikan': len(cikan), 'fiyat': len(fiyat_deg),
        'indirilen_gorsel': len(indirilecek) - basarisiz, 'degisti': degisti,
    }
    print(f"Site: {len(yeni)} ürün (önceki {len(eski)}) | eklenen {len(eklenen)} | çıkan {len(cikan)} | "
          f"fiyatı değişen {len(fiyat_deg)} | indirilecek görsel {len(indirilecek)} | "
          f"{'DEĞİŞİKLİK VAR' if degisti else 'değişiklik yok'}")
    if kuru:
        return ozet, eklenen, cikan, fiyat_deg

    if degisti:
        satirlar[veri_idx] = yeni_satir
        tarih_satiri = f"const DATA_DATE='{simdi.strftime('%Y-%m-%d')}';"
        t_idx = [i for i, l in enumerate(satirlar) if l.startswith('const DATA_DATE=')]
        if t_idx:
            satirlar[t_idx[0]] = tarih_satiri
        else:
            satirlar.insert(veri_idx + 1, tarih_satiri)
        cikti = '\n'.join(satirlar)
        # son kontrol: yazılacak sayfadan veri geri okunabiliyor mu?
        kontrol = [l for l in cikti.split('\n') if l.startswith('const PRODUCTS=')]
        assert len(kontrol) == 1 and len(json.loads(kontrol[0][15:].rstrip(';'))) == len(yeni)
        open(INDEX, 'w', encoding='utf-8').write(cikti)
        ozet['onbellek'] = damgala()

    rapor_yaz(ozet, eklenen, cikan, fiyat_deg)
    return ozet, eklenen, cikan, fiyat_deg


def para(v):
    return f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') + ' ₺'


def rapor_yaz(ozet, eklenen, cikan, fiyat_deg):
    gecmis = []
    if os.path.exists(GECMIS):
        try:
            gecmis = json.load(open(GECMIS, encoding='utf-8'))
        except Exception:
            gecmis = []
    gecmis.append({k: ozet[k] for k in ('tarih', 'toplam', 'eklenen', 'cikan', 'fiyat', 'degisti')})
    gecmis = gecmis[-60:]
    json.dump(gecmis, open(GECMIS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    s = ['# Son veri güncellemesi', '',
         f"**{ozet['tarih']}** (TSİ) — sitede stokta **{ozet['toplam']}** ürün "
         f"(önceki {ozet['onceki']}). Eklenen **{ozet['eklenen']}**, çıkan **{ozet['cikan']}**, "
         f"fiyatı değişen **{ozet['fiyat']}**."
         + ('' if ozet['degisti'] else ' Uygulama verisi aynı kaldı.'), '']
    if eklenen:
        s += ['## Eklenen ürünler', ''] + [f"- {u['n']} — {para(u['p'])}" for u in eklenen] + ['']
    if cikan:
        s += ['## Stoktan çıkan ürünler', ''] + [f"- {p['n']}" for p in cikan] + ['']
    if fiyat_deg:
        s += ['## Fiyatı değişen ürünler', '', '| Ürün | Eski | Yeni |', '|---|---:|---:|']
        s += [f"| {e['n']} | {para(e['p'])} | {para(u['p'])} |"
              for e, u in sorted(fiyat_deg, key=lambda x: tr_fold(x[1]['n']))] + ['']
    s += ['## Geçmiş', '', '| Tarih | Toplam | Eklenen | Çıkan | Fiyat değişen |', '|---|---:|---:|---:|---:|']
    s += [f"| {g['tarih']} | {g['toplam']} | {g['eklenen']} | {g['cikan']} | {g['fiyat']} |" for g in reversed(gecmis)]
    metin = '\n'.join(s) + '\n'
    open(RAPOR, 'w', encoding='utf-8').write(metin)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8').write(metin)


if __name__ == '__main__':
    try:
        if '--damga' in sys.argv:
            print('Önbellek adı:', damgala())
        else:
            calistir(kuru='--kuru' in sys.argv)
    except Exception as hata:
        print(f'HATA: {hata}', file=sys.stderr)
        sys.exit(1)
