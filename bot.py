"""Bitta ishga tushirish sikli (GitHub Actions har ~10 daqiqada chaqiradi):
1) admin tugmalari va tahrirlarni qayta ishlash;
2) osonish.uz'dan tanlangan viloyatlar vakansiyalarini olib, admin chatga tekshiruvga yuborish.

Ishlatish:
  python run.py                 # oddiy sikl
  python run.py --test 116052   # bitta vakansiyani tahlil qilib, matnini chiqarish (Telegramsiz)
"""
import html, json, os, sys, tempfile, time, traceback
import osonish, post, card, tg

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ADMIN_CHAT = os.environ.get("ADMIN_CHAT_ID", "")
CHANNEL = os.environ.get("CHANNEL_ID", "@karyerauznamangan")
MAX_FETCH = int(os.environ.get("MAX_FETCH", "350"))        # bir siklda ko'riladigan max sahifa
INITIAL_SCAN = int(os.environ.get("INITIAL_SCAN", "300"))  # birinchi ishga tushishda oxirgi N ta e'lon
CAPTION_LIMIT = 1024
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "480"))  # tugmalarni tez qayta ishlash uchun tinglash vaqti
DEFAULT_REGIONS = ["namangan"]
SETTINGS_BTN = "⚙️ Viloyatlar"

def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"last_id": None, "offset": 0, "posts": {}, "awaiting": {}, "regions": list(DEFAULT_REGIONS)}

def save(st):
    # eski yakunlangan postlarni tozalash (fayl o'sib ketmasin)
    done = [k for k, p in st["posts"].items() if p.get("status") != "pending"]
    for k in sorted(done, key=int)[:-500]:
        st["posts"].pop(k)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)

def _img(v, variant):
    path = os.path.join(tempfile.gettempdir(), f"v{v['id']}_{variant}.jpg")
    return card.make_card(v, path, variant=variant)

def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        print("XATO:", e)

# ---------- 0. Viloyatlar sozlamasi ----------
def _regions(st):
    return st.setdefault("regions", list(DEFAULT_REGIONS))

def regions_kb(st):
    on = set(_regions(st))
    rows = [[{"text": f"{name}: " + ("✅ Ha" if key in on else "❌ Yo'q"),
              "callback_data": f"rg:{key}"}] for key, (name, _) in sorted(osonish.REGIONS.items(), key=lambda kv: kv[0] != "namangan")]
    rows.append([{"text": "✅ Hammasi — Ha", "callback_data": "rg:*all"},
                 {"text": "❌ Hammasi — Yo'q", "callback_data": "rg:*none"}])
    rows.append([{"text": "💾 Saqlash", "callback_data": "rg:*done"}])
    return {"inline_keyboard": rows}

def regions_text(st):
    on = _regions(st)
    return ("⚙️ <b>Viloyatlar sozlamasi</b>\n\n"
            "Qaysi viloyat e'lonlari kelsin? Viloyat tugmasini bosib <b>Ha / Yo'q</b> ni almashtiring.\n"
            f"Hozir yoqilgan: <b>{len(on)}</b> ta.")

def show_regions(st):
    tg.call("sendMessage", chat_id=ADMIN_CHAT, text=regions_text(st), parse_mode="HTML",
            reply_markup=regions_kb(st))

def on_region_button(st, q, key):
    regs = _regions(st)
    msg_id = q["message"]["message_id"]
    if key == "*all":
        regs[:] = list(osonish.REGIONS)
    elif key == "*none":
        regs.clear()
    elif key == "*done":
        names = [osonish.REGIONS[k][0] for k in osonish.REGIONS if k in regs]
        txt = ("✅ <b>Saqlandi.</b> Endi quyidagi viloyatlar e'lonlari keladi:\n• " + "\n• ".join(names)) if names \
            else "⚠️ <b>Saqlandi, lekin birorta viloyat tanlanmagan</b> — yangi e'lonlar kelmaydi."
        tg.call("editMessageText", chat_id=ADMIN_CHAT, message_id=msg_id, text=txt, parse_mode="HTML")
        return
    elif key in osonish.REGIONS:
        regs.remove(key) if key in regs else regs.append(key)
    tg.call("editMessageText", chat_id=ADMIN_CHAT, message_id=msg_id, text=regions_text(st),
            parse_mode="HTML", reply_markup=regions_kb(st))

# ---------- Holat (/holat) va ogohlantirishlar ----------
def show_status(st):
    s = st.get("stats", {})
    pend = [k for k, p in st["posts"].items() if p.get("status") == "pending"]
    names = [osonish.REGIONS[k][0] for k in osonish.REGIONS if k in _regions(st)]
    txt = ("📊 <b>Bot holati</b>\n\n"
           f"🕒 Oxirgi tekshiruv: {s.get('time', '—')}\n"
           f"🔎 Ko'rilgan e'lonlar: {s.get('scanned', 0)} ta\n"
           f"📨 Yuborilgan: {s.get('sent', 0)} ta\n"
           f"⚠️ Xatolar: {s.get('errors', 0)} ta\n"
           f"🆔 Oxirgi ID: {st.get('last_id')}\n"
           f"⏳ Tasdiq kutayotgan postlar: {len(pend)} ta\n"
           f"📍 Viloyatlar: {', '.join(names) or 'tanlanmagan'}\n"
           f"📊 Jami yuborilgan: {s.get('total_sent', 0)} ta")
    if s.get("last_error"):
        txt += f"\n\n❗ Oxirgi xato: <code>{html.escape(s['last_error'][:300])}</code>"
    tg.call("sendMessage", chat_id=ADMIN_CHAT, text=txt, parse_mode="HTML")

def alert(st, text, every=3600):
    """Adminni ogohlantirish (bir xil xabar soatiga ko'pi bilan 1 marta)."""
    now = time.time()
    if now - st.get("last_alert", 0) < every:
        return
    st["last_alert"] = now
    _safe(tg.call, "sendMessage", chat_id=ADMIN_CHAT, text="⚠️ " + text)

# ---------- 1. Admin harakatlari ----------
def handle_updates(st, wait=0):
    ups = tg.call("getUpdates", offset=st["offset"], timeout=wait,
                  allowed_updates=["callback_query", "message"])
    for u in ups:
        st["offset"] = u["update_id"] + 1
        try:
            if "callback_query" in u:
                on_button(st, u["callback_query"])
            elif "message" in u:
                on_message(st, u["message"])
        except Exception:
            traceback.print_exc()

def to_channel(mid):
    """Postni kanalga nusxalash; xato bo'lsa adminga sababini aytadi."""
    try:
        tg.call("copyMessage", chat_id=CHANNEL, from_chat_id=ADMIN_CHAT, message_id=mid)
        return True
    except Exception as e:
        print("Kanalga joylab bo'lmadi:", e)
        _safe(tg.call, "sendMessage", chat_id=ADMIN_CHAT, reply_to_message_id=mid,
              text=f"❌ Kanalga ({CHANNEL}) joylab bo'lmadi.\nSabab: {e}\n\n"
                   "Tekshiring: bot kanalga ADMIN qilinganmi va «Post messages» huquqi bormi; "
                   "CHANNEL_ID to'g'rimi. Keyin ✅ ni qayta bosing.")
        return False

def on_button(st, q):
    msg = q.get("message", {})
    if str(msg.get("chat", {}).get("id")) != str(ADMIN_CHAT):
        return
    act, _, vid = q.get("data", "").partition(":")
    _safe(tg.call, "answerCallbackQuery", callback_query_id=q["id"])
    if act == "rg":
        return on_region_button(st, q, vid)
    p = st["posts"].get(vid)
    who = q["from"].get("first_name", "admin")
    if not p:
        # Holat (state.json) yo'qolgan eski post: tugma baribir ishlasin
        mid = msg.get("message_id")
        if act == "ok" and mid:
            if not to_channel(mid):
                return
            st["posts"][vid] = {"msg_id": mid, "status": "posted"}
            _safe(tg.call, "editMessageReplyMarkup", chat_id=ADMIN_CHAT, message_id=mid,
                  reply_markup=tg.status_kb(f"✅ Kanalga joylandi ({who})"))
        elif act == "no" and mid:
            st["posts"][vid] = {"msg_id": mid, "status": "rejected"}
            _safe(tg.call, "editMessageReplyMarkup", chat_id=ADMIN_CHAT, message_id=mid,
                  reply_markup=tg.status_kb(f"❌ Rad etildi ({who})"))
        elif act in ("ed", "im"):
            _safe(tg.call, "sendMessage", chat_id=ADMIN_CHAT, reply_to_message_id=mid,
                  text="⚠️ Bu post eski — uni faqat tasdiqlash yoki rad etish mumkin.")
        return
    if p["status"] != "pending":
        return
    if act == "ok":
        if not to_channel(p["msg_id"]):
            return
        p["status"] = "posted"
        tg.call("editMessageReplyMarkup", chat_id=ADMIN_CHAT, message_id=p["msg_id"],
                reply_markup=tg.status_kb(f"✅ Kanalga joylandi ({who})"))
    elif act == "no":
        p["status"] = "rejected"
        tg.call("editMessageReplyMarkup", chat_id=ADMIN_CHAT, message_id=p["msg_id"],
                reply_markup=tg.status_kb(f"❌ Rad etildi ({who})"))
    elif act == "ed":
        m = tg.call("sendMessage", chat_id=ADMIN_CHAT, reply_to_message_id=p["msg_id"],
                    text=f"✏️ ID {vid}: postning yangi matnini shu xabarga JAVOB (reply) qilib yuboring.\n"
                         "Maslahat: post matnini nusxalab, tahrirlab yuboring — formatlash saqlanadi.",
                    reply_markup={"force_reply": True, "selective": True})
        st["awaiting"][str(m["message_id"])] = vid
    elif act == "im":
        p["variant"] = p.get("variant", 0) + 1
        path = _img(p["v"], p["variant"])
        with open(path, "rb") as f:
            media = {"type": "photo", "media": "attach://p", "caption": p["caption"]}
            if p.get("entities"):
                media["caption_entities"] = p["entities"]
            else:
                media["parse_mode"] = "HTML"
            tg.call("editMessageMedia", files={"p": f}, chat_id=ADMIN_CHAT, message_id=p["msg_id"],
                    media=media, reply_markup=tg.review_kb(vid))

def on_message(st, m):
    if str(m.get("chat", {}).get("id")) != str(ADMIN_CHAT):
        return
    t = (m.get("text") or "").strip()
    if t.startswith("/start"):
        tg.call("sendMessage", chat_id=ADMIN_CHAT,
                text="Assalomu alaykum! Men yangi vakansiyalarni tekshiruvga yuboraman.\n"
                     "Viloyatlarni sozlash uchun pastdagi «⚙️ Viloyatlar» tugmasini bosing.",
                reply_markup={"keyboard": [[{"text": SETTINGS_BTN}]], "resize_keyboard": True,
                              "is_persistent": True})
        return
    if t == SETTINGS_BTN or t.startswith("/viloyatlar"):
        return show_regions(st)
    if t.startswith("/holat"):
        return show_status(st)
    rep = m.get("reply_to_message")
    if not rep or str(rep["message_id"]) not in st["awaiting"]:
        return
    vid = st["awaiting"].pop(str(rep["message_id"]))
    p = st["posts"].get(vid)
    text = m.get("text") or m.get("caption")
    if not p or p["status"] != "pending" or not text:
        return
    if len(text) > CAPTION_LIMIT:
        tg.call("sendMessage", chat_id=ADMIN_CHAT, reply_to_message_id=m["message_id"],
                text=f"⚠️ Matn juda uzun ({len(text)} belgi, ruxsat {CAPTION_LIMIT}). Qisqartirib qayta yuboring.")
        st["awaiting"][str(rep["message_id"])] = vid
        return
    ents = m.get("entities") or m.get("caption_entities") or []
    tg.call("editMessageCaption", chat_id=ADMIN_CHAT, message_id=p["msg_id"], caption=text,
            caption_entities=ents, reply_markup=tg.review_kb(vid))
    p["caption"], p["entities"] = text, ents
    _safe(tg.call, "deleteMessage", chat_id=ADMIN_CHAT, message_id=rep["message_id"])
    _safe(tg.call, "setMessageReaction", chat_id=ADMIN_CHAT, message_id=m["message_id"],
          reaction=[{"type": "emoji", "emoji": "👍"}])

# ---------- 2. Yangi vakansiyalar ----------
def scrape(st):
    stats = {"time": time.strftime("%d.%m.%Y %H:%M"), "scanned": 0, "sent": 0, "errors": 0,
             "total_sent": st.get("stats", {}).get("total_sent", 0)}
    st["stats"] = stats
    try:
        todo = osonish.new_ids(st["last_id"], INITIAL_SCAN)[:MAX_FETCH]
    except Exception as e:
        stats["errors"] += 1
        stats["last_error"] = f"ID ro'yxati: {e}"
        print("XATO:", stats["last_error"])
        alert(st, f"osonish.uz'dan e'lonlar ro'yxatini olib bo'lmadi:\n{e}")
        return
    print(f"Ko'rib chiqiladi: {len(todo)} ta e'lon ({todo[0] if todo else '-'} … {todo[-1] if todo else '-'})")
    fails = st.setdefault("fails", {})
    streak = 0
    for n, vid in enumerate(todo):
        if n and n % 15 == 0:
            _safe(handle_updates, st)   # uzoq skan paytida ham ✅/❌ tugmalari kechikmasin
        try:
            v = osonish.fetch_vacancy(vid)
        except osonish.NotFound:
            st["last_id"] = vid           # bunday e'lon yo'q — keyingisiga o'tamiz
            continue
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"{vid}: {e}"
            print(f"{vid}: XATO ({e})")
            streak += 1
            if streak >= 3:
                # Sayt javob bermayapti/bloklayapti: ID'larni yo'qotmaslik uchun to'xtaymiz, keyingi safar davom etadi
                alert(st, f"osonish.uz javob bermayapti (ketma-ket {streak} xato). Oxirgi xato:\n{e}\n"
                          "Keyingi tekshiruvda qayta urinaman.")
                return
            continue
        streak = 0
        stats["scanned"] += 1
        if v.get("region_key") not in _regions(st) or str(vid) in st["posts"]:
            st["last_id"] = vid
            continue
        caption = post.build_text(v)
        if len(caption) > CAPTION_LIMIT:
            caption = caption[:CAPTION_LIMIT - 1] + "…"
        try:
            m = tg.send_photo(ADMIN_CHAT, _img(v, 0), caption, tg.review_kb(vid))
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"Telegram {vid}: {e}"
            print(f"{vid}: Telegramga yuborib bo'lmadi ({e})")
            fails[str(vid)] = fails.get(str(vid), 0) + 1
            if fails[str(vid)] < 3:
                return                    # keyingi safar shu e'londan qayta urinadi
            alert(st, f"Vakansiya {vid} ni yuborib bo'lmadi, o'tkazib yuborildi:\n{e}", every=0)
            st["last_id"] = vid
            continue
        fails.pop(str(vid), None)
        st["posts"][str(vid)] = {"msg_id": m["message_id"], "status": "pending", "variant": 0,
                                 "caption": m.get("caption", caption), "entities": m.get("caption_entities"),
                                 "v": v}
        st["last_id"] = vid  # faqat muvaffaqiyatli yuborilgandan keyin
        stats["sent"] += 1
        stats["total_sent"] += 1
        save(st)  # har postdan keyin saqlash — uzilib qolsa ham takrorlanmaydi
        print(f"{vid}: tekshiruvga yuborildi — {v['title']}")
    print(f"Natija: ko'rildi {stats['scanned']}, yuborildi {stats['sent']}, xato {stats['errors']}")

def main():
    if "--test" in sys.argv:
        vid = int(sys.argv[sys.argv.index("--test") + 1])
        html_ = osonish._get(f"{osonish.BASE}/vacancies/{vid}")
        open(f"test_{vid}.html", "w").write(html_)
        v = osonish.parse_vacancy(html_, vid)
        print(json.dumps(v, ensure_ascii=False, indent=1))
        print("\n" + post.build_text(v))
        print("\nRasm:", card.make_card(v, f"test_{vid}.jpg"))
        return
    assert tg.TOKEN and ADMIN_CHAT, "BOT_TOKEN va ADMIN_CHAT_ID sozlanmagan"
    st = load()
    _regions(st)
    _safe(tg.call, "setMyCommands", commands=[
        {"command": "holat", "description": "Bot holati va statistika"},
        {"command": "viloyatlar", "description": "Viloyatlarni sozlash (Ha/Yo'q)"},
        {"command": "start", "description": "Botni ishga tushirish"}])
    start = time.time()
    try:
        _safe(handle_updates, st)
        save(st)
        try:
            scrape(st)
        except Exception:
            traceback.print_exc()
        save(st)
        # Qolgan vaqtda tugmalarni deyarli darhol qayta ishlash uchun tinglab turamiz
        while time.time() - start < LISTEN_SECONDS:
            try:
                handle_updates(st, wait=min(25, max(1, int(LISTEN_SECONDS - (time.time() - start)))))
            except Exception as e:
                print("getUpdates xato:", e)
                time.sleep(5)
            save(st)
    finally:
        save(st)

if __name__ == "__main__":
    main()
