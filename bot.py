"""
Tezkor OAK e'lonlar — oak.uz saytidagi dissertatsiya himoyasi e'lonlarini
avtomatik ravishda Telegram kanalga joylovchi bot.

Muhit o'zgaruvchilari (environment variables):
  BOT_TOKEN       — @BotFather bergan token (majburiy)
  CHANNEL_ID      — kanal: "@tezkor_oak_elonlar" yoki "-100..." (majburiy)
  STATE_FILE      — yuborilgan e'lonlar ro'yxati saqlanadigan fayl (default: state.json)
  FIRST_RUN_POST  — birinchi ishga tushganda nechta oxirgi e'lonni yuborish (default: 3)
  LOOP_INTERVAL   — 0 bo'lsa bir marta ishlaydi (GitHub Actions uchun),
                    masalan 600 bo'lsa har 10 daqiqada tekshiradi (VPS uchun)
"""

import copy
import html
import json
import logging
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
FIRST_RUN_POST = int(os.environ.get("FIRST_RUN_POST", "3"))
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "0"))
CHANNEL_SIGNATURE = os.environ.get("CHANNEL_SIGNATURE", "")  # masalan: @tezkor_oak_elonlar

BASE = "https://oak.uz"
SOURCES = {
    "/page/8": "Himoya haqidagi e’lonlar",
    "/page/31": "Mustaqil ilmiy kengash",
}
MAX_PAGES = 3          # oddiy tekshiruvda har bir bo'limdan nechta sahifa ko'riladi
START_DATE = os.environ.get("START_DATE", "01.09.2026")   # shu sanadan boshlab barcha e'lonlar chiqadi
DEEP_DAYS = int(os.environ.get("DEEP_DAYS", "60"))        # chuqur tekshiruv necha kun orqaga qaraydi
DEEP_MAX_PAGES = 200                                       # chuqur tekshiruvda sahifalar chegarasi
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "40"))
SILENT_AFTER_DAYS = 3  # shundan eski e'lonlar ovozsiz (bildirishnomasiz) yuboriladi
MAX_SEEN = 3000        # state faylda saqlanadigan ID lar soni

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TezkorOAKBot/1.0)"}
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
ID_RE = re.compile(r"/pages/(\d+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oak")


# ---------------- Sayt bilan ishlash ----------------

SITE_LOCALE = os.environ.get("SITE_LOCALE", "uz")   # uz = O'zbekcha (lotin), uzc = kirill, ru, en
_session = requests.Session()
_session.headers.update(HEADERS)
_locale_ready = False


def _ensure_locale() -> None:
    """Saytni O'zb (lotin) tiliga o'tkazadi — xuddi yuqoridagi "Til → O'zb" tugmasi kabi."""
    global _locale_ready
    if _locale_ready:
        return
    _locale_ready = True
    try:
        _session.get(f"{BASE}/setlocale/{SITE_LOCALE}", timeout=40)
        log.info("Sayt tili: %s", SITE_LOCALE)
    except requests.RequestException as e:
        log.warning("Sayt tilini o'zgartirib bo'lmadi: %s", e)


_page_cache: dict = {}


def fetch(url: str, tries: int = 3) -> str:
    if url in _page_cache:
        return _page_cache[url]
    html_text = _fetch(url, tries)
    if "/pages/" in url:                 # e'lon sahifasi: matn va PDF uchun bir marta yuklaymiz
        _page_cache[url] = html_text
    return html_text


def _fetch(url: str, tries: int = 3) -> str:
    _ensure_locale()
    for i in range(tries):
        try:
            r = _session.get(url, timeout=40)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            log.warning("Yuklab bo'lmadi (%s/%s) %s: %s", i + 1, tries, url, e)
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"Sahifa ochilmadi: {url}")


def clean_text(el) -> str:
    """HTML elementdan qatorlari saqlangan toza matn oladi."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for blk in el.find_all(["p", "div", "li"]):
        blk.append("\n")
    text = el.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _drop_comments(soup) -> None:
    """Sayt kodidagi yashirin izohlarni (masalan "Post Item Start") olib tashlaydi."""
    from bs4 import Comment, Declaration, Doctype, ProcessingInstruction
    for c in soup.find_all(string=lambda t: isinstance(t, (Comment, Declaration, Doctype, ProcessingInstruction))):
        c.extract()


def parse_list(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    _drop_comments(soup)
    items, seen = [], set()

    for h in soup.find_all(["h2", "h3", "h4"]):
        a = h.find("a", href=ID_RE)
        if not a:
            continue
        pid = int(ID_RE.search(a["href"]).group(1))
        if pid in seen:
            continue
        seen.add(pid)
        title = a.get_text(" ", strip=True)

        # E'lon blokini topish: sarlavhadan yuqoriga, matni yetarli bo'lgan ota-element
        container = h.parent
        for _ in range(4):
            if container is None or container.name in ("ul", "body"):
                break
            if len(container.get_text(strip=True)) > len(title) + 40:
                break
            container = container.parent
        if container is None:
            continue

        block = copy.copy(container)
        raw = block.get_text(" ", strip=True)
        dm = DATE_RE.search(raw)
        date = dm.group(1) if dm else ""

        # Sarlavha, sana va "Batafsil" havolalarini olib tashlaymiz
        for tag in block.find_all(["h2", "h3", "h4"]):
            tag.decompose()
        for link in block.find_all("a"):
            t = link.get_text(strip=True)
            if t == "Batafsil" or DATE_RE.fullmatch(t or "") or link.get("href") == "#":
                link.decompose()
        for img in block.find_all("img"):
            img.decompose()

        summary = clean_text(block)
        summary = "\n".join(l for l in summary.split("\n") if l and not DATE_RE.fullmatch(l))

        items.append({
            "id": pid,
            "title": title,
            "date": date,
            "summary": summary,
            "url": f"{BASE}/pages/{pid}",
        })
    return items


# ---------------- Telegram ----------------

# ---------- Kirill -> lotin (o'zbek alifbosi) ----------

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "’", "ь": "",
    "ы": "i", "э": "e", "ю": "yu", "я": "ya", "ў": "o‘", "қ": "q", "ғ": "g‘",
    "ҳ": "h",
}
_VOWELS = set("аеёиоуэюяўaeiou")


def to_latin(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        low = ch.lower()
        prev = text[i - 1].lower() if i > 0 else " "
        if low == "е":
            lat = "ye" if (not prev.isalpha() or prev in _VOWELS or prev in "ъь") else "e"
        elif low in _CYR:
            lat = _CYR[low]
        else:
            out.append(ch)
            continue
        if ch.isupper() and lat:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            lat = lat.upper() if (nxt.isupper() and len(lat) > 1) else lat[0].upper() + lat[1:]
        out.append(lat)
    return "".join(out)


def is_uzbek_cyrillic(text: str) -> bool:
    """O'zbek kirilliga xos harflar (ў қ ғ ҳ) bo'lsa — o'zbekcha; ruscha matnda ular bo'lmaydi."""
    return any(ch in "ўқғҳЎҚҒҲ" for ch in text)


# ---------- E'lon matnidan maydonlarni ajratib olish ----------

SHIFR_RE = re.compile(r"(\d{2}\.\d{2}\.\d{2})\s*[–—-]?\s*(.+?)(?:\s*\(|\.?\s*$)")
NAME_RE = re.compile(r"^(.+?)ning\s+(?:falsafa|fan)\s+doktori", re.IGNORECASE)
FIELD_LAT_RE = re.compile(r"\(([\w‘’'ʻ` -]+?)\s+fanlari\)", re.IGNORECASE)


def field_tag(field: str) -> str:
    """'qishloq xo‘jaligi' -> 'qishloq_xojaligi', 'fizika-matematika' -> 'fizika_matematika'"""
    t = re.sub(r"[‘’'ʻ`]", "", field.lower())
    return re.sub(r"[\s-]+", "_", t).strip("_")
RANKS = ("professor", "dotsent", "katta ilmiy xodim", "akademik")


def short_person(value: str) -> str:
    """'Mullaxo‘jayeva Karomat Toshxo‘jayevna, ..., dotsent' -> 'Mullaxo‘jayeva K.T., dotsent'"""
    parts = [p.strip(" .") for p in value.split(",") if p.strip(" .")]
    if not parts:
        return value
    words = parts[0].split()
    name = parts[0]
    if len(words) >= 2:
        initials = ""
        for w in words[1:3]:
            m = re.match(r"(Sh|Ch|O‘|G‘|O'|G'|Yo|Yu|Ya)", w)
            initials += (m.group(1) if m else w[0]) + "."
        name = f"{words[0]} {initials}"
    rank = parts[-1] if len(parts) > 1 and parts[-1].lower() in RANKS else ""
    return f"{name}, {rank}" if rank else name


def extract(item: dict) -> dict:
    title = to_latin(item["title"])
    summary = to_latin(item.get("full") or item["summary"])
    info = {"title": title, "degree": "", "field": "", "name": "", "topic": "",
            "shifr": "", "supervisor": "", "institution": "", "council": "",
            "date": item.get("date", "")}

    if "DSc" in title:
        info["degree"] = "DSc"
    elif "PhD" in title:
        info["degree"] = "PhD"

    m = NAME_RE.search(title)
    info["name"] = m.group(1).strip() if m else ""

    fm = FIELD_LAT_RE.search(summary)
    if fm:
        info["field"] = fm.group(1).strip()

    lines = summary.split("\n")
    for n, line in enumerate(lines):
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        lab, value = label.lower(), value.strip()
        if "mavzu" in lab and "shifr" in lab and not info["topic"]:
            # Shifr ba'zan keyingi qatorda turadi: 05.05.07-“...”
            if not SHIFR_RE.search(value) and n + 1 < len(lines) and re.match(r"\s*\d{2}\.\d{2}\.\d{2}", lines[n + 1]):
                value = value.rstrip(" ,") + ", " + lines[n + 1].strip()
            sm = SHIFR_RE.search(value)
            if sm:
                spec = re.sub(r"\s+ixtisosligi$", "", sm.group(2).strip(" “”\"«»„"), flags=re.IGNORECASE)
                info["shifr"] = f"{sm.group(1)} – {spec}"
                value = value[:sm.start()]
            info["topic"] = value.strip(" ,.;").strip("“”\"«»„ ")
        elif ("rahbar" in lab or "maslahatchi" in lab) and not info["supervisor"]:
            info["supervisor"] = short_person(value)
        elif (re.search(r"\bik\b", lab) or ("ilmiy kengash" in lab and ("faoliyat" in lab or "raqam" in lab))) \
                and not info["council"]:
            name = re.split(r",?\s*(?:DSc|PhD|DSC|PHD)\s*[.\d]", value)[0]
            info["council"] = name.strip(" ,.;")
        elif "bajarilgan" in lab or "bajarilayotgan" in lab:
            info["institution"] = info["institution"] or value.strip(" .")
    return info


def build_message(item: dict, section: str, pdf_link: str | None = None,
                  topic_limit: int | None = None) -> str:
    e = html.escape
    info = extract(item)
    if topic_limit is not None and len(info["topic"]) > topic_limit:
        info["topic"] = (info["topic"][:topic_limit].rsplit(" ", 1)[0] + "…") if topic_limit > 0 else ""

    parts = [f"🎓 <b>{e(info['title'])}</b>"]

    body = []
    if info["topic"]:
        body.append(f"📖 «{e(info['topic'])}»")
    if info["shifr"]:
        body.append(f"🔢 {e(info['shifr'])}")
    if info["supervisor"]:
        body.append(f"👨‍🏫 Ilmiy rahbar: {e(info['supervisor'])}")
    if info["institution"]:
        body.append(f"🏛 {e(info['institution'])}")
    if info["council"] and info["council"] != info["institution"]:
        body.append(f"🏢 Ilmiy kengash: {e(info['council'])}")
    parts.append("\n".join(body))

    tags = []
    if info["degree"]:
        tags.append("#" + info["degree"])
    if info["field"]:
        tags.append("#" + field_tag(info["field"]))
    if section != SOURCES["/page/8"]:
        tags.append("#mustaqil_kengash")

    link = f'🔗 <a href="{item["url"]}">To‘liq e’lon — oak.uz</a>'
    if pdf_link:
        link = f'📄 <a href="{pdf_link}">E’lon to‘g‘risida ma’lumotnoma olish (PDF)</a>\n' + link
    if info["date"]:
        link = f"📅 E’lon sanasi: <b>{e(info['date'])}</b>\n" + link
    parts.append(link + ("\n" + " ".join(tags) if tags else ""))
    if CHANNEL_SIGNATURE:
        parts.append(f"📢 {e(CHANNEL_SIGNATURE)}")
    return "\n\n".join(parts)


def _tg(method: str, data: dict, files: dict | None = None) -> dict:
    """Telegram Bot API so'rovi (limitga tushsa kutib, qayta urinadi)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(5):
        r = requests.post(url, data=data, files=files, timeout=60)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            log.info("Telegram limit, %s soniya kutamiz", wait)
            time.sleep(wait + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram xatosi ({method}): {r.status_code} {r.text}")
        return r.json().get("result", {})
    raise RuntimeError(f"Telegram'ga yuborib bo'lmadi ({method})")


def send_message(text: str, silent: bool = False) -> int:
    res = _tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": True, "disable_notification": silent})
    return res.get("message_id", 0)


# ---------------- Post rasmi (avtomatik chiziladi) ----------------

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

CAPTION_LIMIT = 1024
CARD_TITLE = "TEZKOR OAK HIMOYA E’LONLARI"
BW, BH = 1920, 1080      # yakuniy o'lcham
S = 2                    # silliqlik uchun 2 baravar katta chizib, keyin kichraytiramiz
THEMES = {
    #        fon (yuqori)    fon (past)       nur            aksent
    "PhD": ((7, 22, 52), (18, 70, 140), (70, 150, 255), (255, 196, 87)),
    "DSc": ((30, 8, 48), (98, 30, 118), (205, 95, 255), (255, 206, 120)),
    "": ((8, 36, 44), (20, 100, 108), (60, 210, 200), (255, 196, 87)),
}
DEGREE_NAMES = {"PhD": "Falsafa doktori", "DSc": "Fan doktori"}
_FONTS = {
    "bold": ["fonts/Poppins-Bold.ttf", "fonts/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "medium": ["fonts/Poppins-Medium.ttf", "fonts/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["fonts/Poppins-Regular.ttf", "fonts/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}
_font_cache: dict = {}


def _font(size: int, weight: str = "regular"):
    key = (size, weight)
    if key not in _font_cache:
        for p in _FONTS[weight]:
            if Path(p).exists():
                _font_cache[key] = ImageFont.truetype(p, size * S)
                break
        else:
            _font_cache[key] = ImageFont.load_default(size * S)
    return _font_cache[key]


def _wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=font) <= width * S:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, width, max_lines, sizes, weight):
    for size in sizes:
        font = _font(size, weight)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= max_lines:
            return font, size, lines
    size = sizes[-1]
    font = _font(size, weight)
    lines = _wrap(draw, text, font, width)[:max_lines]
    lines[-1] = lines[-1].rstrip(" ,.") + "…"
    return font, size, lines


def _spaced(draw, xy, text, font, fill, tracking):
    """Harflar orasi kengaytirilgan matn."""
    x, y = xy
    for ch in text:
        draw.text((x * S, y * S), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) / S + tracking
    return x


def _spaced_len(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) / S + tracking for ch in text) - tracking


def _cap_icon(draw, x, y, size, bg, fg):
    """Bitiruvchi qalpoqchasi belgisi."""
    k = size / 72
    P = lambda px, py: ((x + px * k) * S, (y + py * k) * S)  # noqa: E731
    draw.rounded_rectangle((x * S, y * S, (x + size) * S, (y + size) * S), radius=int(20 * k * S), fill=bg)
    draw.polygon([P(20, 38), P(52, 38), P(52, 50), P(36, 56), P(20, 50)], fill=fg)
    draw.polygon([P(36, 16), P(62, 30), P(36, 44), P(10, 30)], fill=fg)
    draw.line([P(58, 32), P(58, 50)], fill=fg, width=int(4 * k * S))
    draw.ellipse([P(54, 48), P(62, 56)], fill=fg)


def make_card(info: dict) -> bytes | None:
    from PIL import ImageFilter

    shifr = info.get("shifr", "")
    council = info.get("council") or info.get("institution", "")
    if not shifr and not council:
        return None
    code, _, spec = shifr.partition(" – ")
    degree = info.get("degree", "")
    top, bottom, glow, accent = THEMES.get(degree, THEMES[""])
    W, H = BW * S, BH * S
    white = (255, 255, 255)
    soft = (214, 226, 244)

    # 1) Diagonal gradient fon
    vert = Image.linear_gradient("L").resize((W, H))
    horiz = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).transpose(
        Image.Transpose.FLIP_LEFT_RIGHT).resize((W, H))
    grad = Image.blend(vert, horiz, 0.45)
    img = Image.composite(Image.new("RGB", (W, H), bottom), Image.new("RGB", (W, H), top), grad).convert("RGBA")

    # 2) Yumshoq nur va bezaklar
    fx = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fx)
    fd.ellipse((W - 900 * S, -500 * S, W + 300 * S, 650 * S), fill=glow + (110,))
    fd.ellipse((-400 * S, H - 300 * S, 500 * S, H + 500 * S), fill=glow + (55,))
    fx = fx.filter(ImageFilter.GaussianBlur(160 * S))
    img = Image.alpha_composite(img, fx)

    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    cx, cy = W - 170 * S, 120 * S
    for r, a, w in ((520, 26, 3), (400, 34, 3), (280, 44, 2)):
        dd.ellipse((cx - r * S, cy - r * S, cx + r * S, cy + r * S), outline=(255, 255, 255, a), width=w * S)
    for gx in range(12):                       # nuqtali to'r
        for gy in range(5):
            px_, py_ = (1450 + gx * 34) * S, (880 + gy * 34) * S
            dd.ellipse((px_ - 3 * S, py_ - 3 * S, px_ + 3 * S, py_ + 3 * S), fill=(255, 255, 255, 30))
    img = Image.alpha_composite(img, deco)

    d = ImageDraw.Draw(img)
    PAD = 110

    # 3) Sarlavha qismi
    _cap_icon(d, PAD, 74, 80, accent, top)
    _spaced(d, (PAD + 108, 76), CARD_TITLE, _font(34, "bold"), white, 3)
    sub_f = _font(24, "regular")
    sub_t = "Dissertatsiya himoyasi haqida e’lon"
    d.text(((PAD + 108) * S, 124 * S), sub_t, font=sub_f, fill=soft)
    if info.get("date"):
        sx = PAD + 108 + d.textlength(sub_t, font=sub_f) / S
        d.text(((sx + 14) * S, 124 * S), "•", font=sub_f, fill=soft)
        d.text(((sx + 40) * S, 124 * S), info["date"], font=_font(24, "bold"), fill=accent)

    if degree:
        bf, sf = _font(40, "bold"), _font(20, "medium")
        sub = DEGREE_NAMES.get(degree, "")
        bw = max(d.textlength(degree, font=bf), d.textlength(sub, font=sf)) / S + 64
        x0 = BW - PAD - bw
        d.rounded_rectangle((x0 * S, 70 * S, (BW - PAD) * S, 162 * S), radius=26 * S, fill=accent)
        d.text(((x0 + bw / 2) * S, 104 * S), degree, font=bf, fill=top, anchor="mm")
        d.text(((x0 + bw / 2) * S, 140 * S), sub, font=sf, fill=top, anchor="mm")

    # 4) Asosiy panel — avval o'lchaymiz, keyin chizamiz
    inner = BW - 2 * PAD - 2 * 72
    ops, h = [], 0
    if shifr:
        ops.append(("label", "IXTISOSLIK", h)); h += 46
        if code and spec:
            ops.append(("code", code, h)); h += 118
            text = spec
        else:
            text = shifr
        font, size, lines = _fit(d, text, inner, 3, [64, 58, 52, 46, 42], "bold")
        for ln in lines:
            ops.append(("spec", (ln, font), h)); h += int(size * 1.22)
        h += 34
        ops.append(("divider", None, h)); h += 42
    if council:
        ops.append(("label", "ILMIY KENGASH", h)); h += 50
        room = 2 if shifr else 4
        font, size, lines = _fit(d, council, inner, room, [46, 42, 38, 34], "regular")
        for ln in lines:
            ops.append(("council", (ln, font), h)); h += int(size * 1.35)

    panel_h = h + 2 * 60
    area_top, area_bottom = 210, 930
    py0 = area_top + max(0, (area_bottom - area_top - panel_h) // 2)
    glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glass)
    gd.rounded_rectangle((PAD * S, py0 * S, (BW - PAD) * S, (py0 + panel_h) * S),
                         radius=36 * S, fill=(255, 255, 255, 20), outline=(255, 255, 255, 46), width=2 * S)
    gd.rounded_rectangle((PAD * S, (py0 + 40) * S, (PAD + 8) * S, (py0 + panel_h - 40) * S),
                         radius=4 * S, fill=accent + (255,))
    img = Image.alpha_composite(img, glass)
    d = ImageDraw.Draw(img)

    x = PAD + 72
    y0 = py0 + 60
    for kind, val, off in ops:
        y = y0 + off
        if kind == "label":
            _spaced(d, (x, y), val, _font(24, "medium"), accent, 4)
        elif kind == "code":
            d.text((x * S, (y - 14) * S), val, font=_font(100, "bold"), fill=white)
        elif kind == "spec":
            d.text((x * S, y * S), val[0], font=val[1], fill=white)
        elif kind == "divider":
            d.rounded_rectangle((x * S, y * S, (x + 150) * S, (y + 6) * S), radius=3 * S, fill=accent)
        elif kind == "council":
            d.text((x * S, y * S), val[0], font=val[1], fill=soft)

    # 5) Pastki qator
    fy = 990
    ff = _font(28, "medium")
    if CHANNEL_SIGNATURE:
        d.text((PAD * S, fy * S), CHANNEL_SIGNATURE, font=ff, fill=white, anchor="lm")
    else:
        d.text((PAD * S, fy * S), "oak.uz", font=ff, fill=white, anchor="lm")
    if info.get("field"):
        label = info["field"][:1].upper() + info["field"][1:] + " fanlari"
        lf = _font(26, "medium")
        lw = d.textlength(label, font=lf) / S + 56
        x1 = BW - PAD
        d.rounded_rectangle(((x1 - lw) * S, (fy - 30) * S, x1 * S, (fy + 30) * S),
                            radius=30 * S, outline=(255, 255, 255, 140), width=2 * S)
        d.text(((x1 - lw / 2) * S, fy * S), label, font=lf, fill=white, anchor="mm")

    final = img.convert("RGB").resize((BW, BH), Image.LANCZOS)
    buf = BytesIO()
    final.save(buf, "JPEG", quality=95, subsampling=0, optimize=True)
    return buf.getvalue()


# ---------------- E'lon ma'lumotnomasi (PDF) ----------------

_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table", "tr",
               "section", "article", "blockquote", "center"}
_SKIP_TAGS = {"script", "style", "img", "button", "form", "iframe", "svg", "noscript", "input"}


def _compact(t: str) -> str:
    return re.sub(r"\s+", "", t)


def _to_markup(el) -> str:
    """HTML elementni reportlab tushunadigan oddiy belgilashga o'giradi (qalin, qator, paragraf)."""
    from bs4 import Comment, NavigableString
    out = []
    for node in el.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            out.append(html.escape(str(node), quote=False))
        elif node.name in _SKIP_TAGS:
            continue
        elif node.name in ("b", "strong"):
            out.append("<b>" + _to_markup(node) + "</b>")
        elif node.name in ("i", "em"):
            out.append("<i>" + _to_markup(node) + "</i>")
        elif node.name == "br":
            out.append("<br/>")
        elif node.name in _BLOCK_TAGS:
            out.append("\n\n" + _to_markup(node) + "\n\n")
        else:
            out.append(_to_markup(node))
    return "".join(out)


_NAV_MARKERS = ("boshmenyu", "бошменю", "asosiysahifa", "асосийсаҳифа", "ishonchtelefoni",
                "ишончтелефони", "saytxaritasi", "сайтхаритаси", "главнаястраница")


def fetch_detail(item: dict):
    """E'lonning to'liq sahifasidan asosiy matn blokini topadi."""
    soup = BeautifulSoup(fetch(item["url"]), "html.parser")
    _drop_comments(soup)
    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    key = _compact(item["summary"])[:30]
    if not key:
        return None
    best = None
    for el in soup.find_all(["div", "article", "section", "td", "main"]):
        txt = _compact(el.get_text())
        if key in txt and (best is None or len(txt) < best[0]):
            best = (len(txt), el)
    if not best:
        return None
    # Bo'limlar alohida bloklarda bo'lsa ham hammasini olish uchun yuqoriga kengayamiz,
    # menyu / yon panel / "Asosiy sahifa" yo'li qo'shilib qoladigan joyda to'xtaymiz
    el = best[1]
    while el.parent is not None and el.parent.name not in ("body", "html", "[document]"):
        ptxt = _compact(el.parent.get_text()).lower()
        if any(m in ptxt for m in _NAV_MARKERS):
            break
        el = el.parent
    return el


def make_pdf(item: dict) -> bytes | None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
    from reportlab.lib.fonts import addMapping

    # Times New Roman ko'rinishi: Liberation Serif (TNR bilan bir xil o'lchamli bepul shrift),
    # topilmasa — DejaVu Sans
    def pick(*names):
        for n in names:
            for d in ("fonts", "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/dejavu"):
                if Path(d, n).exists():
                    return str(Path(d, n))
        return None

    faces = {
        "TNR": pick("LiberationSerif-Regular.ttf", "DejaVuSans.ttf"),
        "TNR-B": pick("LiberationSerif-Bold.ttf", "DejaVuSans-Bold.ttf"),
        "TNR-I": pick("LiberationSerif-Italic.ttf", "LiberationSerif-Regular.ttf", "DejaVuSans.ttf"),
        "TNR-BI": pick("LiberationSerif-BoldItalic.ttf", "LiberationSerif-Bold.ttf", "DejaVuSans-Bold.ttf"),
    }
    if not all(faces.values()):
        log.warning("PDF uchun shrift topilmadi")
        return None
    if "TNR" not in pdfmetrics.getRegisteredFontNames():
        for name, path in faces.items():
            pdfmetrics.registerFont(TTFont(name, path))
        addMapping("TNR", 0, 0, "TNR"); addMapping("TNR", 1, 0, "TNR-B")
        addMapping("TNR", 0, 1, "TNR-I"); addMapping("TNR", 1, 1, "TNR-BI")

    body = ParagraphStyle("body", fontName="TNR", fontSize=12, leading=16.5, alignment=TA_JUSTIFY,
                          spaceAfter=5)
    title_st = ParagraphStyle("title", fontName="TNR-B", fontSize=14, leading=19, alignment=TA_CENTER,
                              spaceAfter=12)
    meta = ParagraphStyle("meta", fontName="TNR", fontSize=10, leading=13, textColor=colors.HexColor("#5b6b80"))

    # Matn: to'liq sahifadan, topilmasa — e'lonlar ro'yxatidagi qisqa matndan
    paragraphs = []
    container = None
    try:
        container = fetch_detail(item)
    except Exception as ex:  # noqa: BLE001
        log.warning("To'liq e'lon sahifasi olinmadi: %s", ex)
    if container is not None:
        for chunk in _to_markup(container).split("\n\n"):
            chunk = re.sub(r"[ \t\r\n\xa0]+", " ", chunk).strip()
            chunk = re.sub(r"^(<br/>\s*)+|(\s*<br/>)+$", "", chunk).strip()
            if chunk and re.sub(r"<[^>]+>", "", chunk).strip():
                paragraphs.append(chunk)
    if not paragraphs:
        paragraphs = [html.escape(line, quote=False) for line in item["summary"].split("\n") if line.strip()]

    # O'zbekcha kirill matn bo'lsa — lotinga o'giramiz (ruscha va boshqa tillar o'zgarmaydi)
    pdf_title = item["title"]
    if is_uzbek_cyrillic(" ".join(paragraphs) + pdf_title):
        paragraphs = [to_latin(p) for p in paragraphs]
        pdf_title = to_latin(pdf_title)

    story = [
        Paragraph(f"Manba: oak.uz — Oliy attestatsiya komissiyasi rasmiy veb-sayti"
                  + (f" &nbsp;•&nbsp; E’lon sanasi: {item['date']}" if item.get("date") else ""), meta),
        Paragraph(f'Havola: <link href="{item["url"]}" color="#1d4e89">{item["url"]}</link>', meta),
        Spacer(1, 3 * mm),
        HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#c9d3e0")),
        Spacer(1, 5 * mm),
    ]
    plain = lambda t: _compact(re.sub(r"<[^>]+>", "", html.unescape(t)))  # noqa: E731
    head = plain(pdf_title)[:25]
    if not any(head in plain(p) for p in paragraphs[:3]):
        story.append(Paragraph(html.escape(pdf_title, quote=False), title_st))
    for n, p in enumerate(paragraphs):
        style = title_st if n < 3 and head in plain(p) else body
        try:
            story.append(Paragraph(p, style))
        except Exception:  # noqa: BLE001 — buzilgan belgilash bo'lsa, oddiy matn
            story.append(Paragraph(html.escape(re.sub(r"<[^>]+>", " ", p), quote=False), body))

    footer = "Tezkor OAK himoya e’lonlari" + (f"  •  {CHANNEL_SIGNATURE}" if CHANNEL_SIGNATURE else "")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("TNR", 9.5)
        canvas.setFillColor(colors.HexColor("#7a8799"))
        canvas.drawString(20 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"{doc.page}-bet")
        canvas.restoreState()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=pdf_title, author="oak.uz", subject="Dissertatsiya himoyasi haqida e’lon")
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def _visible_len(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def send_photo(photo: bytes, caption: str, silent: bool = False) -> int:
    res = _tg("sendPhoto", {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML",
                            "disable_notification": silent},
              files={"photo": ("post.jpg", photo, "image/jpeg")})
    return res.get("message_id", 0)


# ---------------- Ma'lumotnomani GitHub Releases'ga joylash ----------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")   # Actions o'zi beradi: "user/repo"
_release_ids: dict = {}


def _gh(method: str, url: str, **kw):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    headers.update(kw.pop("headers", {}))
    return requests.request(method, url, headers=headers, timeout=60, **kw)


def _release_id(tag: str) -> int:
    """Har oy uchun bitta release (masalan pdf-2026-10) — ma'lumotnomalar shu yerda saqlanadi."""
    if tag in _release_ids:
        return _release_ids[tag]
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    r = _gh("GET", f"{api}/tags/{tag}")
    if r.status_code == 404:
        r = _gh("POST", api, json={
            "tag_name": tag,
            "name": f"E’lon ma’lumotnomalari — {tag[4:]}",
            "body": "Tezkor OAK himoya e’lonlari: oak.uz e’lonlarining PDF ma’lumotnomalari.",
            "make_latest": "false",
        })
    r.raise_for_status()
    _release_ids[tag] = r.json()["id"]
    return _release_ids[tag]


def upload_pdf(pdf: bytes, filename: str, month: str = "") -> str | None:
    """PDF ni yuklaydi va to'g'ridan-to'g'ri yuklab olish havolasini qaytaradi."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GITHUB_TOKEN yoki GITHUB_REPOSITORY yo'q — ma'lumotnoma havolasi qo'shilmaydi")
        return None
    tag = "pdf-" + (month or time.strftime("%Y-%m"))
    rid = _release_id(tag)
    url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{rid}/assets"
    r = _gh("POST", url, params={"name": filename}, data=pdf, headers={"Content-Type": "application/pdf"})
    if r.status_code == 422:      # bu e'lonning ma'lumotnomasi allaqachon yuklangan — o'shandan foydalanamiz
        return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{filename}"
    r.raise_for_status()
    return r.json()["browser_download_url"]


def enrich(item: dict) -> None:
    """Ma'lumotlarni ro'yxatdagi qisqa matndan emas, e'lonning to'liq sahifasidan olamiz."""
    if item.get("full"):
        return
    try:
        el = fetch_detail(item)
        if el is not None:
            item["full"] = clean_text(copy.copy(el))
    except Exception as ex:  # noqa: BLE001
        log.warning("To'liq sahifa o'qilmadi (%s) — ro'yxatdagi matndan foydalanamiz", ex)


def fit_caption(item: dict, section: str, pdf_link: str | None) -> tuple[str, bool]:
    """Izoh 1024 belgiga sig'masa, mavzuni qisqartiramiz — post baribir rasm bilan chiqsin."""
    for lim in (None, 500, 320, 200, 120, 0):
        t = build_message(item, section, pdf_link, topic_limit=lim)
        if _visible_len(t) <= CAPTION_LIMIT:
            return t, True
    return build_message(item, section, pdf_link), False


def publish(item: dict, section: str, silent: bool = False) -> None:
    enrich(item)
    # 1) Ma'lumotnoma: PDF yasab yuklaymiz (xato bo'lsa — post havolasiz chiqadi)
    pdf_link = None
    try:
        pdf = make_pdf(item)
        if pdf:
            info = extract(item)
            surname = re.sub(r"[^A-Za-z0-9]+", "", (info["name"].split() or ["elon"])[0]) or "elon"
            d = parse_date(item.get("date", ""))
            month = f"{d.year}-{d.month:02d}" if d else ""
            pdf_link = upload_pdf(pdf, f"OAK_elon_{item['id']}_{surname}.pdf", month)
    except Exception as ex:  # noqa: BLE001
        log.error("Ma'lumotnoma tayyorlanmadi: %s", ex)

    # 2) Post
    text, fits = fit_caption(item, section, pdf_link)
    if fits:
        try:
            card = make_card(extract(item))
            if card:
                send_photo(card, text, silent)
                return
        except Exception as ex:  # noqa: BLE001
            log.error("Rasm bilan yuborilmadi (%s) — matn ko'rinishida yuboramiz", ex)
    send_message(text, silent)


# ---------------- Holat (state) ----------------

def parse_date(d: str) -> date | None:
    try:
        return datetime.strptime(d, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def today_uz() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5)).date()


STATE: dict = {}


def load_state() -> bool:
    """state.json ni o'qiydi. Fayl bo'lmasa False qaytaradi."""
    global STATE
    if STATE_FILE.exists():
        STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return True
    STATE = {"seen": []}
    return False


def save_state(seen) -> None:
    STATE["seen"] = sorted(set(seen))[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), encoding="utf-8")


def migrate_skipped(seen: set[int]) -> None:
    """Bot ilk ishga tushganda kanalga chiqarmay "ko'rilgan" deb belgilagan e'lonlarni
    git tarixidan aniqlab, ularni qayta navbatga qo'yadi (bir martalik)."""
    if STATE.get("migrated_v2"):
        return
    STATE["migrated_v2"] = True
    try:
        commits = subprocess.run(["git", "log", "--reverse", "--format=%H", "--", str(STATE_FILE)],
                                 capture_output=True, text=True, check=True).stdout.split()
        if not commits:
            return
        first = json.loads(subprocess.run(["git", "show", f"{commits[0]}:{STATE_FILE}"],
                                          capture_output=True, text=True, check=True).stdout)
        ids = sorted(first.get("seen", []))
        skipped = set(ids[:-FIRST_RUN_POST]) if FIRST_RUN_POST > 0 else set(ids)
        seen -= skipped
        log.info("Ilk ishga tushishda o'tkazib yuborilgan %d ta e'lon qayta navbatga qo'yildi", len(skipped))
    except Exception as e:  # noqa: BLE001
        log.warning("Git tarixidan o'qib bo'lmadi (%s) — eski o'tkazib yuborilganlar qo'lda yuboriladi", e)


def collect(seen: set[int], deep: bool) -> list[tuple[dict, str]]:
    """Kanalda hali chiqmagan, START_DATE dan keyingi e'lonlarni yig'adi.
    Oddiy tekshiruv: har bo'limning 1-3 sahifasi.
    Chuqur tekshiruv: START_DATE gacha (ko'pi bilan DEEP_DAYS kun) barcha sahifalar —
    kech joylangan, eski sanali e'lonlarni ham topish uchun."""
    start = parse_date(START_DATE) or date(2000, 1, 1)
    bound = max(start, today_uz() - timedelta(days=DEEP_DAYS)) if deep else start
    limit = DEEP_MAX_PAGES
    found = {}
    for path, section in SOURCES.items():
        for page in range(1, limit + 1):
            url = f"{BASE}{path}" + (f"?page={page}" if page > 1 else "")
            try:
                items = parse_list(fetch(url))
            except RuntimeError as e:
                log.error(e)
                break
            if not items:
                if page == 1:
                    log.warning("%s sahifasida e'lon topilmadi — sayt tuzilishi o'zgargan bo'lishi mumkin", url)
                break
            known = 0
            for i in items:
                d = parse_date(i["date"])
                if i["id"] in seen:
                    known += 1
                    continue
                if (d is not None and d < start) or (d is None and page > 1):
                    continue
                found.setdefault(i["id"], (i, section))
            dates = [parse_date(i["date"]) for i in items if parse_date(i["date"])]
            if dates and min(dates) < bound:      # chegaradan o'tdik
                break
            # Oddiy tekshiruvda 3-sahifadan keyin ham davom etamiz, agar sahifadagi e'lonlarning
            # birortasi ham tanish bo'lmasa — demak qisqa vaqtda juda ko'p e'lon joylangan
            if not deep and page >= MAX_PAGES and known > 0:
                break
            if not deep and page >= MAX_PAGES:
                log.info("%s: %d-sahifada ham hammasi yangi — keyingi sahifani tekshiramiz", path, page)
            if deep:
                time.sleep(0.4)                   # saytga ortiqcha yuk bermaslik uchun
    # Eskidan yangiga: avval sana, keyin raqam bo'yicha
    return sorted(found.values(),
                  key=lambda x: (parse_date(x[0]["date"]) or date.max, x[0]["id"]))


FORCE_IDS = os.environ.get("FORCE_IDS", "")
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


def item_from_page(pid: int) -> tuple[dict, str] | None:
    """E'lonni to'g'ridan-to'g'ri o'z sahifasidan o'qiydi (qo'lda yuborish uchun)."""
    url = f"{BASE}/pages/{pid}"
    soup = BeautifulSoup(fetch(url), "html.parser")
    _drop_comments(soup)

    section = SOURCES["/page/8"]
    title, date_s = "", ""
    bc = soup.select_one("[class*=breadcrumb]")
    if bc:
        if bc.find("a", href=re.compile(r"/page/31\b")):
            section = SOURCES["/page/31"]
        lis = bc.find_all("li") or [bc]
        last = lis[-1].get_text(" ", strip=True)
        dm = DATE_RE.search(last)
        date_s = dm.group(1) if dm else ""
        title = DATE_RE.sub("", last).replace("()", "").strip()

    best = None
    for el in soup.find_all(["div", "article", "section", "td", "main"]):
        low = el.get_text(" ", strip=True).lower()
        if ("мавзуси" in low or "mavzusi" in low) and (best is None or len(low) < best[0]):
            best = (len(low), el)
    if not best:
        log.error("%s sahifasida e'lon matni topilmadi", url)
        return None
    lines = clean_text(copy.copy(best[1])).split("\n")
    start = next((n for n, l in enumerate(lines) if "мавзуси" in l.lower() or "mavzusi" in l.lower()), 0)
    summary = "\n".join(lines[start:start + 12])
    if not title:
        title = " ".join(lines[:start]).strip() or f"E’lon №{pid}"
    title = re.sub(r"\s+", " ", title)
    return {"id": pid, "title": title, "date": date_s, "summary": summary, "url": url}, section


def is_silent(item: dict) -> bool:
    d = parse_date(item.get("date", ""))
    return bool(d and (today_uz() - d).days > SILENT_AFTER_DAYS)


def run_once() -> None:
    existed = load_state()
    seen = set(STATE.get("seen", []))
    if existed:
        migrate_skipped(seen)

    # 1) Qo'lda ko'rsatilgan e'lonlar (Actions → Run workflow → "ids" maydoni)
    for raw in re.findall(r"\d+", FORCE_IDS):
        pid = int(raw)
        try:
            got = item_from_page(pid)
            if got:
                publish(*got, silent=is_silent(got[0]))
                log.info("Qo'lda yuborildi: %s", got[0]["url"])
                seen.add(pid)
                save_state(seen)
                time.sleep(3)
        except Exception as e:  # noqa: BLE001
            log.error("%s raqamli e'lonni yuborib bo'lmadi: %s", pid, e)

    # 2) Chuqur tekshiruv: navbat qolgan bo'lsa, qo'lda ishga tushirilganda yoki har soatda bir marta
    deep = STATE.get("backlog", False) or MANUAL_RUN or not existed or time.gmtime().tm_min < 15
    items = collect(seen, deep)
    batch = items[:MAX_POSTS_PER_RUN]
    log.info("%s tekshiruv: %d ta yangi e'lon, shundan hozir %d tasi yuboriladi",
             "Chuqur" if deep else "Oddiy", len(items), len(batch))

    sent = 0
    for item, section in batch:
        try:
            publish(item, section, silent=is_silent(item))
            log.info("Yuborildi: %s (%s)", item["url"], item["date"])
        except RuntimeError as e:
            log.error(e)
            break
        seen.add(item["id"])
        sent += 1
        save_state(seen)   # har yuborishdan keyin saqlaymiz — takror bo'lmasligi uchun
        time.sleep(3)

    STATE["backlog"] = len(items) > sent
    save_state(seen)
    if STATE["backlog"]:
        log.info("Navbatda yana %d ta e'lon bor — keyingi tekshiruvda davom etadi", len(items) - sent)


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit("BOT_TOKEN va CHANNEL_ID o'zgaruvchilarini kiriting")
    if LOOP_INTERVAL > 0:
        while True:
            try:
                run_once()
            except Exception as e:  # noqa: BLE001
                log.exception("Kutilmagan xato: %s", e)
            time.sleep(LOOP_INTERVAL)
    else:
        run_once()


if __name__ == "__main__":
    main()
