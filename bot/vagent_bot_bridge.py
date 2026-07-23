# -*- coding: utf-8 -*-
"""
VAGENT BOT BRIDGE — bot native Vagent'ni WEB YADROSIga (vagent_api.py) ulaydi.

Bot alohida reimplementatsiya QILMAYDI. localhost:8788/vagent/chat'ni chaqiradi va
SSE oqimini (event, data) juftlariga aylantiradi — bot uni Telegram xabarlariga
render qiladi. Shu sabab reference/generatsiya/billing/til/xotira — HAMMASI web
yadroda, AYNAN bir xil (feature parietet, "reference yetib bormadi" bo'lishi mumkin emas).

Balans: tg_id → 'web:<id>' (voro_web.db) → o'sha yagona balans (web/miniapp/bot bir xil).
Auth: VORO_BOT_SECRET (web bilan bir xil) bilan HMAC imzo — make_vagent_url kabi.
"""
import os
import time
import json
import hmac
import hashlib
import sqlite3
import base64
import httpx

VAGENT_API = os.environ.get("VAGENT_API_BASE", "http://127.0.0.1:8788/vagent")
WEB_DB     = os.environ.get("VORO_WEB_DB", "/opt/voro-web/voro_web.db")

_SECRET_CACHE = {"v": None}


def _bot_secret() -> str:
    if _SECRET_CACHE["v"]:
        return _SECRET_CACHE["v"]
    s = os.environ.get("VORO_BOT_SECRET", "")
    if not s:
        for path in ("/root/bot/.env", "/opt/voro-web/.env"):
            try:
                for ln in open(path):
                    if ln.startswith("VORO_BOT_SECRET="):
                        s = ln.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except Exception:
                pass
            if s:
                break
    _SECRET_CACHE["v"] = s
    return s


def _sign(uid: str, exp: int) -> str:
    return hmac.new(_bot_secret().encode(), f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()


def web_uid_for_tg(tg_id: int):
    """tg_id → 'web:<id>' (voro_web.db orqali). Topilmasa None (akkaunt yo'q)."""
    try:
        con = sqlite3.connect(f"file:{WEB_DB}?mode=ro", uri=True, timeout=3)
        r = con.execute("SELECT id FROM users WHERE tg_id=?", (int(tg_id),)).fetchone()
        con.close()
        if r:
            return f"web:{int(r[0])}"
    except Exception:
        pass
    return None


def _auth_headers(uid: str, mime: str = "") -> dict:
    exp = int(time.time()) + 3600
    h = {"X-Uid": uid, "X-Exp": str(exp), "X-Sig": _sign(uid, exp)}
    if mime:
        h["X-Mime"] = mime
    return h


async def upload_ref(uid: str, raw: bytes, mime: str = "image/jpeg"):
    """Rasm/videoni /vagent/upload-raw orqali yuklaydi (web bilan bir xil ishlov:
    HEIC→JPEG normalize, .mov→mp4 transcode). URL qaytaradi yoki None."""
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{VAGENT_API}/upload-raw",
                             headers=_auth_headers(uid, mime), content=raw)
            if r.status_code == 200:
                return r.json().get("url")
    except Exception:
        pass
    return None


async def stream_chat(uid: str, *, message: str = "", lang: str = "uz",
                      pending_refs=None, pending_video: str = "",
                      confirm_token=None, decline: bool = False, chat_id: str = "tgbot"):
    """/vagent/chat'ga POST + SSE oqim. (event, data) juftlarini yield qiladi.
    event lardan biri: text, status, progress, options, confirm, result, balance, error, done."""
    exp = int(time.time()) + 3600
    body = {
        "uid": uid, "exp": str(exp), "sig": _sign(uid, exp), "lang": lang,
        "message": message,
        "attachments": list(pending_refs or []),
        "pending_refs": list(pending_refs or []),
        "pending_video": pending_video or "",
        "confirm_token": confirm_token,
        "decline": bool(decline),
        "chat_id": chat_id,
    }
    cur_ev = None
    try:
        async with httpx.AsyncClient(timeout=900) as client:
            async with client.stream("POST", f"{VAGENT_API}/chat", json=body) as resp:
                if resp.status_code != 200:
                    yield ("error", {"text": "Server band — biroz kuting."})
                    yield ("done", {})
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event: "):
                        cur_ev = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                        except Exception:
                            data = {}
                        if cur_ev:
                            yield (cur_ev, data)
    except Exception as e:
        yield ("error", {"text": "Aloqa uzildi — qayta urinib ko'ring."})
        yield ("done", {})
