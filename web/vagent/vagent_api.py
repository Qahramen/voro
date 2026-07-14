# -*- coding: utf-8 -*-
"""
VAGENT 3.0 — VoroCreatorBot Mini App agentik AI yadro
======================================================
voro_web_api.py ichiga ulanadi:

    from vagent_api import vagent_router
    app.include_router(vagent_router)

v3 YANGILIKLARI (v2 zaifliklarini yopadi):
  ✔ CRASH-PROOF: SSE uzilsa ham generatsiya davom etadi, natija
    saqlanadi, Mini App qayta ochilganda tiklanadi (/vagent/inbox)
  ✔ REFERENS RASM: foydalanuvchi o'z suratini yuklaydi (@image1)
  ✔ OVOZ: gapirib buyurish (Groq Whisper transkripsiya)
  ✔ PARALLEL: ko'p-kadrli reja bir vaqtda generatsiya bo'ladi
  ✔ PROGRESS: jonli % va vaqt hisoblagichi
  ✔ PROAKTIV: bayram/mavsum kalendari — Vagent o'zi g'oya beradi
  ✔ LIMIT: bir foydalanuvchiga max 3 parallel ish

"INTEGRATSIYA:" belgilari — botdagi mavjud kod bilan ulanish nuqtalari.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
import fcntl
import tempfile
from datetime import date
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from vagent_analytics import track, compute_stats, daily_digest_text

# ============================================================
# SOZLAMALAR
# ============================================================

VAGENT_VERSION = "4.0"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("VAGENT_MODEL", "claude-sonnet-4-6")  # INTEGRATSIYA: yangilash uchun env
ATLAS_API_KEY = os.environ.get("ATLAS_API_KEY", "")
ATLAS_BASE = "https://api.atlascloud.ai"        # INTEGRATSIYA: bot bilan bir xil
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # ovoz transkripsiyasi (botda bor)
BOT_SECRET = os.environ.get("VORO_BOT_SECRET", "")
OWNER_ID = os.environ.get("VORO_OWNER_ID", "")   # Qahramonning Telegram user_id'si (admin panel uchun)
PUBLIC_BASE = os.environ.get("VORO_PUBLIC_BASE", "https://voro.uz")  # tashqi URL

DATA_DIR = os.environ.get("VORO_DATA_DIR", "/opt/voro")
USERS_JSON = os.environ.get("VORO_USERS_JSON", "/root/bot/users.json")   # INTEGRATSIYA: bot balans fayli
MEMORY_JSON = os.path.join(DATA_DIR, "vagent_memory.json")
INBOX_JSON = os.path.join(DATA_DIR, "vagent_inbox.json")     # crash-recovery natijalari
UPLOADS_DIR = os.path.join(DATA_DIR, "vagent_uploads")
SKILLS_JSON = os.path.join(DATA_DIR, "vagent_skills.json")   # o'sib boruvchi retseptlar bazasi
MODELS_JSON = os.path.join(DATA_DIR, "vagent_models.json")   # narx/model konfiguratsiyasi (hot-reload)
ELEMENTS_DB = os.environ.get("VORO_ELEMENTS_DB", os.path.join(DATA_DIR, "elements.db"))    # INTEGRATSIYA: bot Element Library SQLite yo'li
ELEMENTS_DIR = os.environ.get("VORO_ELEMENTS_DIR", os.path.join(DATA_DIR, "elements"))     # INTEGRATSIYA: element fayllar papkasi

MAX_TURN_TOOL_LOOPS = 12
SESSION_TTL = 60 * 60 * 6
FREE_ITERATION_DISCOUNT = 0.5
MAX_ACTIVE_JOBS_PER_USER = 3   # concurrent limit (botdagi kabi)
MAX_UPLOAD_MB = 8

# ------------------------------------------------------------
# NARXLAR. INTEGRATSIYA: botdagi resolution-aware pricing bilan
# BIR MANBA bo'lsin (pricing.py modulga chiqarib import qilinsin).
# ------------------------------------------------------------
# ------------------------------------------------------------
# NARXLAR — vagent_models.json dan HOT-RELOAD (deploysiz tahrir).
# Fayl bo'lmasa/buzuq bo'lsa quyidagi zaxira jadval ishlaydi.
# ------------------------------------------------------------
_FALLBACK_MODELS = {
    "pricing": {
        "image": {"nano-banana-2": {"base": 2}, "gpt-image-2": {"base": 5}},
        "video": {
            "seedance-2.0": {"base_per_sec": 4, "res_mult": {"480p": 1.0, "720p": 1.5, "1080p": 2.5}},
            "kling-3.0":    {"base_per_sec": 6, "res_mult": {"720p": 1.0, "1080p": 1.8}},
            "veo-3.1":      {"base_per_sec": 8, "res_mult": {"720p": 1.0, "1080p": 1.6}},
        },
    },
    "hints": {
        "nano-banana-2": "Tez va arzon rasm.",
        "gpt-image-2": "Yuqori sifat, matn/logo aniq.",
        "seedance-2.0": "Eng kuchli video, multi-referens.",
        "kling-3.0": "Silliq harakat.",
        "veo-3.1": "Kinematik sifat.",
    },
}
_models_cache = {"mtime": 0.0, "data": _FALLBACK_MODELS}

def _models_config() -> dict:
    try:
        mt = os.path.getmtime(MODELS_JSON)
        if mt != _models_cache["mtime"]:
            with open(MODELS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert "pricing" in data and "hints" in data
            _models_cache.update(mtime=mt, data=data)
    except Exception:
        pass
    return _models_cache["data"]

def get_pricing_table() -> dict:
    return _models_config()["pricing"]

def get_model_hints() -> dict:
    return _models_config()["hints"]

# O'zbekiston bayram/mavsum kalendari — proaktiv g'oyalar uchun
UZ_CALENDAR = [
    ((1, 1),  "Yangi yil"),
    ((3, 8),  "Xotin-qizlar kuni"),
    ((3, 21), "Navro'z bayrami"),
    ((5, 9),  "Xotira va qadrlash kuni"),
    ((6, 1),  "Bolalar kuni"),
    ((9, 1),  "Mustaqillik kuni"),
    ((10, 1), "O'qituvchilar kuni"),
    ((12, 8), "Konstitutsiya kuni"),
]

# ============================================================
# XAVFSIZ JSON (fcntl lock + atomik yozish)
# ============================================================

def _locked_read(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        except Exception:
            return default
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _locked_update(path: str, default: Any, fn):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = _locked_read(path, default)
            result = fn(data)
            _locked_write(path, data)
            return result
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)

# ============================================================
# BALANS — INTEGRATSIYA: botdagi helper bo'lsa, shuni import qiling
# ============================================================

def _credit_key(u: dict) -> str:
    return "credits" if "credits" in u else ("tangacha" if "tangacha" in u else "credits")

def get_balance(user_id: str) -> int:
    u = _locked_read(USERS_JSON, {}).get(str(user_id), {})
    return int(u.get(_credit_key(u), 0))

def deduct_credits(user_id: str, amount: int) -> bool:
    def fn(users):
        u = users.setdefault(str(user_id), {})
        k = _credit_key(u)
        if int(u.get(k, 0)) < amount:
            return False
        u[k] = int(u.get(k, 0)) - amount
        return True
    return _locked_update(USERS_JSON, {}, fn)

def refund_credits(user_id: str, amount: int) -> None:
    def fn(users):
        u = users.setdefault(str(user_id), {})
        k = _credit_key(u)
        u[k] = int(u.get(k, 0)) + amount
        return True
    _locked_update(USERS_JSON, {}, fn)

# ============================================================
# DOIMIY XOTIRA
# ============================================================

def memory_get(user_id: str) -> dict:
    return _locked_read(MEMORY_JSON, {}).get(
        str(user_id), {"facts": [], "name": "", "history": []})

def memory_add_fact(user_id: str, fact: str) -> None:
    def fn(mem):
        u = mem.setdefault(str(user_id), {"facts": [], "name": "", "history": []})
        if fact not in u["facts"]:
            u["facts"] = (u["facts"] + [fact])[-40:]
        return True
    _locked_update(MEMORY_JSON, {}, fn)

def memory_log_job(user_id: str, entry: dict) -> None:
    def fn(mem):
        u = mem.setdefault(str(user_id), {"facts": [], "name": "", "history": []})
        u["history"] = (u["history"] + [entry])[-25:]
        return True
    _locked_update(MEMORY_JSON, {}, fn)


def memory_add_reaction(user_id: str, item: dict) -> None:
    """Did-profil: har bir 🔥/👍/👎 reaksiya Vagent'ning didni o'rganishiga xizmat qiladi.
    Bu — kompaund moat: qancha ko'p ishlatsa, Vagent didini shuncha yaxshi biladi,
    raqobatchiga o'tish qimmatlashadi."""
    def fn(mem):
        u = mem.setdefault(str(user_id), {"facts": [], "name": "", "history": []})
        u["taste"] = (u.get("taste", []) + [item])[-30:]
        return True
    _locked_update(MEMORY_JSON, {}, fn)

# ============================================================
# INBOX — crash-recovery: SSE uzilsa ham natija shu yerda kutadi
# ============================================================

def inbox_push(user_id: str, item: dict) -> None:
    def fn(box):
        lst = box.setdefault(str(user_id), [])
        lst.append({**item, "id": uuid.uuid4().hex[:10], "ts": int(time.time())})
        box[str(user_id)] = lst[-20:]
        return True
    _locked_update(INBOX_JSON, {}, fn)

def inbox_pull(user_id: str, since_ts: int = 0) -> list:
    box = _locked_read(INBOX_JSON, {})
    return [i for i in box.get(str(user_id), []) if i["ts"] > since_ts]

# ============================================================
# HMAC AUTH
# ============================================================

def verify_auth(user_id: str, exp: str, sig: str) -> bool:
    if not BOT_SECRET:
        return False
    try:
        if int(exp) < time.time():
            return False
    except Exception:
        return False
    expected = hmac.new(BOT_SECRET.encode(), f"{user_id}:{exp}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

# ============================================================
# ATLAS CLOUD
# INTEGRATSIYA: bu 2 funksiya botdagi mavjud Atlas funksiyalarning
# nusxasi bo'lsin (endpoint, payload, javob maydonlari).
# ============================================================

# INTEGRATSIYA: qisqa model nomlarini real Atlas ID'lariga moslash (variant referens soniga qarab)
def _atlas_model_id(friendly: str, kind: str, n_refs: int) -> str:
    m = (friendly or "").lower()
    if kind == "image":
        base = "openai/gpt-image-2" if "gpt" in m else "google/nano-banana-2"
        return base + ("/edit" if n_refs > 0 else "/text-to-image")
    # video
    if "veo" in m:
        if n_refs >= 2: return "google/veo3.1/reference-to-video"
        if n_refs >= 1: return "google/veo3.1-fast/image-to-video"
        return "google/veo3.1/text-to-video"
    if "kling" in m:
        return "kwaivgi/kling-v3.0-std/image-to-video"   # kling v3 referens rasm talab qiladi
    if n_refs >= 2: return "bytedance/seedance-2.0/reference-to-video"
    if n_refs >= 1: return "bytedance/seedance-2.0/image-to-video"
    return "bytedance/seedance-2.0/text-to-video"


def _attachment_b64(url):
    """voro.uz/vagent/file/ lokal URL'idan base64 + media_type (Claude vision uchun)."""
    try:
        name = url.rstrip("/").split("/vagent/file/")[-1].split("?")[0]
        path = os.path.join(UPLOADS_DIR, name)
        if not os.path.exists(path):
            return None, None
        with open(path, "rb") as fh:
            raw = fh.read()
        ext = name.rsplit(".", 1)[-1].lower()
        mt = {"png": "image/png", "webp": "image/webp",
              "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/jpeg")
        return base64.b64encode(raw).decode(), mt
    except Exception:
        return None, None


async def _atlas_upload_ref(url):
    """Lokal reference URL'ni Atlas uploadMedia orqali aliyuncs URL'ga aylantiradi
    (Atlas tashqi URL'ni ishonchli olmasligi mumkin). Tashqi URL bo'lsa o'zini qaytaradi."""
    b64, mt = _attachment_b64(url)
    if not b64:
        return url
    try:
        raw = base64.b64decode(b64)
        ext = {"image/png": "png", "image/webp": "webp"}.get(mt, "jpg")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{ATLAS_BASE}/api/v1/model/uploadMedia",
                headers={"Authorization": f"Bearer {ATLAS_API_KEY}", "User-Agent": "Mozilla/5.0"},
                files={"file": (f"ref.{ext}", raw, mt)})
            d = (r.json().get("data") or {})
            return d.get("download_url") or url
    except Exception:
        return url


async def atlas_create_job(kind: str, model: str, payload: dict) -> str:
    refs = payload.get("references") or []
    refs = [await _atlas_upload_ref(u) for u in refs]   # lokal -> Atlas aliyuncs
    model_id = _atlas_model_id(model, kind, len(refs))
    endpoint = "generateImage" if kind == "image" else "generateVideo"
    body = {"model": model_id, "prompt": payload.get("prompt", "")}
    if payload.get("negative_prompt"):
        body["negative_prompt"] = payload["negative_prompt"]
    if payload.get("aspect_ratio"):
        body["aspect_ratio"] = payload["aspect_ratio"]
    if kind == "video":
        body["resolution"] = payload.get("resolution", "720p")
        body["duration"] = int(payload.get("duration", 5))
    if refs:
        if "reference-to-video" in model_id or len(refs) > 1:
            body["images"] = refs
        else:
            body["image"] = refs[0]
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            f"{ATLAS_BASE}/api/v1/model/{endpoint}",
            headers={"Authorization": f"Bearer {ATLAS_API_KEY}",
                     "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"},
            json=body)
        jd = r.json()
        pid = (jd.get("data") or {}).get("id")
        if not pid:
            raise RuntimeError(str(jd.get("message") or jd.get("msg") or jd)[:200])
        return pid


async def atlas_poll_job(job_id: str, on_progress=None, timeout_sec: int = 900) -> dict:
    start = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        while time.time() - start < timeout_sec:
            try:
                r = await client.get(
                    f"{ATLAS_BASE}/api/v1/model/prediction/{job_id}",
                    headers={"Authorization": f"Bearer {ATLAS_API_KEY}",
                             "User-Agent": "Mozilla/5.0"})
                data = (r.json().get("data") or {})
            except Exception:
                data = {}
            st = (data.get("status") or "").lower()
            if on_progress:
                await on_progress(int(time.time() - start), None)
            if st in ("completed", "succeeded", "success"):
                outs = data.get("outputs") or []
                return {"status": "ok", "url": (outs[0] if outs else "")}
            if st in ("failed", "error", "canceled", "cancelled"):
                return {"status": "failed", "error": data.get("error") or "noma'lum xato"}
            await asyncio.sleep(5)
    return {"status": "failed", "error": "vaqt tugadi (timeout)"}

# ============================================================
# NARX
# ============================================================

def calc_price(kind: str, model: str, resolution: str = "720p", duration: int = 5) -> Optional[int]:
    try:
        P = get_pricing_table()
        if kind == "image":
            return P["image"][model]["base"]
        cfg = P["video"][model]
        mult = cfg["res_mult"].get(resolution)
        return None if mult is None else int(round(cfg["base_per_sec"] * duration * mult))
    except KeyError:
        return None

# ============================================================
# SESSIYALAR
# ============================================================

class Session:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.messages: list[dict] = []
        self.lang: str = "uz"
        self.pending_quote: Optional[dict] = None
        self.confirmed_token: Optional[str] = None
        self.last_jobs: list[dict] = []
        self.active_jobs = 0
        self.updated = time.time()

# Oddiy rate-limit: 60 soniyada max 15 xabar / foydalanuvchi
from collections import deque
_RATE: dict[str, deque] = {}

def rate_ok(uid: str, limit: int = 15, window: int = 60) -> bool:
    q = _RATE.setdefault(uid, deque())
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = asyncio.Lock()

async def get_session(user_id: str) -> Session:
    async with SESSIONS_LOCK:
        for k in [k for k, s in SESSIONS.items() if time.time() - s.updated > SESSION_TTL]:
            SESSIONS.pop(k, None)
        s = SESSIONS.setdefault(user_id, Session(user_id))
        s.updated = time.time()
        return s

# ============================================================
# CLAUDE TOOLLARI
# ============================================================

JOB_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["image", "video"]},
        "model": {"type": "string"},
        "prompt": {"type": "string", "description": "Professional INGLIZ tilida"},
        "negative_prompt": {"type": "string"},
        "aspect_ratio": {"type": "string"},
        "resolution": {"type": "string"},
        "duration": {"type": "integer"},
        "reference_urls": {"type": "array", "items": {"type": "string"}},
        "label": {"type": "string", "description": "Qisqa o'zbekcha nom"},
    },
    "required": ["kind", "model", "prompt", "label"],
}

TOOLS = [
    {"name": "check_balance",
     "description": "Joriy tangacha balansi.",
     "input_schema": {"type": "object", "properties": {}}},

    {"name": "get_pricing",
     "description": "Narx jadvali va model hintlari.",
     "input_schema": {"type": "object", "properties": {}}},

    {"name": "get_today_context",
     "description": "Bugungi sana va yaqinlashayotgan O'zbekiston bayramlari — mavsumiy g'oya berish uchun.",
     "input_schema": {"type": "object", "properties": {}}},

    {"name": "estimate_cost",
     "description": "Rejalashtirilgan ishlar narxini hisoblaydi. Generatsiyadan OLDIN majburiy.",
     "input_schema": {"type": "object", "properties": {"jobs": {"type": "array", "items": {
         "type": "object",
         "properties": {"kind": {"type": "string"}, "model": {"type": "string"},
                        "resolution": {"type": "string"}, "duration": {"type": "integer"},
                        "label": {"type": "string"}},
         "required": ["kind", "model", "label"]}}}, "required": ["jobs"]}},

    {"name": "request_confirmation",
     "description": "Tangacha sarfini tasdiqlash kartasi. Generatsiyadan oldin MAJBURIY. Chaqirilgach javobni yakunlang — foydalanuvchi tugma bosishini kutamiz.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string"},
         "total": {"type": "integer"},
         "jobs": {"type": "array", "items": {"type": "object"}}},
         "required": ["summary", "total", "jobs"]}},

    {"name": "generate_batch",
     "description": "1 yoki bir nechta ishni PARALLEL generatsiya qiladi. FAQAT tasdiqdan keyin. Ko'p-kadrli rejalarda hammasini bitta chaqiruvda bering — bir vaqtda ishlaydi.",
     "input_schema": {"type": "object", "properties": {
         "jobs": {"type": "array", "items": JOB_ITEM_SCHEMA},
         "is_iteration": {"type": "boolean", "description": "Oldingi natijaning remixi bo'lsa true — 50% chegirma"}},
         "required": ["jobs"]}},

    {"name": "search_skills",
     "description": "Isbotlangan prompt-retseptlar bazasidan qidiradi (motion transfer, reklama, futbol shablon, to'y va h.k.). HAR generatsiya rejasidan oldin tekshir — tayyor retsept sifatni keskin oshiradi.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},

    {"name": "list_elements",
     "description": "Foydalanuvchining Element kutubxonasi — saqlangan qahramonlar, logotiplar, mahsulot rasmlari. Ularni reference_urls sifatida ishlatish mumkin (@image1).",
     "input_schema": {"type": "object", "properties": {}}},

    {"name": "remember_fact",
     "description": "Foydalanuvchi haqidagi muhim faktni doimiy xotiraga yozadi.",
     "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}},
                      "required": ["fact"]}},
]

# ============================================================
# SYSTEM PROMPT — Vagent shaxsiyati
# ============================================================

_LANG_RULE = {
    "uz": "Faqat O'ZBEK tilida (lotin)",
    "ru": "Отвечай ТОЛЬКО на РУССКОМ языке",
    "en": "Reply ONLY in ENGLISH",
}


def build_system_prompt(user_id: str, lang: str = "uz") -> str:
    lang_rule = _LANG_RULE.get(lang, _LANG_RULE["uz"])
    lang_name = {"ru": "на русском", "en": "in English"}.get(lang, "o'zbekcha (lotin)")
    mem = memory_get(user_id)
    facts = "\n".join(f"- {f}" for f in mem["facts"]) or "- (hali fakt yo'q)"
    recent = "\n".join(
        f"- {h.get('label','?')} | {h.get('model','?')} | {h.get('price','?')} tangacha"
        for h in mem["history"][-5:]) or "- (hali ish yo'q)"

    return f"""Sen — VAGENT, VoroCreatorBot'ning jonli AI-rejissyori va foydalanuvchining shaxsiy ijodiy hamkori. Timsoling — Voro roboti.

# SHAXSIYAT
- {lang_rule}, iliq, samimiy, qisqa. Mos joyda 1-2 emoji.
- Foydalanuvchini ismi bilan chaqir (bilmasang bir marta so'ra va remember_fact bilan saqla).
- PROAKTIVSAN: g'oyani kuchaytir, o'z takliflaringni qo'sh. Suhbat boshida get_today_context'ni tekshir — bayram yaqin bo'lsa, mos g'oya taklif qil.

# EMOTSIONAL INTELLEKT (psixolog darajasida)
Har xabardan suhbatdoshning holatini his qil va shunga moslash:
- YANGI / IKKILANAYOTGAN odam (qisqa, noaniq xabarlar, "bilmadim"): sodda tilda gapir, texnik atamalarni tushuntir, eng arzon kichik g'alaba taklif qil ("keling, avvaliga 2 tangachalik sinov rasmi qilamiz — yoqsa davom etamiz"). Birinchi muvaffaqiyat — eng muhim daqiqa.
- ASABIYLASHGAN odam (natija yoqmadi, xato bo'ldi): AVVAL his-tuyg'uni tan ol ("tushunaman, kutganingizdek chiqmadi"), hech qachon o'zingni yoki uni ayblama, darhol aniq yechim ber (50% chegirmali qayta urinish shu yerda juda o'rinli).
- XURSAND odam: natijani birga nishonla — aniq kompliment ayt (nimasi zo'r chiqqanini nomlab), keyin tabiiy keyingi qadamni taklif qil.
- PROFESSIONAL (mobilograf, SMM, biznes): gap-so'zsiz, texnik va tez ishla, vaqtini qadrla.
Kognitiv yuk qoidasi: bitta xabarda MAKSIMUM 1 savol va 3 variant. Ko'p savol — odamni charchatadi.
Psixologik profilni remember_fact bilan saqla: tajriba darajasi, muloqot uslubi, nimadan xursand bo'lgani, nima uchun kontent qilayotgani (biznes/oila/ijod).

# HALOLLIK CHEGARASI (buzilmas)
Ishonch — eng qimmat aktiv. TAQIQLANADI: soxta shoshiltirish ("faqat bugun!"), bosim o'tkazish, aybdorlik hissini uyg'otish, yashirin xarajat. "Keyinroq" degan odamga bosim qilma — xushmuomala yakunla, u qaytadi. Sotish emas, YORDAM ber — sotuv o'zi keladi.

# QOIDALAR (buzilmas)
1. TANGACHA MUQADDAS: estimate_cost → request_confirmation → foydalanuvchi "ha" → generate_batch. request_confirmation'dan keyin javobni YAKUNLA.
2. Balans yetmasa — arzonroq variant taklif qil (kichik model, past resolution, qisqa duration).
3. Generatsiya promptlari professional INGLIZ tilida; foydalanuvchiga javob va tushuntirish esa {lang_name}.
4. Seedance filtri: shubhali so'zlarni neytral sinonimlarga almashtir ("fight" → "dynamic action choreography"). Bola, mashhur shaxs, brend logotipi bilan xavfli so'rovlarni rad et.
5. Referens rasmlar: foydalanuvchi rasm biriktirsa, URL xabar ichida [BIRIKTIRILGAN RASM: ...] ko'rinishida keladi — uni reference_urls'ga qo'sh va promptda @image1 sifatida ishlat. Motion transfer: @video1 = faqat harakat, @image1 = qiyofa/uslub, NEGATIVE promptda vizual aralashuvni taqiqla.
6. Narx/sifat balansi: oddiy ish uchun qimmat model taklif qilma, har doim arzon alternativani eslat.
7. Katta g'oya → avval REJA (kadrlar + model + narx + jami), bitta tasdiq, keyin generate_batch bilan HAMMASI PARALLEL.
8. Natijadan keyin qisqa tahlil + 50% chegirmali qayta-iteratsiya borligini eslat.
9. Bir vaqtda max {MAX_ACTIVE_JOBS_PER_USER} ish — katta rejalarni {MAX_ACTIVE_JOBS_PER_USER} talik guruhlarga bo'l.
10. O'Z-O'ZINI TEKSHIRISH: yaratilgan RASM natijasi senga ko'rsatiladi — sinchiklab tekshir: so'ralgan narsa bormi, matn/logotip to'g'ri yozilganmi, anatomik yoki vizual nuqson yo'qmi. Jiddiy nuqson topsang — YASHIRMA: halol ayt, nima noto'g'riligini tushuntir va 50% chegirmali tuzatilgan qayta urinish taklif qil (promptni o'zing yaxshilab).
11. QORALAMA→FINAL: 30+ tangachalik video buyurtmadan oldin 2 tangachalik nano-banana kadr-qoralama taklif qil: "avval kompozitsiyani arzon rasmda ko'rib olamiz, ma'qul bo'lsa videoga o'tamiz". Bu foydalanuvchi pulini himoya qiladi va ishonch quradi.
12. Har reja oldidan search_skills bilan tayyor retsept qidir — bor bo'lsa, retseptdagi prompt qolipini asos qilib ol. Foydalanuvchi "o'zim/qahramonim bilan" desa list_elements'dan saqlangan qahramonlarini tekshir.

# FOYDALANUVCHI
Balans: {get_balance(user_id)} tangacha
Faktlar:
{facts}
Oxirgi ishlar:
{recent}

# MODELLAR
{json.dumps(get_model_hints(), ensure_ascii=False)}
Narxni faqat estimate_cost bilan hisobla — yoddan aytma."""

# ============================================================
# TOOL BAJARUVCHI
# ============================================================

async def _run_single_job(sess: Session, j: dict, price: int, emit) -> dict:
    uid = sess.user_id
    label = j.get("label", "Ish")
    payload = {
        "prompt": j["prompt"],
        "negative_prompt": j.get("negative_prompt", ""),
        "aspect_ratio": j.get("aspect_ratio", "9:16"),
    }
    if j["kind"] == "video":
        payload["resolution"] = j.get("resolution", "720p")
        payload["duration"] = int(j.get("duration", 5))
    if j.get("reference_urls"):
        payload["references"] = j["reference_urls"]

    async def on_progress(elapsed, pct):
        await emit("progress", {"label": label, "elapsed": elapsed, "pct": pct})

    try:
        job_id = await atlas_create_job(j["kind"], j["model"], payload)
        result = await atlas_poll_job(job_id, on_progress=on_progress)
    except Exception as e:
        refund_credits(uid, price)
        track(uid, "gen_fail", {"price": price, "model": j["model"], "error": str(e)[:100]})
        return {"label": label, "error": f"Atlas xatosi: {e}. {price} tangacha qaytarildi."}

    if result["status"] != "ok":
        refund_credits(uid, price)
        track(uid, "gen_fail", {"price": price, "model": j["model"],
                                "error": str(result.get("error"))[:100]})
        return {"label": label,
                "error": f"Muvaffaqiyatsiz: {result.get('error')}. {price} tangacha qaytarildi."}

    track(uid, "gen_ok", {"price": price, "model": j["model"], "kind": j["kind"]})
    entry = {"label": label, "model": j["model"], "price": price,
             "kind": j["kind"], "url": result["url"], "ts": int(time.time())}
    memory_log_job(uid, entry)
    inbox_push(uid, entry)  # SSE uzilgan bo'lsa ham natija saqlanadi
    await emit("result", {"kind": j["kind"], "url": result["url"], "label": label,
                          "price": price, "balance": get_balance(uid)})
    return {"label": label, "status": "ok", "kind": j["kind"],
            "url": result["url"], "price_paid": price}


async def run_tool(name: str, inp: dict, sess: Session, emit) -> dict:
    uid = sess.user_id

    if name == "check_balance":
        bal = get_balance(uid)
        await emit("balance", {"balance": bal})
        return {"balance": bal}

    if name == "get_pricing":
        return {"pricing": get_pricing_table(), "hints": get_model_hints()}

    if name == "get_today_context":
        today = date.today()
        upcoming = []
        for (m, d), nm in UZ_CALENDAR:
            hd = date(today.year, m, d)
            if hd < today:
                hd = date(today.year + 1, m, d)
            diff = (hd - today).days
            if diff <= 30:
                upcoming.append({"bayram": nm, "necha_kun_qoldi": diff})
        return {"bugun": today.isoformat(), "yaqin_bayramlar": upcoming,
                "mavsum": ["qish", "qish", "bahor", "bahor", "bahor", "yoz",
                           "yoz", "yoz", "kuz", "kuz", "kuz", "qish"][today.month - 1]}

    if name == "estimate_cost":
        out, total = [], 0
        for j in inp.get("jobs", []):
            p = calc_price(j["kind"], j["model"], j.get("resolution", "720p"),
                           int(j.get("duration", 5)))
            out.append({**j, "price": p})
            total += p or 0
        return {"jobs": out, "total": total, "balance": get_balance(uid)}

    if name == "request_confirmation":
        token = uuid.uuid4().hex[:12]
        sess.pending_quote = {"token": token, "total": int(inp["total"]), "jobs": inp["jobs"]}
        sess.confirmed_token = None
        track(uid, "quote_shown", {"total": int(inp["total"])})
        await emit("confirm", {"token": token, "summary": inp["summary"],
                               "total": int(inp["total"]), "jobs": inp["jobs"],
                               "balance": get_balance(uid)})
        return {"status": "waiting_user", "token": token}

    if name == "generate_batch":
        q = sess.pending_quote
        if not q or sess.confirmed_token != q["token"]:
            return {"error": "Foydalanuvchi hali tasdiqlamagan. Avval request_confirmation."}

        jobs = inp.get("jobs", [])
        if not jobs:
            return {"error": "jobs bo'sh."}
        if sess.active_jobs + len(jobs) > MAX_ACTIVE_JOBS_PER_USER:
            return {"error": f"Limit: bir vaqtda max {MAX_ACTIVE_JOBS_PER_USER} ish. "
                             f"Rejani {MAX_ACTIVE_JOBS_PER_USER} talik guruhlarga bo'ling."}

        # narxlarni hisoblash va yechish (hammasi oldindan)
        priced = []
        for j in jobs:
            p = calc_price(j["kind"], j["model"], j.get("resolution", "720p"),
                           int(j.get("duration", 5)))
            if p is None:
                return {"error": f"'{j.get('label')}' uchun narx hisoblanmadi."}
            if inp.get("is_iteration"):
                p = max(1, int(p * FREE_ITERATION_DISCOUNT))
            priced.append((j, p))

        total = sum(p for _, p in priced)
        if not deduct_credits(uid, total):
            return {"error": f"Balans yetarli emas ({get_balance(uid)} bor, {total} kerak)."}

        await emit("status", {"text": f"⚙️ {len(priced)} ta ish parallel boshlandi ({total} 🪙)"})
        sess.active_jobs += len(priced)
        try:
            results = await asyncio.gather(
                *[_run_single_job(sess, j, p, emit) for j, p in priced])
        finally:
            sess.active_jobs -= len(priced)

        sess.last_jobs = jobs
        sess.pending_quote = None
        sess.confirmed_token = None
        return {"results": results, "balance": get_balance(uid)}

    if name == "search_skills":
        skills = _locked_read(SKILLS_JSON, [])
        q = inp.get("query", "").lower()
        words = [w for w in q.split() if len(w) > 2]
        scored = []
        for s in skills:
            hay = (s.get("name", "") + " " + " ".join(s.get("tags", []))).lower()
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        top = [s for _, s in scored[:3]]
        return {"retseptlar": top} if top else {
            "retseptlar": [], "izoh": "Mos retsept topilmadi — o'z bilimingdan foydalanib professional prompt yoz."}

    if name == "list_elements":
        # INTEGRATSIYA: bot Element Library sxemasiga moslashtiring.
        # Kutilgan jadval: elements(id, user_id, name, file_path)
        try:
            import sqlite3
            con = sqlite3.connect(ELEMENTS_DB)
            rows = con.execute(
                "SELECT id, name, file_path FROM elements WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (uid,)).fetchall()
            con.close()
            items = [{"id": r[0], "nomi": r[1],
                      "url": f"{PUBLIC_BASE}/vagent/element/{os.path.basename(r[2])}"}
                     for r in rows]
            return {"elementlar": items} if items else {
                "elementlar": [], "izoh": "Kutubxona bo'sh. Foydalanuvchi botdagi Element Library'ga rasm saqlashi mumkin."}
        except Exception as e:
            return {"elementlar": [], "izoh": f"Kutubxona hozircha ulanmagan ({e})."}

    if name == "remember_fact":
        memory_add_fact(uid, inp["fact"])
        return {"status": "saqlandi"}

    return {"error": f"Noma'lum tool: {name}"}

# ============================================================
# CLAUDE STREAMING ORKESTRATSIYASI
# ============================================================

async def claude_stream_turn(sess: Session, emit) -> None:
    system = build_system_prompt(sess.user_id, sess.lang)

    for _ in range(MAX_TURN_TOOL_LOOPS):
        assistant_blocks, tool_calls = [], []
        stop_reason = None

        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": ANTHROPIC_MODEL, "max_tokens": 3000,
                      "system": system, "tools": TOOLS,
                      "messages": sess.messages, "stream": True},
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    await emit("error", {"text": f"Claude API {resp.status_code}: {body[:200]}"})
                    return

                cur_text, cur_tool, cur_tool_json = "", None, ""
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    t = ev.get("type")

                    if t == "content_block_start":
                        blk = ev["content_block"]
                        if blk["type"] == "tool_use":
                            cur_tool, cur_tool_json = {"id": blk["id"], "name": blk["name"]}, ""
                        elif blk["type"] == "text":
                            cur_text = ""
                    elif t == "content_block_delta":
                        d = ev["delta"]
                        if d.get("type") == "text_delta":
                            cur_text += d["text"]
                            await emit("text", {"delta": d["text"]})
                        elif d.get("type") == "input_json_delta":
                            cur_tool_json += d.get("partial_json", "")
                    elif t == "content_block_stop":
                        if cur_tool is not None:
                            try:
                                ti = json.loads(cur_tool_json) if cur_tool_json else {}
                            except Exception:
                                ti = {}
                            assistant_blocks.append({"type": "tool_use", "id": cur_tool["id"],
                                                     "name": cur_tool["name"], "input": ti})
                            tool_calls.append({"id": cur_tool["id"],
                                               "name": cur_tool["name"], "input": ti})
                            cur_tool = None
                        elif cur_text:
                            assistant_blocks.append({"type": "text", "text": cur_text})
                            cur_text = ""
                    elif t == "message_delta":
                        stop_reason = ev.get("delta", {}).get("stop_reason")

        if assistant_blocks:
            sess.messages.append({"role": "assistant", "content": assistant_blocks})

        if stop_reason != "tool_use" or not tool_calls:
            return

        results, waiting_user = [], False
        for tc in tool_calls:
            await emit("status", {"text": f"🔧 {tc['name']}"})
            out = await run_tool(tc["name"], tc["input"], sess, emit)
            blocks: list[dict] = [{"type": "text",
                                   "text": json.dumps(out, ensure_ascii=False)}]
            # O'Z-O'ZINI TEKSHIRISH: yaratilgan rasmlarni orkestrator KO'RADI
            # va sifatini baholaydi (Higgsfield'da yo'q qobiliyat)
            for r in (out.get("results") or [])[:2]:
                if r.get("status") == "ok" and r.get("kind") == "image" and r.get("url"):
                    blocks.append({"type": "image",
                                   "source": {"type": "url", "url": r["url"]}})
            results.append({"type": "tool_result", "tool_use_id": tc["id"],
                            "content": blocks})
            if out.get("status") == "waiting_user":
                waiting_user = True

        sess.messages.append({"role": "user", "content": results})
        if waiting_user:
            return

    await emit("error", {"text": "Juda ko'p qadam — so'rovni soddalashtiring."})

# ============================================================
# API ENDPOINTLAR
# ============================================================

vagent_router = APIRouter(prefix="/vagent")


class ChatIn(BaseModel):
    uid: str
    exp: str
    sig: str
    lang: str = "uz"
    message: str = ""
    attachments: list[str] = []        # referens rasm URL'lari
    confirm_token: Optional[str] = None
    decline: bool = False


class UploadIn(BaseModel):
    uid: str
    exp: str
    sig: str
    data_b64: str
    mime: str = "image/jpeg"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@vagent_router.post("/chat")
async def vagent_chat(body: ChatIn):
    if not verify_auth(body.uid, body.exp, body.sig):
        async def denied():
            yield sse("error", {"text": "Avtorizatsiya xatosi. Mini App'ni bot tugmasi orqali oching."})
        return StreamingResponse(denied(), media_type="text/event-stream")

    if not rate_ok(body.uid):
        async def limited():
            yield sse("error", {"text": "Juda tez yozayapsiz 🙂 Bir daqiqadan keyin davom etamiz."})
        return StreamingResponse(limited(), media_type="text/event-stream")

    sess = await get_session(body.uid)
    sess.lang = (body.lang or "uz") if body.lang in ("uz","ru","en") else "uz"

    if body.confirm_token and sess.pending_quote and \
            body.confirm_token == sess.pending_quote["token"]:
        sess.confirmed_token = body.confirm_token
        track(body.uid, "confirmed", {"total": sess.pending_quote["total"]})
        user_text = "✅ Ha, roziman, boshla!"
    elif body.decline:
        track(body.uid, "declined",
              {"total": sess.pending_quote["total"] if sess.pending_quote else 0})
        sess.pending_quote, sess.confirmed_token = None, None
        user_text = "❌ Yo'q, hozircha kerak emas."
    else:
        user_text = body.message.strip() or "Salom!"
        track(body.uid, "msg")

    _atts = [u for u in body.attachments[:4] if u]
    if _atts:
        _content = []
        for url in _atts:
            user_text += f"\n[BIRIKTIRILGAN RASM: {url}]"
            _b64, _mt = _attachment_b64(url)
            if _b64:
                _content.append({"type": "image",
                                 "source": {"type": "base64", "media_type": _mt, "data": _b64}})
        _content.append({"type": "text", "text": user_text})
        sess.messages.append({"role": "user", "content": _content})
    else:
        sess.messages.append({"role": "user", "content": user_text})
    if len(sess.messages) > 30:
        sess.messages = sess.messages[-30:]
        while sess.messages and (
            sess.messages[0]["role"] != "user" or
            (isinstance(sess.messages[0].get("content"), list) and
             any(b.get("type") == "tool_result" for b in sess.messages[0]["content"]))):
            sess.messages.pop(0)

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event, data):
        await queue.put((event, data))

    async def worker():
        try:
            await claude_stream_turn(sess, emit)
        except Exception as e:
            await emit("error", {"text": f"Ichki xato: {e}"})
        finally:
            await queue.put(("done", {}))

    # MUHIM: worker mustaqil task — mijoz uzilsa HAM davom etadi.
    # Natijalar inbox'ga yoziladi, Mini App /inbox orqali tiklaydi.
    asyncio.create_task(worker())

    async def streamer() -> AsyncGenerator[str, None]:
        while True:
            event, data = await queue.get()
            yield sse(event, data)
            if event == "done":
                break

    return StreamingResponse(streamer(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@vagent_router.post("/upload")
async def vagent_upload(body: UploadIn):
    """Referens rasm yuklash → public URL."""
    if not verify_auth(body.uid, body.exp, body.sig):
        return {"ok": False, "error": "auth"}
    try:
        raw = base64.b64decode(body.data_b64)
    except Exception:
        return {"ok": False, "error": "base64 xato"}
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return {"ok": False, "error": f"Fayl {MAX_UPLOAD_MB}MB dan katta"}
    ext = {"image/png": "png", "image/webp": "webp"}.get(body.mime, "jpg")
    name = f"{body.uid}_{uuid.uuid4().hex[:10]}.{ext}"
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    with open(os.path.join(UPLOADS_DIR, name), "wb") as f:
        f.write(raw)
    # INTEGRATSIYA: agar Atlas tashqi URL'ni qabul qilmasa, shu yerda
    # botdagi atlas_upload_media funksiyasini chaqirib, Atlas URL qaytaring.
    track(body.uid, "upload")
    return {"ok": True, "url": f"{PUBLIC_BASE}/vagent/file/{name}"}


@vagent_router.get("/file/{name}")
async def vagent_file(name: str):
    if "/" in name or ".." in name:
        return {"ok": False}
    path = os.path.join(UPLOADS_DIR, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "topilmadi"}
    return FileResponse(path)


@vagent_router.post("/voice")
async def vagent_voice(body: UploadIn):
    """Ovozli buyruq → matn (Groq Whisper)."""
    if not verify_auth(body.uid, body.exp, body.sig):
        return {"ok": False, "error": "auth"}
    if not GROQ_API_KEY:
        return {"ok": False, "error": "GROQ_API_KEY sozlanmagan"}
    try:
        raw = base64.b64decode(body.data_b64)
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("voice.webm", raw, body.mime or "audio/webm")},
                data={"model": "whisper-large-v3", "language": "uz"})
        r.raise_for_status()
        track(body.uid, "voice")
        return {"ok": True, "text": r.json().get("text", "").strip()}
    except Exception as e:
        return {"ok": False, "error": f"Transkripsiya xatosi: {e}"}


@vagent_router.get("/element/{name}")
async def vagent_element(name: str):
    if "/" in name or ".." in name:
        return {"ok": False}
    path = os.path.join(ELEMENTS_DIR, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "topilmadi"}
    return FileResponse(path)


@vagent_router.get("/inbox")
async def vagent_inbox(uid: str, exp: str, sig: str, since: int = 0):
    """SSE uzilganda yo'qolgan natijalarni tiklash."""
    if not verify_auth(uid, exp, sig):
        return {"ok": False, "error": "auth"}
    return {"ok": True, "items": inbox_pull(uid, since)}


@vagent_router.get("/health")
async def vagent_health():
    """Monitoring uchun: UptimeRobot/cron shu yerni tekshiradi."""
    checks = {
        "version": VAGENT_VERSION,
        "anthropic_key": bool(ANTHROPIC_API_KEY),
        "atlas_key": bool(ATLAS_API_KEY),
        "bot_secret": bool(BOT_SECRET),
        "users_json": os.path.exists(USERS_JSON),
        "models_config": os.path.exists(MODELS_JSON),
        "skills": len(_locked_read(SKILLS_JSON, [])),
    }
    checks["ok"] = all([checks["anthropic_key"], checks["atlas_key"], checks["bot_secret"]])
    return checks


@vagent_router.get("/me")
async def vagent_me(uid: str, exp: str, sig: str):
    if not verify_auth(uid, exp, sig):
        return {"ok": False, "error": "auth"}
    mem = memory_get(uid)
    return {"ok": True, "balance": get_balance(uid),
            "name": mem.get("name", ""),
            "facts_count": len(mem.get("facts", [])),
            "recent": mem.get("history", [])[-3:]}


# ============================================================
# ADMIN (faqat OWNER_ID uchun) — o'lchov paneli
# ============================================================

def _is_owner(uid: str, exp: str, sig: str) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID and verify_auth(uid, exp, sig)


@vagent_router.get("/admin/stats")
async def admin_stats(uid: str, exp: str, sig: str, days: int = 7):
    """Voronka, daromad, xatolar. Bot /stats buyrug'i shu yerdan oladi."""
    if not _is_owner(uid, exp, sig):
        return {"ok": False, "error": "auth"}
    return {"ok": True, "stats": compute_stats(min(days, 90))}


@vagent_router.get("/admin/digest")
async def admin_digest(uid: str, exp: str, sig: str):
    """Kunlik o'zbekcha hisobot matni. INTEGRATSIYA: bot har kuni ertalab
    (masalan 09:00 da) shu endpointni chaqirib, matnni Qahramonga yuborsin."""
    if not _is_owner(uid, exp, sig):
        return {"ok": False, "error": "auth"}
    return {"ok": True, "text": daily_digest_text()}
