# ══════════════════════════════════════════════════════════════════════════
#  VAGENT — WEB YADRO KO'PRIGI (bu kod voro_creator_bot.py ichiga KIRITILADI)
#  Bot alohida agent YURITMAYDI — vagent_bot_bridge orqali web yadroni (vagent_api)
#  chaqiradi. reference/generatsiya/billing/til/xotira — HAMMASI web'da, aynan bir xil.
#  Bog'liq bot globallari: VAGENT_MODE, get_user, InlineKeyboardButton,
#  InlineKeyboardMarkup, Update, ContextTypes (voro_creator_bot.py da mavjud).
# ══════════════════════════════════════════════════════════════════════════
import sys as _vg_sys
if "/root/bot" not in _vg_sys.path:
    _vg_sys.path.insert(0, "/root/bot")
import time as _vg_time
import vagent_bot_bridge as vgbridge

_VG_L = {
    "ref_saved": {"uz": "Rasm qabul qilindi ✓ Endi nima qilishni yozing.",
                  "ru": "Фото принято ✓ Напишите, что сделать.",
                  "en": "Photo received ✓ Tell me what to do."},
    "ref_fail":  {"uz": "Rasmni yuklab bo'lmadi — boshqa rasm yuboring.",
                  "ru": "Не удалось загрузить фото — пришлите другое.",
                  "en": "Couldn't upload the photo — send another."},
    "no_acc":    {"uz": "Akkaunt topilmadi. Iltimos, /start bosing.",
                  "ru": "Аккаунт не найден. Нажмите /start.",
                  "en": "Account not found. Please press /start."},
    "err":       {"uz": "⚠️ Xatolik yuz berdi — qayta urinib ko'ring.",
                  "ru": "⚠️ Произошла ошибка — попробуйте снова.",
                  "en": "⚠️ Something went wrong — please try again."},
    "yes":       {"uz": "✅ Ha", "ru": "✅ Да", "en": "✅ Yes"},
    "no":        {"uz": "❌ Yo'q", "ru": "❌ Нет", "en": "❌ No"},
}


def _vg_lang(tg_uid):
    try:
        u = get_user(tg_uid)
        if isinstance(u, dict):
            return u.get("lang") or "uz"
    except Exception:
        pass
    return "uz"


def _vg_t(key, lang):
    return _VG_L.get(key, {}).get(lang) or _VG_L.get(key, {}).get("uz", "")


async def _vagent_render(context, chat_id, uid_web, *, message="", pending_refs=None,
                         pending_video="", confirm_token=None, decline=False,
                         lang="uz", tg_uid=0):
    """Bitta Vagent turnini render qiladi: bridge SSE oqimi → Telegram xabarlari.
    8 emit voqeasi (text/status/progress/options/confirm/result/balance/error) maplangan."""
    bot = context.bot
    sess_chat = f"tg{tg_uid}"
    cur_mid = [None]
    cur_text = [""]
    last_edit = [0.0]
    prog_mid = [None]

    async def _flush():
        if cur_text[0].strip():
            if cur_mid[0] is None:
                m = await bot.send_message(chat_id, cur_text[0])
                cur_mid[0] = m.message_id
            else:
                try:
                    await bot.edit_message_text(cur_text[0], chat_id, cur_mid[0])
                except Exception:
                    pass
        cur_mid[0] = None
        cur_text[0] = ""
        last_edit[0] = 0.0

    try:
        async for ev, data in vgbridge.stream_chat(
                uid_web, message=message, lang=lang,
                pending_refs=pending_refs, pending_video=pending_video,
                confirm_token=confirm_token, decline=decline, chat_id=sess_chat):

            if ev == "text":
                cur_text[0] += data.get("delta", "")
                now = _vg_time.time()
                if cur_mid[0] is None and cur_text[0].strip():
                    m = await bot.send_message(chat_id, cur_text[0])
                    cur_mid[0] = m.message_id
                    last_edit[0] = now
                elif cur_mid[0] is not None and now - last_edit[0] > 1.4:
                    try:
                        await bot.edit_message_text(cur_text[0], chat_id, cur_mid[0])
                    except Exception:
                        pass
                    last_edit[0] = now

            elif ev == "status":
                # generatsiya bosqichlari (masalan "⚙️ ish boshlandi") — alohida xabar
                _st = (data.get("text") or "").strip()
                if _st:
                    await _flush()
                    try:
                        await bot.send_message(chat_id, _st)
                    except Exception:
                        pass

            elif ev == "progress":
                # jonli "yaratilyapti" — chat action bilan (xabar spam qilmaydi)
                try:
                    await bot.send_chat_action(chat_id, "upload_photo")
                except Exception:
                    pass

            elif ev == "options":
                await _flush()
                opts = [(o.get("label") or "") for o in data.get("options", [])]
                context.user_data["vg_opts"] = opts
                kb = [[InlineKeyboardButton(o, callback_data=f"vg:o:{i}")]
                      for i, o in enumerate(opts) if o]
                if kb:
                    await bot.send_message(chat_id, data.get("prompt") or "👇",
                                           reply_markup=InlineKeyboardMarkup(kb))

            elif ev == "confirm":
                await _flush()
                context.user_data["vg_ct"] = data.get("token")
                summary = data.get("summary", "")
                total = data.get("total")
                txt = summary + (f"\n\n💰 {total}⚡️" if total is not None else "")
                kb = [[InlineKeyboardButton(_vg_t("yes", lang), callback_data="vg:yes"),
                       InlineKeyboardButton(_vg_t("no", lang), callback_data="vg:no")]]
                await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb))

            elif ev == "result":
                await _flush()
                url = data.get("url")
                kind = data.get("kind")
                cap = data.get("label") or ""
                try:
                    if kind == "video":
                        await bot.send_video(chat_id, url, caption=cap)
                    else:
                        await bot.send_photo(chat_id, url, caption=cap)
                except Exception:
                    try:
                        await bot.send_message(chat_id, url)
                    except Exception:
                        pass
                # referens/video ishlatildi → tozalaymiz (web kabi)
                context.user_data.pop("vg_refs", None)
                context.user_data.pop("vg_video", None)

            elif ev == "balance":
                pass  # JIM — balans /me yoki keyingi ko'rinishda

            elif ev == "error":
                await _flush()
                _e = (data.get("text") or "").strip()
                if _e:
                    try:
                        await bot.send_message(chat_id, _e)
                    except Exception:
                        pass

        await _flush()
    except Exception:
        try:
            await bot.send_message(chat_id, _vg_t("err", lang))
        except Exception:
            pass


async def vagent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """VAGENT_MODE — WEB YADRO orqali (bridge). Eski reimplementatsiya o'rniga."""
    msg = update.message
    if not msg:
        return VAGENT_MODE
    tg_uid = msg.from_user.id
    try:
        u = get_user(tg_uid)
        if isinstance(u, dict) and u.get("banned"):
            await msg.reply_text(_vg_t("err", "uz"))
            return VAGENT_MODE
    except Exception:
        pass
    lang = _vg_lang(tg_uid)
    uid_web = vgbridge.web_uid_for_tg(tg_uid)
    if not uid_web:
        await msg.reply_text(_vg_t("no_acc", lang))
        return VAGENT_MODE

    # RASM → Atlas'ga upload (web bilan bir xil ishlov) → yopishqoq pending_refs
    is_img = bool(msg.photo) or bool(
        msg.document and (msg.document.mime_type or "").startswith("image/"))
    if is_img:
        try:
            _f = await (msg.photo[-1].get_file() if msg.photo else msg.document.get_file())
            _raw = bytes(await _f.download_as_bytearray())
            url = await vgbridge.upload_ref(uid_web, _raw, "image/jpeg")
        except Exception:
            url = None
        if not url:
            await msg.reply_text(_vg_t("ref_fail", lang))
            return VAGENT_MODE
        refs = list(context.user_data.get("vg_refs") or [])
        refs.append(url)
        context.user_data["vg_refs"] = refs[-3:]
        cap = (msg.caption or "").strip()
        if not cap:
            await msg.reply_text(_vg_t("ref_saved", lang))
            return VAGENT_MODE
        text = cap
    else:
        text = msg.text or ""

    try:
        await context.bot.send_chat_action(msg.chat_id, "typing")
    except Exception:
        pass
    await _vagent_render(context, msg.chat_id, uid_web, message=text,
                         pending_refs=context.user_data.get("vg_refs"),
                         pending_video=context.user_data.get("vg_video", ""),
                         lang=lang, tg_uid=tg_uid)
    return VAGENT_MODE


async def vagent_cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """VAGENT_MODE inline tugmalari: vg:o:<i> (variant), vg:yes / vg:no (tasdiq)."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    d = q.data or ""
    tg_uid = q.from_user.id
    lang = _vg_lang(tg_uid)
    uid_web = vgbridge.web_uid_for_tg(tg_uid)
    if not uid_web:
        return VAGENT_MODE
    chat_id = q.message.chat_id
    # tugmalarni olib tashlaymiz (qayta bosilmasin)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if d.startswith("vg:o:"):
        try:
            idx = int(d.split(":")[2])
        except Exception:
            return VAGENT_MODE
        opts = context.user_data.get("vg_opts") or []
        if 0 <= idx < len(opts):
            await _vagent_render(context, chat_id, uid_web, message=opts[idx],
                                 pending_refs=context.user_data.get("vg_refs"),
                                 pending_video=context.user_data.get("vg_video", ""),
                                 lang=lang, tg_uid=tg_uid)
    elif d == "vg:yes":
        await _vagent_render(context, chat_id, uid_web,
                             confirm_token=context.user_data.get("vg_ct"),
                             lang=lang, tg_uid=tg_uid)
    elif d == "vg:no":
        await _vagent_render(context, chat_id, uid_web, decline=True,
                             lang=lang, tg_uid=tg_uid)
    return VAGENT_MODE
