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
import time
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
MAX_PAGES = 3          # har bir bo'limdan nechta sahifagacha orqaga qarash
MAX_SEEN = 3000        # state faylda saqlanadigan ID lar soni

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TezkorOAKBot/1.0)"}
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
ID_RE = re.compile(r"/pages/(\d+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oak")


# ---------------- Sayt bilan ishlash ----------------

def fetch(url: str, tries: int = 3) -> str:
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
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


def parse_list(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
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


# ---------- E'lon matnidan maydonlarni ajratib olish ----------

SHIFR_RE = re.compile(r"(\d{2}\.\d{2}\.\d{2})\s*[–—-]\s*(.+?)(?:\s*\(|\.?\s*$)")
NAME_RE = re.compile(r"^(.+?)ning\s+(?:falsafa|fan)\s+doktori", re.IGNORECASE)
FIELD_LAT_RE = re.compile(r"\(([\w‘’' ]+?)\s+fanlari\)", re.IGNORECASE)
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
    summary = to_latin(item["summary"])
    info = {"title": title, "degree": "", "field": "", "name": "", "topic": "",
            "shifr": "", "supervisor": "", "institution": ""}

    if "DSc" in title:
        info["degree"] = "DSc"
    elif "PhD" in title:
        info["degree"] = "PhD"

    m = NAME_RE.search(title)
    info["name"] = m.group(1).strip() if m else ""

    fm = FIELD_LAT_RE.search(summary)
    if fm:
        info["field"] = fm.group(1).strip()

    for line in summary.split("\n"):
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        lab, value = label.lower(), value.strip()
        if "mavzu" in lab and "shifr" in lab and not info["topic"]:
            sm = SHIFR_RE.search(value)
            if sm:
                info["shifr"] = f"{sm.group(1)} – {sm.group(2).strip()}"
                value = value[:sm.start()]
            info["topic"] = value.strip(" ,.;").strip("“”\"«»„ ")
        elif ("rahbar" in lab or "maslahatchi" in lab) and not info["supervisor"]:
            info["supervisor"] = short_person(value)
        elif "bajarilgan" in lab or "bajarilayotgan" in lab:
            info["institution"] = info["institution"] or value.strip(" .")
    return info


def build_message(item: dict, section: str) -> str:
    e = html.escape
    info = extract(item)

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
    parts.append("\n".join(body))

    tags = []
    if info["degree"]:
        tags.append("#" + info["degree"])
    if info["field"]:
        tags.append("#" + re.sub(r"[‘’'\s]+", "_", info["field"].lower()).strip("_"))
    if section != SOURCES["/page/8"]:
        tags.append("#mustaqil_kengash")

    link = f'🔗 <a href="{item["url"]}">To‘liq e’lon — oak.uz</a>'
    parts.append(link + ("\n" + " ".join(tags) if tags else ""))
    if CHANNEL_SIGNATURE:
        parts.append(f"📢 {e(CHANNEL_SIGNATURE)}")
    return "\n\n".join(parts)


def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for _ in range(5):
        r = requests.post(url, data=payload, timeout=30)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            log.info("Telegram limit, %s soniya kutamiz", wait)
            time.sleep(wait + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram xatosi: {r.status_code} {r.text}")
        return
    raise RuntimeError("Telegram'ga yuborib bo'lmadi")


# ---------------- Holat (state) ----------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(seen: list[int]) -> None:
    seen = sorted(set(seen))[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps({"seen": seen}, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- Asosiy jarayon ----------------

def collect(seen: set[int]) -> list[tuple[dict, str]]:
    """Barcha bo'limlardan yangi e'lonlarni yig'adi."""
    found = {}
    for path, section in SOURCES.items():
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE}{path}" + (f"?page={page}" if page > 1 else "")
            try:
                items = parse_list(fetch(url))
            except RuntimeError as e:
                log.error(e)
                break
            if not items:
                log.warning("%s sahifasida e'lon topilmadi — sayt tuzilishi o'zgargan bo'lishi mumkin", url)
                break
            new = [i for i in items if i["id"] not in seen]
            for i in new:
                found.setdefault(i["id"], (i, section))
            if len(new) < len(items):   # eski e'lonlarga yetib keldik
                break
    return sorted(found.values(), key=lambda x: x[0]["id"])  # eskidan yangiga


def run_once() -> None:
    state = load_state()
    first_run = state is None
    seen = set(state["seen"]) if state else set()

    new_items = collect(seen)
    log.info("Yangi e'lonlar: %d", len(new_items))

    if first_run:
        # Birinchi ishga tushishda kanalni to'ldirib yubormaslik uchun
        # faqat oxirgi FIRST_RUN_POST tasini yuboramiz, qolganini "ko'rilgan" deb belgilaymiz
        skip = new_items[:-FIRST_RUN_POST] if FIRST_RUN_POST > 0 else new_items
        seen.update(i["id"] for i, _ in skip)
        new_items = new_items[len(skip):]
        save_state(list(seen))

    for item, section in new_items:
        try:
            send_message(build_message(item, section))
            log.info("Yuborildi: %s", item["url"])
        except RuntimeError as e:
            log.error(e)
            break
        seen.add(item["id"])
        save_state(list(seen))   # har yuborishdan keyin saqlaymiz — takror bo'lmasligi uchun
        time.sleep(3)


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
