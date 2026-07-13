# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════
 voro.uz — backend API (FastAPI)
════════════════════════════════════════════════════════════════════
 Ishga tushirish:
   uvicorn voro_web_api:app --host 127.0.0.1 --port 8788

 Kerakli kutubxonalar:
   pip install fastapi "uvicorn[standard]" httpx python-multipart

 ⚠️ DISPATCH UCHUN ENG MUHIM TODO:
   atlas_submit() va atlas_poll() funksiyalarini voro_creator_bot.py
   dagi MAVJUD Atlas Cloud chaqiruvlari bilan moslashtir (pastda
   "DISPATCH TODO" deb belgilangan). Qolgan hamma narsa tayyor.

 Frontend bilan kontrakt (VoroWebApp.jsx, API_BASE='/api'):
   POST /auth/register {email,password,name} -> {name,email,balance}
   POST /auth/login    {email,password}      -> {name,email,balance}
   POST /auth/logout
   GET  /me                                  -> {name,email,balance}
   POST /upload (multipart file)             -> {upload_id,url}
   POST /generate {mid,res,asp,dur,prompt,refs[]} -> {job_id,price,balance}
   GET  /jobs/{id}   -> {status,progress,result_url,error,balance}
   GET  /history     -> [{id,type,name,emoji,price,url}]
   POST /pay/create {pkg,method}             -> {url}
   POST /pay/payme          (Payme Merchant JSON-RPC)
   POST /pay/click/prepare  POST /pay/click/complete (Click SHOP-API)
════════════════════════════════════════════════════════════════════
"""
import os
import json
import time
import uuid
import hmac
import base64
import sqlite3
import hashlib
import asyncio
import threading
import contextvars
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ────────────────────────── KONFIG (.env / systemd Environment) ──
SECRET = os.getenv("VORO_SECRET", "")
if not SECRET or "O'ZGARTIR" in SECRET or len(SECRET) < 24:
    raise RuntimeError(
        "VORO_SECRET o'rnatilmagan yoki juda qisqa! "
        ".env ga qo'ying: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
DB_PATH = os.getenv("VORO_DB", "voro_web.db")
_bg_tasks = set()   # M4: fon tasklar havolasi — garbage collectordan himoya
UPLOAD_DIR = Path(os.getenv("VORO_UPLOADS", "uploads"))
MEDIA_DIR = Path(os.getenv("VORO_MEDIA", "media"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://voro.uz")  # Atlas'ga reference URL berish uchun
SIGNUP_BONUS = int(os.getenv("SIGNUP_BONUS", "3"))  # ro'yxatdan o'tganda bepul tangacha
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"  # lokal testda 0 qiling

ATLAS_API_KEY = os.getenv("ATLAS_API_KEY", "")
ATLAS_BASE_URL = os.getenv("ATLAS_BASE_URL", "https://api.atlascloud.ai")  # DISPATCH TODO: bot bilan solishtir

PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_KEY = os.getenv("PAYME_KEY", "")            # ishchi kalit
PAYME_TEST_KEY = os.getenv("PAYME_TEST_KEY", "")  # sandbox kaliti
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")

# Bot <-> sayt ichki integratsiya (yagona hamyon)
INTERNAL_KEY = os.getenv("VORO_INTERNAL_KEY", "")  # bot va sayt o'rtasidagi maxfiy kalit
BOT_USERNAME = os.getenv("BOT_USERNAME", "VoroCreatorBot")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()  # admin panel egasi
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")  # Google Sign-In (ID-token tekshirish)

TOKEN_TTL = 30 * 24 * 3600  # 30 kun

# ────────────────────────── PAKETLAR (frontend bilan BIR XIL!) ──
# TODO (Qahramon): yakuniy so'm narxlarini tasdiqla
PACKAGES = [
    {"t": 20, "b": 0, "uzs": 14900},
    {"t": 60, "b": 10, "uzs": 39900},
    {"t": 150, "b": 30, "uzs": 89900},
    {"t": 400, "b": 120, "uzs": 219900},
]

# ────────────────────────── MODELLAR (bot bilan mos, 2026-07-02) ──
# web=False -> saytda o'chirilgan (video/audio yuklash talab qiladi)
MODELS = {
    "google/veo3.1/reference-to-video": {"type": "video", "name": "Veo 3.1", "emoji": "🎥", "res": ["720p", "1080p", "4k"], "asp": [], "dur": [8], "refs": 3, "refs_req": True, "web": True},
    "google/veo3.1-fast/image-to-video": {"type": "video", "name": "Veo 3.1 Fast", "emoji": "🎥", "res": ["720p", "1080p", "4k"], "asp": ["16:9", "9:16"], "dur": [4, 6, 8], "refs": 2, "refs_req": False, "web": True},
    "google/veo3.1-lite/image-to-video": {"type": "video", "name": "Veo 3.1 Lite", "emoji": "🎥", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [4, 6, 8], "refs": 1, "refs_req": True, "web": True},
    "google/veo3.1-lite/start-end-frame-to-video": {"type": "video", "name": "Veo 3.1 Start/End", "emoji": "🎥", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [8], "refs": 2, "refs_req": True, "web": True},
    "google/gemini-omni-flash/reference-to-video": {"type": "video", "name": "Gemini Omni Flash", "emoji": "🔮", "res": ["720p"], "asp": ["16:9", "9:16"], "dur": [3, 4, 5, 6, 7, 8, 9, 10], "refs": 5, "refs_req": True, "web": True},
    "google/gemini-omni-flash/image-to-video": {"type": "video", "name": "Gemini Omni Image", "emoji": "🔮", "res": ["720p"], "asp": ["16:9", "9:16"], "dur": [3, 4, 5, 6, 7, 8, 9, 10], "refs": 1, "refs_req": True, "web": True},
    "google/gemini-omni-flash/video-edit": {"type": "video", "name": "Gemini Omni Video Edit", "emoji": "🎬", "res": ["720p"], "asp": [], "dur": [], "refs": 6, "refs_req": True, "web": True, "price_formula": "video_sec"},
    "google/gemini-omni-flash/text-to-video": {"type": "video", "name": "Gemini Omni Text", "emoji": "🔮", "res": ["720p"], "asp": ["16:9", "9:16"], "dur": [3, 4, 5, 6, 7, 8, 9, 10], "refs": 0, "refs_req": False, "web": True},
    "bytedance/seedance-2.0/text-to-video": {"type": "video", "name": "Seedance 2.0", "emoji": "🌱", "res": ["480p", "720p", "1080p", "1440p"], "asp": ["adaptive", "16:9", "9:16", "1:1", "4:3"], "dur": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15], "refs": 7, "refs_req": False, "web": True},
    "bytedance/seedance-2.0-fast/reference-to-video": {"type": "video", "name": "Seedance 2.0 Fast", "emoji": "⚡", "res": ["480p", "720p", "1080p"], "asp": ["adaptive", "16:9", "9:16", "1:1", "4:3"], "dur": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15], "refs": 9, "refs_req": False, "web": True},
    "bytedance/seedance-v1.5-pro/image-to-video-fast": {"type": "video", "name": "Seedance 1.5 Pro", "emoji": "🌱", "res": ["480p", "720p", "1080p"], "asp": ["adaptive", "16:9", "9:16", "1:1"], "dur": [4, 5, 6, 8, 10, 12], "refs": 1, "refs_req": True, "web": True},
    "kwaivgi/kling-v3.0-std/image-to-video": {"type": "video", "name": "Kling 3.0", "emoji": "⚡️", "res": ["720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [3, 4, 5, 6, 8, 10, 12, 15], "refs": 1, "refs_req": True, "web": True},
    "kwaivgi/kling-video-o3-pro/video-edit": {"type": "video", "name": "Kling O3 Video", "emoji": "🖥️", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [], "refs": 1, "refs_req": True, "web": False},
    "kwaivgi/kling-v2.6-std/motion-control": {"type": "video", "name": "Kling Motion Control", "emoji": "🤿", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [], "refs": 2, "refs_req": True, "web": False},
    "kwaivgi/kling-v2.5-turbo-pro/image-to-video": {"type": "video", "name": "Kling 2.5 Turbo", "emoji": "⚡️", "res": ["720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [5, 10], "refs": 1, "refs_req": True, "web": True},
    "atlascloud/infinitetalk": {"type": "video", "name": "InfiniteTalk", "emoji": "🗣", "res": ["480p", "720p"], "asp": ["16:9", "9:16", "1:1"], "dur": [], "refs": 1, "refs_req": True, "web": False},
    "kwaivgi/kling-v2.6-pro/image-to-video": {"type": "video", "name": "Kling 2.6", "emoji": "⚡", "res": ["720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [5, 10], "refs": 1, "refs_req": True, "web": True},
    "alibaba/wan-2.7/text-to-video": {"type": "video", "name": "Wan 2.7", "emoji": "🌀", "res": ["480p", "720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [2, 3, 4, 5, 6, 8, 10, 12, 15], "refs": 0, "refs_req": False, "web": True},
    "alibaba/wan-2.7/image-to-video": {"type": "video", "name": "Wan 2.7 I2V", "emoji": "🌀", "res": ["480p", "720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [2, 3, 4, 5, 6, 8, 10, 12, 15], "refs": 1, "refs_req": True, "web": True},
    "minimax/hailuo-2.3/t2v-standard": {"type": "video", "name": "Hailuo 2.3", "emoji": "🌊", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [6, 10], "refs": 0, "refs_req": False, "web": True},
    "minimax/hailuo-2.3/fast": {"type": "video", "name": "Hailuo 2.3 I2V", "emoji": "🌀", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [6, 10], "refs": 1, "refs_req": True, "web": True},
    "xai/grok-imagine-video-v1.5/image-to-video": {"type": "video", "name": "Grok Video 1.5", "emoji": "🤖", "res": ["720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [2, 3, 4, 5, 6, 8], "refs": 1, "refs_req": True, "web": True},
    "alibaba/happyhorse-1.0/text-to-video": {"type": "video", "name": "HappyHorse 1.0", "emoji": "🐴", "res": ["720p", "1080p"], "asp": ["16:9", "9:16"], "dur": [1, 2, 3, 4, 5, 6, 8], "refs": 0, "refs_req": False, "web": True},
    "vidu/q3/reference-to-video": {"type": "video", "name": "Vidu Q3", "emoji": "🎬", "res": ["720p", "1080p"], "asp": ["16:9", "9:16", "1:1"], "dur": [1, 2, 3, 4, 5, 6, 8], "refs": 3, "refs_req": True, "web": True},
    "google/nano-banana-2/edit": {"type": "image", "name": "Nano Banana 2", "emoji": "🍌", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16", "3:2", "2:3", "4:3"], "dur": [], "refs": 3, "refs_req": False, "web": True},
    "google/nano-banana-pro/edit": {"type": "image", "name": "Nano Banana Pro", "emoji": "🍌", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16", "3:2", "2:3", "4:3"], "dur": [], "refs": 4, "refs_req": False, "web": True},
    "openai/gpt-image-2/text-to-image": {"type": "image", "name": "GPT Image 2", "emoji": "🤖", "res": ["low", "medium", "high"], "asp": ["1:1", "3:2", "2:3"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "bytedance/seedream-v5.0-lite": {"type": "image", "name": "Seedream 5.0", "emoji": "🌸", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16", "4:3", "3:4"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "alibaba/wan-2.7/text-to-image": {"type": "image", "name": "Wan 2.7 Image", "emoji": "🌀", "res": ["512", "1024", "2048"], "asp": ["1:1", "16:9", "9:16"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "alibaba/wan-2.7-pro/text-to-image": {"type": "image", "name": "Wan 2.7 Pro", "emoji": "🌀", "res": ["512", "1024", "2048"], "asp": ["1:1", "16:9", "9:16", "4:3"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "xai/grok-imagine-image-quality/text-to-image": {"type": "image", "name": "Grok Image", "emoji": "🤖", "res": ["1k", "2k"], "asp": ["1:1", "16:9", "9:16"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "qwen/qwen-image-2.0/text-to-image": {"type": "image", "name": "Qwen Image 2.0", "emoji": "🔮", "res": ["1k", "2k"], "asp": ["1:1", "16:9", "9:16"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "black-forest-labs/flux-1.1-pro": {"type": "image", "name": "Flux Pro 2.0", "emoji": "⚡", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16", "3:2", "2:3"], "dur": [], "refs": 0, "refs_req": False, "web": True},
    "black-forest-labs/flux-kontext-max": {"type": "image", "name": "Flux Kontext", "emoji": "⚡", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16"], "dur": [], "refs": 1, "refs_req": False, "web": True},
    "bytedance/seedream-v4.5/text-to-image": {"type": "image", "name": "Seedream 4.5", "emoji": "🌸", "res": ["1k", "2k", "4k"], "asp": ["1:1", "16:9", "9:16", "4:3", "3:4"], "dur": [], "refs": 0, "refs_req": False, "web": True},
}

# ────────────────────────── NARXLAR (tangachada; bot bilan mos) ──
def _custom_rows():
    try:
        return q("SELECT mid, config, enabled FROM custom_models")
    except Exception:
        return []


def _custom_to_meta(cfg):
    """Admin kiritgan config -> MODELS meta formati."""
    return {
        "type": cfg.get("type", "video"),
        "name": cfg.get("name", "Model"),
        "emoji": cfg.get("emoji", "✨"),
        "res": cfg.get("res") or [],
        "asp": cfg.get("asp") or [],
        "dur": cfg.get("dur") or [],
        "refs": int(cfg.get("refs") or 0),
        "refs_req": bool(cfg.get("refs_req")),
        "web": True,
        "custom": True,
        "ref_types": cfg.get("ref_types") or ["image"],
        "payload_map": cfg.get("payload_map") or {},
        "pricing": cfg.get("pricing") or {},
        "price_formula": "video_sec" if (cfg.get("pricing") or {}).get("per_sec") else None,
        "hook": cfg.get("hook", ""),
    }


def get_model(mid):
    """Statik MODELS + admin qo'shgan custom modellar."""
    m = MODELS.get(mid)
    if m:
        return m
    row = q("SELECT config, enabled FROM custom_models WHERE mid=?", (mid,), one=True)
    if row and row["enabled"]:
        try:
            return _custom_to_meta(json.loads(row["config"]))
        except Exception:
            return None
    return None


PRICING = {
    "google/veo3.1/reference-to-video": {"dur": {8: 191}},
    "google/veo3.1-fast/image-to-video": {"dur": {4: 22, 6: 33, 8: 43}},
    "google/veo3.1-lite/image-to-video": {"dur": {4: 12, 6: 18, 8: 24}},
    "google/veo3.1-lite/start-end-frame-to-video": {"dur": {8: 24}},
    "google/gemini-omni-flash/reference-to-video": {"dur": {3: 27, 4: 36, 5: 45, 6: 54, 7: 63, 8: 72, 9: 81, 10: 90}},
    "google/gemini-omni-flash/image-to-video": {"dur": {3: 27, 4: 36, 5: 45, 6: 54, 7: 63, 8: 72, 9: 81, 10: 90}},
    "google/gemini-omni-flash/video-edit": {"per_sec": 10},
    "google/gemini-omni-flash/text-to-video": {"dur": {3: 24, 4: 32, 5: 40, 6: 48, 7: 56, 8: 64, 9: 72, 10: 80}},
    "bytedance/seedance-2.0/text-to-video": {"dur": {1: 6, 2: 12, 3: 18, 4: 24, 5: 30, 6: 36, 7: 42, 8: 48, 9: 54, 10: 60, 12: 72, 15: 90}},
    "bytedance/seedance-2.0-fast/reference-to-video": {"dur": {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 29, 7: 34, 8: 39, 9: 44, 10: 49, 12: 58, 15: 73}},
    "bytedance/seedance-v1.5-pro/image-to-video-fast": {"dur": {4: 12, 5: 14, 6: 17, 8: 23, 10: 28, 12: 34}},
    "kwaivgi/kling-v3.0-std/image-to-video": {"dur": {3: 13, 4: 17, 5: 22, 6: 26, 8: 34, 10: 43, 12: 51, 15: 64}},
    "kwaivgi/kling-v2.5-turbo-pro/image-to-video": {"dur": {5: 21, 10: 42}},
    "kwaivgi/kling-v2.6-pro/image-to-video": {"dur": {5: 24, 10: 48}},
    "alibaba/wan-2.7/text-to-video": {"dur": {2: 9, 3: 13, 4: 17, 5: 21, 6: 25, 8: 34, 10: 42, 12: 50, 15: 63}},
    "alibaba/wan-2.7/image-to-video": {"dur": {2: 9, 3: 13, 4: 17, 5: 21, 6: 25, 8: 34, 10: 42, 12: 50, 15: 63}},
    "minimax/hailuo-2.3/t2v-standard": {"dur": {6: 25, 10: 42}},
    "minimax/hailuo-2.3/fast": {"dur": {6: 25, 10: 42}},
    "xai/grok-imagine-video-v1.5/image-to-video": {"dur": {2: 12, 3: 18, 4: 23, 5: 29, 6: 35, 8: 46}},
    "alibaba/happyhorse-1.0/text-to-video": {"dur": {1: 9, 2: 17, 3: 25, 4: 34, 5: 42, 6: 50, 8: 67}},
    "vidu/q3/reference-to-video": {"dur": {1: 3, 2: 5, 3: 8, 4: 10, 5: 13, 6: 15, 8: 20}},
    "google/nano-banana-2/edit": {"res": {"1k": 3, "2k": 4, "4k": 5}},
    "google/nano-banana-pro/edit": {"res": {"1k": 4, "2k": 6, "4k": 8}},
    "openai/gpt-image-2/text-to-image": {"res": {"low": 1, "medium": 3, "high": 10}},
    "bytedance/seedream-v5.0-lite": {"res": {"1k": 3, "2k": 4, "4k": 6}},
    "alibaba/wan-2.7/text-to-image": {"res": {"512": 1, "1024": 2, "2048": 4}},
    "alibaba/wan-2.7-pro/text-to-image": {"res": {"512": 3, "1024": 5, "2048": 9}},
    "xai/grok-imagine-image-quality/text-to-image": {"res": {"1k": 4, "2k": 8}},
    "qwen/qwen-image-2.0/text-to-image": {"res": {"1k": 2, "2k": 3}},
    "black-forest-labs/flux-1.1-pro": {"res": {"1k": 3, "2k": 5, "4k": 8}},
    "black-forest-labs/flux-kontext-max": {"res": {"1k": 5, "2k": 8, "4k": 11}},
    "bytedance/seedream-v4.5/text-to-image": {"res": {"1k": 3, "2k": 4, "4k": 6}},
}


def price_for(mid, res, dur):
    p = PRICING.get(mid)
    if not p:
        m = get_model(mid)
        p = (m or {}).get("pricing") or None
        if p:
            # JSON kalitlari string bo'ladi — normallash
            if "dur" in p:
                p = {"dur": {int(k): v for k, v in p["dur"].items()}}
    if not p:
        return None
    if "per_sec" in p:
        try:
            d = float(dur or 0)
        except Exception:
            return None
        return int(d * p["per_sec"] + 0.999) if d > 0 else None
    if "dur" in p:
        return p["dur"].get(int(dur)) if dur is not None else None
    if "res" in p:
        return p["res"].get(str(res)) if res is not None else None
    return None


# ────────────────────────── BAZA ──
_lock = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db():
    with _lock:
        d = db()
        d.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pass_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS custom_models(
            mid TEXT PRIMARY KEY,
            config TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS slot_media(
            slot TEXT PRIMARY KEY,
            url TEXT,
            mid TEXT,
            updated INTEGER
        );
        CREATE TABLE IF NOT EXISTS uploads(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            mid TEXT NOT NULL,
            res TEXT, asp TEXT, dur INTEGER,
            prompt TEXT,
            refs_json TEXT,
            price INTEGER NOT NULL,
            status TEXT NOT NULL,          -- queued|processing|done|failed
            progress INTEGER DEFAULT 0,
            result_url TEXT,
            error TEXT,
            atlas_id TEXT,
            created INTEGER NOT NULL,
            updated INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tangacha INTEGER NOT NULL,
            bonus INTEGER NOT NULL,
            amount_uzs INTEGER NOT NULL,
            method TEXT NOT NULL,          -- payme|click|octo
            status TEXT NOT NULL,          -- new|paid|canceled
            payme_id TEXT,
            payme_state INTEGER DEFAULT 0,
            payme_create_ms INTEGER DEFAULT 0,
            payme_perform_ms INTEGER DEFAULT 0,
            payme_cancel_ms INTEGER DEFAULT 0,
            payme_reason INTEGER,
            click_trans_id TEXT,
            created INTEGER NOT NULL,
            paid_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS link_codes(
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires INTEGER NOT NULL
        );
        """)
        try:
            d.execute("ALTER TABLE slot_media ADD COLUMN mid TEXT")
        except Exception:
            pass
        for _col in ("kind TEXT", "duration REAL"):
            try:
                d.execute(f"ALTER TABLE uploads ADD COLUMN {_col}")
            except Exception:
                pass
        try:
            # Eski sxemada url NOT NULL edi — videosiz model saqlash uchun olib tashlaymiz
            info = d.execute("PRAGMA table_info(slot_media)").fetchall()
            url_notnull = any(r[1] == "url" and r[3] == 1 for r in info)
            if url_notnull:
                d.execute("ALTER TABLE slot_media RENAME TO slot_media_old")
                d.execute("CREATE TABLE slot_media(slot TEXT PRIMARY KEY, url TEXT, mid TEXT, updated INTEGER)")
                d.execute("INSERT INTO slot_media(slot,url,mid,updated) SELECT slot,url,mid,updated FROM slot_media_old")
                d.execute("DROP TABLE slot_media_old")
        except Exception:
            pass
        try:
            d.execute("ALTER TABLE users ADD COLUMN telegram_id INTEGER")
        except Exception:
            pass
        for _col in ("google_sub TEXT", "avatar TEXT"):
            try:
                d.execute(f"ALTER TABLE users ADD COLUMN {_col}")
            except Exception:
                pass
        d.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tg ON users(telegram_id)")
        d.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google ON users(google_sub)")
        d.commit()


def q(sql, args=(), one=False, commit=False):
    with _lock:
        cur = db().execute(sql, args)
        if commit:
            db().commit()
            return cur
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


# ────────────────────────── YORDAMCHILAR ──
def err(msg, code=400):
    lang = _REQ_LANG.get()
    if lang in ("en", "ru"):
        msg = ERR_I18N.get(msg, {}).get(lang, msg)
    return JSONResponse({"error": msg}, status_code=code)


def friendly_error(raw) -> str:
    """Texnik xatoni foydalanuvchiga ANIQ SABABLI, ammo backend tafsilotisiz xabarga aylantiradi."""
    if not raw:
        return "Generatsiya amalga oshmadi. Qayta urinib ko'ring — tangalar qaytarildi."
    s = str(raw)
    low = s.lower()
    import re as _re

    # ── 1. Senzura: PROMPT sababli ──
    if any(k in low for k in ("prompt is blocked", "prohibited content", "prompt was blocked",
                              "text was flagged", "prompt violat")):
        return ("🛡 Prompt xavfsizlik filtridan o'tmadi. His-hayajonli, agressiv yoki shubhali "
                "so'zlarni olib tashlab, neytral tavsif bilan qayta urining. Tangalar qaytarildi.")

    # ── 2. Senzura: RASM/VIDEO reference sababli (qaysi biri — aniqlaymiz) ──
    if any(k in low for k in ("image was flagged", "image is flagged", "input image", "image violat",
                              "unsafe image", "nsfw image", "image content", "reference image",
                              "image did not pass", "image blocked")):
        m = _re.search(r"image[^0-9]{0,12}(\d+)", low)
        which = f"{m.group(1)}-reference rasm" if m else "Yuklangan reference rasmlardan biri"
        return (f"🛡 {which} xavfsizlik filtridan o'tmadi (yuz yaqin plan, taniqli shaxs yoki ochiq "
                f"kiyim bo'lishi mumkin). Boshqa rasm bilan qayta urining. Tangalar qaytarildi.")
    if any(k in low for k in ("video was flagged", "input video", "video content", "video violat")):
        return ("🛡 Yuklangan video xavfsizlik filtridan o'tmadi. Boshqa video bilan urining. "
                "Tangalar qaytarildi.")

    # ── 3. Umumiy moderatsiya (manbasi noaniq) ──
    if any(k in low for k in ("content", "moderation", "policy", "nsfw", "sensitive", "risk", "safety")):
        return ("🛡 So'rov xavfsizlik filtridan o'tmadi — sabab prompt yoki reference bo'lishi mumkin. "
                "Avval promptni neytrallashtiring, o'zgarmasa boshqa rasm bilan urining. Tangalar qaytarildi.")

    # ── 4. Reference yetib bormadi ──
    if "at least one image" in low or "no image provided" in low:
        return "Reference rasm yetib bormadi. Rasmni qayta yuklab, qayta urining. Tangalar qaytarildi."
    if "pixelcount" in low or "pixel count" in low or "image size" in low or "too large" in low and "image" in low:
        return "Rasm o'lchami bu modelga mos kelmadi. Boshqa (kichikroq) rasm bilan urining."

    # ── 5. Video muammolari ──
    if any(k in low for k in ("video too long", "duration exceeds", "max duration")):
        return "Video juda uzun bu model uchun. Qisqaroq video yuklab urining. Tangalar qaytarildi."
    if any(k in low for k in ("unsupported format", "codec", "mime", "quicktime")):
        return "Fayl formati qabul qilinmadi. MP4 formatda qayta yuklang. Tangalar qaytarildi."

    # ── 6. Parametr mos kelmadi ──
    if any(k in low for k in ("invalid_params", "invalidparameter", "invalid parameter", "not supported",
                              "unsupported resolution", "unsupported aspect")):
        return "Tanlangan sozlama (sifat/nisbat/davomiylik) bu modelga mos kelmadi. Boshqa variant tanlab urining."

    # ── 7. Xizmat holati ──
    if any(k in low for k in ("insufficient", "balance", "credit", "quota")):
        return "Xizmatda vaqtinchalik cheklov. Birozdan so'ng qayta urining — tangalar qaytarildi."
    if any(k in low for k in ("504", "502", "503", "timeout", "timed out", "gateway",
                              "text/html", "connection", "unreachable", "overload")):
        return "AI serveri hozir band. 1-2 daqiqadan so'ng qayta urining — tangalar qaytarildi."

    # ── 8. Texnik ko'rinishdagi har qanday qoldiq — umumiy, ichki tafsilotsiz ──
    if len(s) > 120 or any(k in s for k in ("http", "url=", "{", "Traceback", "Exception", "aiohttp", "message=", "invalid_request")):
        return "Generatsiya amalga oshmadi. Qayta urinib ko'ring — tangalar qaytarildi."
    return s


# ── Xato xabarlari tarjimasi (frontend x-lang headerini yuboradi) ──
_REQ_LANG = contextvars.ContextVar("req_lang", default="uz")

ERR_I18N = {
    "Kirish talab qilinadi": {"en": "Sign in required", "ru": "Требуется вход"},
    "Email noto'g'ri": {"en": "Invalid email", "ru": "Неверный email"},
    "Parol kamida 6 belgi bo'lsin": {"en": "Password must be at least 6 characters", "ru": "Пароль — минимум 6 символов"},
    "Ismingizni kiriting": {"en": "Enter your name", "ru": "Введите имя"},
    "Bu email ro'yxatdan o'tgan — kirishga urinib ko'ring": {"en": "This email is already registered — try signing in", "ru": "Этот email уже зарегистрирован — попробуйте войти"},
    "Email yoki parol noto'g'ri": {"en": "Wrong email or password", "ru": "Неверный email или пароль"},
    "Balans yetarli emas": {"en": "Not enough balance", "ru": "Недостаточно монет"},
    "Bu model saytda mavjud emas": {"en": "This model is not available on the site", "ru": "Эта модель недоступна на сайте"},
    "Bu model uchun kamida 1 ta reference rasm kerak": {"en": "This model needs at least 1 reference image", "ru": "Этой модели нужно минимум 1 референс-фото"},
    "Tavsif (prompt) yozing": {"en": "Write a description (prompt)", "ru": "Напишите описание (промпт)"},
    "Sifat qiymati noto'g'ri": {"en": "Invalid quality value", "ru": "Неверное значение качества"},
    "Nisbat qiymati noto'g'ri": {"en": "Invalid aspect ratio", "ru": "Неверное соотношение сторон"},
    "Davomiylik qiymati noto'g'ri": {"en": "Invalid duration", "ru": "Неверная длительность"},
    "Narx aniqlanmadi": {"en": "Could not determine the price", "ru": "Не удалось определить цену"},
    "Reference soni ko'p": {"en": "Too many reference images", "ru": "Слишком много референсов"},
    "Reference topilmadi — qayta yuklang": {"en": "Reference not found — upload again", "ru": "Референс не найден — загрузите заново"},
    "Fayl 10 MB dan katta": {"en": "File is over 10 MB", "ru": "Файл больше 10 МБ"},
    "Faqat JPG, PNG yoki WEBP rasm": {"en": "Only JPG, PNG or WEBP images", "ru": "Только JPG, PNG или WEBP"},
    "Topilmadi": {"en": "Not found", "ru": "Не найдено"},
    "Foydalanuvchi topilmadi": {"en": "User not found", "ru": "Пользователь не найден"},
    "Hamyon topilmadi": {"en": "Wallet not found", "ru": "Кошелёк не найден"},
    "Ruxsat yo'q": {"en": "Access denied", "ru": "Доступ запрещён"},
    "Telegram allaqachon ulangan": {"en": "Telegram is already linked", "ru": "Telegram уже привязан"},
    "Bu hisob allaqachon ulangan": {"en": "This account is already linked", "ru": "Этот аккаунт уже привязан"},
    "Kod topilmadi yoki eskirgan": {"en": "Code not found or expired", "ru": "Код не найден или истёк"},
    "Paket noto'g'ri": {"en": "Invalid package", "ru": "Неверный пакет"},
    "To'lov usuli noto'g'ri": {"en": "Invalid payment method", "ru": "Неверный способ оплаты"},
    "Payme hali sozlanmagan": {"en": "Payme is not configured yet", "ru": "Payme ещё не настроен"},
    "Click hali sozlanmagan": {"en": "Click is not configured yet", "ru": "Click ещё не настроен"},
    "Visa to'lovi tez orada qo'shiladi — hozircha Payme yoki Click'dan foydalaning": {"en": "Visa payments are coming soon — use Payme or Click for now", "ru": "Оплата Visa скоро появится — пока используйте Payme или Click"},
    "Summa noto'g'ri": {"en": "Invalid amount", "ru": "Неверная сумма"},
    "Summa 0 bo'lmasin": {"en": "Amount must not be 0", "ru": "Сумма не должна быть 0"},
}


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def make_token(uid):
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{uid}:{exp}"
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def parse_token(token):
    try:
        b, sig = token.split(".")
        payload = base64.urlsafe_b64decode(b.encode()).decode()
        good = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        uid, exp = payload.split(":")
        if int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


def set_session(resp, uid):
    resp.set_cookie("vsess", make_token(uid), max_age=TOKEN_TTL, httponly=True,
                    samesite="lax", secure=COOKIE_SECURE, path="/")


async def current_user(request: Request):
    uid = parse_token(request.cookies.get("vsess", ""))
    if not uid:
        return None
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def user_json(u):
    return {"name": u["name"], "email": u["email"], "balance": u["balance"],
            "telegram_linked": bool(u["telegram_id"]),
            "is_admin": bool(ADMIN_EMAIL) and u["email"] == ADMIN_EMAIL}


def refund(user_id, amount):
    if amount and amount > 0:
        q("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id), commit=True)


def credit_order(order):
    """To'lov muvaffaqiyatli — tangachani qo'shish (idempotent emas, chaqirishdan oldin status tekshiriladi)."""
    total = order["tangacha"] + order["bonus"]
    q("UPDATE users SET balance = balance + ? WHERE id=?", (total, order["user_id"]), commit=True)
    q("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (int(time.time()), order["id"]), commit=True)


# ────────────────────────── ATLAS CLOUD ADAPTERI ──
# Bot (voro_creator_bot.py) da to'liq, sinovdan o'tgan Atlas kodi BOR.
# Uni qayta yozmaymiz — import qilib, o'sha funksiyalarni ishlatamiz.
# Shunday qilib bot qanday generatsiya qilsa, sayt ham AYNAN shunday qiladi.
import sys as _sys
if "/root/bot" not in _sys.path:
    _sys.path.insert(0, "/root/bot")

_bot = None
_bot_last_try = 0.0
def _get_bot():
    """Botni lazy import qiladi. Muvaffaqiyatsizlikni DOIMIY keshlamaydi —
    har 15s da qayta urinadi (deploy paytidagi vaqtinchalik uzilishlarga chidamli)."""
    global _bot, _bot_last_try
    if _bot:
        return _bot
    now = time.time()
    if now - _bot_last_try < 15:
        return None
    _bot_last_try = now
    try:
        import voro_creator_bot as b
        _bot = b
        return _bot
    except Exception as e:
        print(f"[atlas] bot import xatosi (15s dan keyin qayta urinadi): {e}")
        return None


# Atlas 'tugadi'/'xato' statuslari (bot bilan bir xil bo'lishi uchun botdan olamiz, bo'lmasa default)
def _done_statuses():
    b = _get_bot()
    return set(getattr(b, "ATLAS_DONE_STATUSES", None) or {"succeeded", "completed", "done", "success", "COMPLETED", "SUCCEEDED"})
def _failed_statuses():
    b = _get_bot()
    return set(getattr(b, "ATLAS_FAILED_STATUSES", None) or {"failed", "error", "canceled", "cancelled", "FAILED", "ERROR"})


def _extract_media_url(data: dict):
    """Bot javob formatidan natija URL'ini oladi (bot 20182 mantiqi bilan bir xil)."""
    fd = (data.get("data") or {}) if isinstance(data, dict) else {}
    # 1) outputs / images / urls massivi
    out_arr = fd.get("outputs") or fd.get("images") or fd.get("urls") or []
    if out_arr:
        return out_arr[0] if isinstance(out_arr, list) else str(out_arr)
    # 2) output (str yoki list)
    out_s = fd.get("output")
    if isinstance(out_s, list) and out_s:
        return out_s[0]
    if isinstance(out_s, str) and out_s:
        return out_s
    # 3) to'g'ridan-to'g'ri url maydonlari
    for f in ("url", "image_url", "video_url", "result", "media_url"):
        if fd.get(f):
            return fd[f]
    return None


async def atlas_submit(job) -> str:
    """Botning tayyor atlas_submit'ini chaqiradi, prediction_id qaytaradi."""
    b = _get_bot()
    if not b:
        raise RuntimeError("Bot moduli yuklanmadi (Atlas ulanmagan)")

    mid = job["mid"]
    prompt = job["prompt"] or ""
    res = job["res"] or None
    asp = job["asp"] if (job["asp"] and job["asp"] not in ("adaptive", "auto")) else None
    dur = int(job["dur"]) if (job["dur"] and int(job["dur"]) > 0) else None

    # Reference'lar: botdagidek AVVAL Atlasga yuklanadi (aliyuncs URL — ishonchli,
    # .mov ham qabul qilinadi), keyin turiga ko'ra to'g'ri parametrga ajratiladi.
    ref_ids = json.loads(job["refs_json"] or "[]")
    image_urls, audio_urls, video_clip_urls = [], [], []
    for rid in ref_ids:
        up = q("SELECT * FROM uploads WHERE id=?", (rid,), one=True)
        if not up:
            continue
        p = Path(up["path"])
        try:
            kind = up["kind"] if "kind" in up.keys() and up["kind"] else _kind_for_ext(p.suffix.lower())
        except Exception:
            kind = _kind_for_ext(p.suffix.lower())
        try:
            data = p.read_bytes()
        except Exception:
            raise RuntimeError("Reference fayl o'qilmadi — qayta yuklang")
        up_name = p.name
        if kind == "video" and p.suffix.lower() == ".mov":
            data, up_name = mov_to_mp4_bytes(data, p)
        aurl = await b.atlas_upload_media(data, filename=up_name)
        if not aurl:
            raise RuntimeError("Reference yuklab bo'lmadi — qayta urinib ko'ring")
        print(f"[submit] ref {kind} -> atlas: {aurl[:60]}")
        if kind == "audio":
            audio_urls.append(aurl)
        elif kind == "video":
            video_clip_urls.append(aurl)
        else:
            image_urls.append(aurl)

    kwargs = dict(prompt=prompt, aspect_ratio=asp, resolution=res, duration=dur)
    if image_urls:
        kwargs["image_urls"] = image_urls
    if audio_urls:
        kwargs["audio_urls"] = audio_urls
    if video_clip_urls:
        kwargs["video_clip_urls"] = video_clip_urls

    # Gemini Omni Flash: Atlas "images" maydonini kutadi — to'g'ridan submit
    if mid.startswith("google/gemini-omni-flash/"):
        is_vedit = mid.endswith("/video-edit")
        if audio_urls:
            raise RuntimeError("Bu model audio qabul qilmaydi")
        if video_clip_urls and not is_vedit:
            raise RuntimeError("Bu model video qabul qilmaydi")
        payload = {"model": mid, "prompt": prompt, "resolution": "720p"}
        if is_vedit:
            if not video_clip_urls:
                raise RuntimeError("Tahrirlash uchun video biriktiring")
            payload["video"] = video_clip_urls[0]
            # Hujjat: video-edit'da aspect_ratio parametri YO'Q — natija manba video nisbatiga ergashadi
        else:
            if asp:
                payload["aspect_ratio"] = asp
            if dur:
                payload["duration"] = int(dur)
        if image_urls:
            # Atlas i2v birlik "image" (string) kutadi; reference-to-video esa "images" (massiv)
            if mid.endswith("/image-to-video"):
                payload["image"] = image_urls[0]
            else:
                payload["images"] = image_urls
        print("[gemini] payload:", {k: ([u[:48] for u in v] if isinstance(v, list) else str(v)[:60]) for k, v in payload.items()})
        headers = {
            "Authorization": f"Bearer {b.ATLASCLOUD_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=90) as cl:
                    r = await cl.post(f"{b.ATLASCLOUD_BASE}/api/v1/model/generateVideo",
                                      json=payload, headers=headers)
                jd = r.json()
                pid = (jd.get("data") or {}).get("id")
                if pid:
                    return str(pid)
                last_err = RuntimeError(str(jd.get("msg") or jd.get("message") or jd)[:200])
                if r.status_code < 500:
                    raise last_err
            except Exception as e:
                last_err = e
                low = str(e).lower()
                if not any(k in low for k in ("504", "502", "503", "timeout", "gateway", "text/html", "expecting value")):
                    raise
            await asyncio.sleep(6 + attempt * 7)
        raise last_err

    # Admin qo'shgan (custom) modellar — payload_map bo'yicha to'g'ridan submit
    cmeta = get_model(mid) or {}
    if cmeta.get("custom") or cmeta.get("payload_map"):
        pm = cmeta.get("payload_map") or {}
        payload = {"model": mid, "prompt": prompt}
        payload.update(pm.get("extra") or {})
        if pm.get("send_aspect", True) and asp:
            payload[pm.get("aspect_key", "aspect_ratio")] = asp
        if pm.get("send_duration", True) and dur:
            payload[pm.get("duration_key", "duration")] = int(dur)
        if pm.get("send_resolution") and res:
            payload[pm.get("resolution_key", "resolution")] = str(res)
        imf = pm.get("image_field", "images")
        if image_urls:
            payload[imf] = image_urls if not pm.get("image_single") else image_urls[0]
        vf = pm.get("video_field")
        if video_clip_urls:
            if not vf:
                raise RuntimeError("Bu model video qabul qilmaydi")
            payload[vf] = video_clip_urls[0] if not pm.get("video_list") else video_clip_urls
        af = pm.get("audio_field")
        if audio_urls:
            if not af:
                raise RuntimeError("Bu model audio qabul qilmaydi")
            payload[af] = audio_urls[0] if not pm.get("audio_list") else audio_urls
        endpoint = pm.get("endpoint") or ("generateImage" if cmeta.get("type") == "image" else "generateVideo")
        headers = {
            "Authorization": f"Bearer {b.ATLASCLOUD_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=90) as cl:
                    r = await cl.post(f"{b.ATLASCLOUD_BASE}/api/v1/model/{endpoint}",
                                      json=payload, headers=headers)
                jd = r.json()
                pid = (jd.get("data") or {}).get("id")
                if pid:
                    return str(pid)
                last_err = RuntimeError(str(jd.get("msg") or jd.get("message") or jd)[:200])
                if r.status_code < 500:
                    raise last_err
            except Exception as e:
                last_err = e
                low = str(e).lower()
                if not any(k in low for k in ("504", "502", "503", "timeout", "gateway", "text/html", "expecting value")):
                    raise
            await asyncio.sleep(6 + attempt * 7)
        raise last_err

    resp = await b.atlas_submit(mid, **kwargs)
    # Bot submit javobi: {"id": "...", "data": {...}} yoki to'g'ridan {"id": ...}
    if isinstance(resp, dict):
        pid = resp.get("id") or (resp.get("data") or {}).get("id")
        if not pid:
            raise RuntimeError(f"Atlas javobida id yo'q: {str(resp)[:200]}")
        return str(pid)
    return str(resp)


async def atlas_poll(atlas_id) -> dict:
    """Botning prediction endpointini bir marta tekshiradi (bitta so'rov)."""
    b = _get_bot()
    if not b:
        return {"status": "failed", "error": "Bot moduli yuklanmadi"}

    url = f"{b.ATLASCLOUD_BASE}/api/v1/model/prediction/{atlas_id}"
    headers = {"Authorization": f"Bearer {b.ATLASCLOUD_API_KEY}"}
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.get(url, headers=headers)
        data = r.json()

    st = str((data.get("data") or {}).get("status", ""))
    if st in _done_statuses():
        url_out = _extract_media_url(data)
        return {"status": "done", "output_url": url_out}
    if st in _failed_statuses():
        fd = data.get("data") or {}
        err = fd.get("error") or fd.get("message") or fd.get("failure_reason") or "Atlas xatosi"
        return {"status": "failed", "error": str(err)[:200]}
    return {"status": "processing"}


EXT_BY_CT = {"video/mp4": ".mp4", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/webm": ".webm"}


async def download_result(job_id, url, jtype) -> str:
    """Natijani serverga yuklab oladi (Atlas URL muddati o'tib ketmasligi uchun)."""
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cl:
        async with cl.stream("GET", url) as r:
            r.raise_for_status()
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            ext = EXT_BY_CT.get(ct) or (".mp4" if jtype == "video" else ".png")
            fname = f"{job_id}{ext}"
            fpath = MEDIA_DIR / fname
            with open(fpath, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
    return f"/media/{fname}"


def set_job(job_id, **kw):
    kw["updated"] = int(time.time())
    sets = ", ".join(f"{k}=?" for k in kw)
    q(f"UPDATE jobs SET {sets} WHERE id=?", (*kw.values(), job_id), commit=True)


async def run_job(job_id):
    job = q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    if not job:
        return
    try:
        set_job(job_id, status="processing", progress=8)
        atlas_id = await atlas_submit(job)
        set_job(job_id, atlas_id=atlas_id, progress=15)
        deadline = time.time() + 900  # maks 15 daqiqa
        prog = 15
        while time.time() < deadline:
            await asyncio.sleep(4)
            st = await atlas_poll(atlas_id)
            if st["status"] == "done":
                if not st.get("output_url"):
                    raise RuntimeError("Natija URL topilmadi")
                set_job(job_id, progress=95)
                local = await download_result(job_id, st["output_url"], (get_model(job["mid"]) or {}).get("type", "video"))
                set_job(job_id, status="done", progress=100, result_url=local)
                return
            if st["status"] == "failed":
                raise RuntimeError(st.get("error") or "Generatsiya amalga oshmadi")
            prog = min(90, prog + 3)
            set_job(job_id, progress=prog)
        raise TimeoutError("Vaqt tugadi (15 daqiqa)")
    except Exception as e:
        refund(job["user_id"], job["price"])
        set_job(job_id, status="failed", error=str(e)[:200])


# ────────────────────────── APP ──
app = FastAPI(title="voro.uz API", docs_url=None, redoc_url=None)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://qahramen.github.io",
        "https://voro.uz", "https://www.voro.uz",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Tg-Init-Data", "X-Tg-Uid", "X-Tg-Sig", "X-Tg-Name", "Content-Type", "X-Lang"],
)


@app.middleware("http")
async def lang_middleware(request: Request, call_next):
    lang = (request.headers.get("x-lang") or "uz")[:2].lower()
    _REQ_LANG.set(lang if lang in ("uz", "en", "ru") else "uz")
    return await call_next(request)


@app.on_event("startup")
async def startup():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    # Crash-recovery: server o'chib qolganda osilib qolgan ishlar -> failed + refund
    stuck = q("SELECT id, user_id, price FROM jobs WHERE status IN ('queued','processing')")
    for j in stuck:
        refund(j["user_id"], j["price"])
        set_job(j["id"], status="failed", error="Server qayta ishga tushdi — tangacha qaytarildi")


app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR), check_dir=False), name="uploads")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR), check_dir=False), name="media")


# ────────────────────────── AUTH ──
@app.post("/auth/register")
async def register(request: Request):
    b = await request.json()
    email = str(b.get("email", "")).strip().lower()
    password = str(b.get("password", ""))
    name = str(b.get("name", "")).strip()[:60]
    if "@" not in email or "." not in email or len(email) > 120:
        return err("Email noto'g'ri")
    if len(password) < 6:
        return err("Parol kamida 6 belgi bo'lsin")
    if not name:
        return err("Ismingizni kiriting")
    if q("SELECT id FROM users WHERE email=?", (email,), one=True):
        return err("Bu email ro'yxatdan o'tgan — kirishga urinib ko'ring")
    salt = os.urandom(16).hex()
    q("INSERT INTO users(email,name,pass_hash,salt,balance,created) VALUES(?,?,?,?,?,?)",
      (email, name, hash_pw(password, salt), salt, SIGNUP_BONUS, int(time.time())), commit=True)
    u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    resp = JSONResponse(user_json(u))
    set_session(resp, u["id"])
    return resp


@app.post("/auth/login")
async def login(request: Request):
    b = await request.json()
    email = str(b.get("email", "")).strip().lower()
    password = str(b.get("password", ""))
    u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if not u or not hmac.compare_digest(u["pass_hash"], hash_pw(password, u["salt"])):
        return err("Email yoki parol noto'g'ri", 401)
    resp = JSONResponse(user_json(u))
    set_session(resp, u["id"])
    return resp


@app.post("/auth/google")
async def auth_google(request: Request):
    """Google Sign-In: frontend ID-token yuboradi, backend tekshiradi va sessiya beradi."""
    if not GOOGLE_CLIENT_ID:
        return err("Google auth sozlanmagan", 503)
    b = await request.json()
    cred = str(b.get("credential", ""))
    if not cred:
        return err("Google token yo'q")
    try:
        from google.oauth2 import id_token as _gid
        from google.auth.transport import requests as _greq
        info = _gid.verify_oauth2_token(cred, _greq.Request(), GOOGLE_CLIENT_ID)
    except Exception as e:
        print("[google] token tekshirish xatosi:", e)
        return err("Google token yaroqsiz", 401)
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return err("Google token yaroqsiz", 401)
    if not info.get("email_verified"):
        return err("Google email tasdiqlanmagan", 401)
    sub = str(info["sub"])
    email = str(info.get("email", "")).strip().lower()
    name = (info.get("name") or (email.split("@")[0] if email else "Foydalanuvchi"))[:60]
    avatar = info.get("picture")
    # 1) google_sub bo'yicha; 2) email bo'yicha (mavjud hisobga bog'lash); 3) yangi hisob
    u = q("SELECT * FROM users WHERE google_sub=?", (sub,), one=True)
    if not u and email:
        u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if u:
        q("UPDATE users SET google_sub=?, avatar=COALESCE(avatar,?) WHERE id=?",
          (sub, avatar, u["id"]), commit=True)
        uid = u["id"]
    else:
        salt = os.urandom(16).hex()
        q("""INSERT INTO users(email,name,pass_hash,salt,balance,created,google_sub,avatar)
             VALUES(?,?,?,?,?,?,?,?)""",
          (email or f"g{sub}@voro.local", name, "", salt, SIGNUP_BONUS, int(time.time()), sub, avatar),
          commit=True)
        uid = q("SELECT id FROM users WHERE google_sub=?", (sub,), one=True)["id"]
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    resp = JSONResponse(user_json(u))
    set_session(resp, uid)
    return resp


@app.post("/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("vsess", path="/")
    return resp


@app.get("/me")
async def me(u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    return user_json(u)


# ────────────────────────── UPLOAD ──
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".aac"}
ALLOWED_VIDEO = {".mp4", ".mov", ".webm"}


ATLAS_MIN_PX = 450_000      # Atlas talabi: 409,600 dan kam bo'lmasin (zaxira bilan)
ATLAS_MAX_PX = 8_200_000    # 8,847,360 dan oshmasin (zaxira bilan)


def mov_to_mp4_bytes(data, src_path):
    """.mov ni mp4 konteynerga qayta o'raydi (ffmpeg -c copy). ffmpeg yo'q bo'lsa
    fayl shunchaki .mp4 nomi bilan qaytadi (mov/mp4 bir oila — odatda yetarli)."""
    mp4_name = src_path.stem + ".mp4"
    try:
        import subprocess, tempfile, os
        with tempfile.TemporaryDirectory() as td:
            fin = os.path.join(td, "in.mov")
            fout = os.path.join(td, "out.mp4")
            with open(fin, "wb") as f:
                f.write(data)
            r = subprocess.run(["ffmpeg", "-y", "-i", fin, "-c", "copy", "-movflags", "+faststart", fout],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.getsize(fout) > 1000:
                with open(fout, "rb") as f:
                    print(f"[remux] {src_path.name} -> mp4 (ffmpeg)")
                    return f.read(), mp4_name
    except Exception as e:
        print(f"[remux] ffmpeg yo'q/xato ({e}) — nom bilan yuboriladi")
    return data, mp4_name


def _mp4_duration(path):
    """MP4/MOV (QuickTime atom) davomiyligi — mvhd atomidan, kutubxonasiz."""
    import struct
    with open(path, "rb") as f:
        head = f.read(32 * 1024 * 1024)
    i = head.find(b"mvhd")
    if i == -1:
        return 0.0
    ver = head[i + 4]
    if ver == 1:
        ts = struct.unpack(">I", head[i + 24:i + 28])[0]
        du = struct.unpack(">Q", head[i + 28:i + 36])[0]
    else:
        ts = struct.unpack(">I", head[i + 16:i + 20])[0]
        du = struct.unpack(">I", head[i + 20:i + 24])[0]
    return (du / ts) if ts else 0.0


def measure_media_duration(path):
    """Video/audio davomiyligi (soniya): ffprobe -> MP4 atom -> mutagen."""
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        d = float((out.stdout or "").strip() or 0)
        if d > 0:
            return d
    except Exception:
        pass
    try:
        d = _mp4_duration(path)
        if d > 0:
            return d
    except Exception:
        pass
    try:
        mf = _MutagenFile(str(path))
        if mf is not None and mf.info is not None:
            return float(getattr(mf.info, "length", 0) or 0)
    except Exception:
        pass
    return 0.0


def normalize_ref_image(data: bytes, ext: str) -> bytes:
    """Atlas piksel talabiga moslash: kichik rasmni sifatli kattalashtirish,
    juda kattasini kichraytirish. Muvaffaqiyatsiz bo'lsa asl bytes qaytadi."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
        px = w * h
        if ATLAS_MIN_PX <= px <= ATLAS_MAX_PX:
            return data
        scale = (ATLAS_MIN_PX / px) ** 0.5 if px < ATLAS_MIN_PX else (ATLAS_MAX_PX / px) ** 0.5
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        img = img.convert("RGB") if ext in (".jpg", ".jpeg") else img
        img = img.resize((nw, nh), Image.LANCZOS)
        buf = io.BytesIO()
        if ext in (".jpg", ".jpeg"):
            img.save(buf, "JPEG", quality=92)
        elif ext == ".webp":
            img.save(buf, "WEBP", quality=92)
        else:
            img.save(buf, "PNG")
        print(f"[upload] rasm moslandi: {w}x{h} ({px}px) -> {nw}x{nh}")
        return buf.getvalue()
    except Exception as e:
        print(f"[upload] normalize xato: {e}")
        return data


@app.post("/upload")
async def upload(file: UploadFile = File(...), u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (ALLOWED_EXT | ALLOWED_AUDIO | ALLOWED_VIDEO):
        return err("Faqat rasm (JPG/PNG/WEBP), audio (MP3/M4A/WAV) yoki video (MP4/MOV/WEBM)")
    kind = _kind_for_ext(ext)
    limit = 60 if kind == "video" else (25 if kind == "audio" else 10)
    data = await file.read()
    if len(data) > limit * 1024 * 1024:
        return err(f"Fayl {limit} MB dan katta")
    if kind == "image":
        data = normalize_ref_image(data, ext)   # Atlas piksel talabi
    uid = uuid.uuid4().hex
    fpath = UPLOAD_DIR / f"{uid}{ext}"
    with open(fpath, "wb") as f:
        f.write(data)
    dur_s = 0.0
    if kind in ("audio", "video"):
        dur_s = measure_media_duration(fpath)
        print(f"[upload] {kind} duration={dur_s:.2f}s ({fpath.name})")
    q("INSERT INTO uploads(id,user_id,path,created,kind,duration) VALUES(?,?,?,?,?,?)",
      (uid, u["id"], str(fpath), int(time.time()), kind, dur_s), commit=True)
    return {"upload_id": uid, "url": f"/uploads/{uid}{ext}", "kind": kind, "duration": dur_s}


@app.post("/reuse")
async def reuse(request: Request, u=Depends(current_user)):
    """Galereya natijasini reference sifatida qayta ishlatish:
    o'z media faylini uploads ro'yxatiga ko'chiradi va upload_id qaytaradi."""
    if not u:
        return err("Kirish talab qilinadi", 401)
    b = await request.json()
    job_id = str(b.get("job_id") or "")
    j = q("SELECT * FROM jobs WHERE id=? AND user_id=? AND status='done'", (job_id, u["id"]), one=True)
    if not j or not j["result_url"]:
        return err("Natija topilmadi", 404)
    if not str(j["result_url"]).startswith("/media/"):
        return err("Bu natijani reference qilib bo'lmaydi")
    src = MEDIA_DIR / Path(j["result_url"]).name
    if not src.exists():
        return err("Fayl topilmadi", 404)
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXT:
        return err("Faqat rasm natijalarni reference qilib ishlatish mumkin")
    uid = uuid.uuid4().hex
    dst = UPLOAD_DIR / f"{uid}{ext}"
    import shutil as _sh
    _sh.copyfile(src, dst)
    q("INSERT INTO uploads(id,user_id,path,created) VALUES(?,?,?,?)",
      (uid, u["id"], str(dst), int(time.time())), commit=True)
    return {"upload_id": uid, "url": f"/uploads/{uid}{ext}"}


# ────────────────────────── GENERATE ──
@app.post("/generate")
async def generate(request: Request, u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    b = await request.json()
    mid = b.get("mid")
    meta = get_model(mid)
    if not meta or not meta["web"]:
        return err("Bu model saytda mavjud emas")
    if mid in (_get_setting("disabled_models", []) or []):
        return err("Bu model vaqtincha o'chirilgan")
    res = b.get("res")
    asp = b.get("asp")
    dur = b.get("dur")
    prompt = str(b.get("prompt") or "").strip()[:3500]
    refs = b.get("refs") or []

    if meta["res"] and str(res) not in [str(x) for x in meta["res"]]:
        return err("Sifat qiymati noto'g'ri")
    if meta["asp"] and asp is not None and str(asp) not in meta["asp"]:
        return err("Nisbat qiymati noto'g'ri")
    if meta["type"] == "video":
        if not meta["dur"]:
            # Formula-narxli modellar (video-edit kabi) davomiylikni o'zi hisoblaydi
            if not meta.get("price_formula"):
                return err("Bu model saytda mavjud emas")
        elif int(dur or 0) not in meta["dur"]:
            return err("Davomiylik qiymati noto'g'ri")
    else:
        dur = None
    if not isinstance(refs, list) or len(refs) > meta["refs"]:
        return err("Reference soni ko'p")
    if meta["refs_req"] and len(refs) == 0:
        return err("Bu model uchun kamida 1 ta reference rasm kerak")
    for rid in refs:
        row = q("SELECT id FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
        if not row:
            return err("Reference topilmadi — qayta yuklang")
    if not prompt and len(refs) == 0:
        return err("Tavsif (prompt) yozing")

    if meta.get("price_formula") == "video_sec":
        # Narx manba video uzunligidan (video-edit va per-sec custom modellar)
        vdur = 0.0
        for rid in refs:
            row = q("SELECT * FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
            if not row:
                continue
            rk = row["kind"] if "kind" in row.keys() and row["kind"] else _kind_for_ext(Path(row["path"]).suffix.lower())
            if rk != "video":
                continue
            vdur = float(row["duration"] or 0) if "duration" in row.keys() else 0.0
            if vdur <= 0:
                vdur = measure_media_duration(row["path"])
                if vdur > 0:
                    q("UPDATE uploads SET duration=? WHERE id=?", (vdur, rid), commit=True)
            break
        if vdur <= 0:
            return err("Video uzunligi aniqlanmadi — videoni qayta yuklang")
        if vdur > 30.5:
            return err("Video 30 soniyadan oshmasin")
        dur = vdur

    price = price_for(mid, res, dur)
    if price is None:
        return err("Narx aniqlanmadi")

    # Atomik yechish — balans yetmasa rad etiladi (poyga holatlaridan himoya)
    cur = q("UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?",
            (price, u["id"], price), commit=True)
    if cur.rowcount != 1:
        return err("Balans yetarli emas", 402)

    job_id = uuid.uuid4().hex
    now = int(time.time())
    q("""INSERT INTO jobs(id,user_id,mid,res,asp,dur,prompt,refs_json,price,status,progress,created,updated)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (job_id, u["id"], mid, str(res) if res else None, str(asp) if asp else None,
       int(dur) if dur else None, prompt, json.dumps(refs), price, "queued", 0, now, now), commit=True)
    _task = asyncio.create_task(run_job(job_id))   # M4: havolani saqlaymiz — GC o'chirmasin
    _bg_tasks.add(_task)
    _task.add_done_callback(_bg_tasks.discard)
    nb = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return {"job_id": job_id, "price": price, "balance": nb}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str, u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    j = q("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, u["id"]), one=True)
    if not j:
        return err("Topilmadi", 404)
    out = {"status": j["status"], "progress": j["progress"], "result_url": j["result_url"], "error": friendly_error(j["error"]) if j["status"] == "failed" else j["error"]}
    if j["status"] == "failed":
        out["balance"] = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return out


@app.get("/history")
async def history(limit: int = 30, u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    limit = max(1, min(int(limit), 60))
    rows = q("SELECT * FROM jobs WHERE user_id=? AND status='done' ORDER BY created DESC LIMIT ?",
             (u["id"], limit))
    out = []
    for j in rows:
        m = get_model(j["mid"]) or {}
        out.append({"id": j["id"], "mid": j["mid"], "type": m.get("type", "image"), "name": m.get("name", j["mid"]),
                    "emoji": m.get("emoji", "✨"), "price": j["price"], "url": j["result_url"],
                    "prompt": j["prompt"] or "", "created": j["created"],
                    "res": j["res"], "asp": j["asp"], "dur": j["dur"]})
    return out


def _ref_to_jpeg_b64(path, kind):
    """Reference faylni Claude vision uchun tayyorlaydi: rasm -> kichraytirilgan JPEG b64;
    video -> birinchi kadr (ffmpeg). None = tayyorlab bo'lmadi."""
    import base64, io, subprocess, tempfile, os
    try:
        img_bytes = None
        if kind == "video":
            with tempfile.TemporaryDirectory() as td:
                fout = os.path.join(td, "frame.jpg")
                r = subprocess.run(["ffmpeg", "-y", "-ss", "0.3", "-i", str(path),
                                    "-frames:v", "1", "-q:v", "4", fout],
                                   capture_output=True, timeout=30)
                if r.returncode == 0 and os.path.exists(fout):
                    with open(fout, "rb") as f:
                        img_bytes = f.read()
        else:
            with open(path, "rb") as f:
                img_bytes = f.read()
        if not img_bytes:
            return None
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        im = im.convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[enhance-vision] ref tayyorlanmadi: {e}")
        return None


async def _vision_enhance(idea, media_items, model_name, cat, asp, dur, old_prompt, mid=""):
    """Reference'larni KO'RIB, chuqur tahlil qilib, model tilida prompt yozadi (Claude vision).
    media_items: [(b64_jpeg, kind)] — kind: image | video (birinchi kadr)."""
    uses_tags = _model_uses_tags(mid)
    tag_policy = (
        "TAG POLICY: this model uses reference tags. Bind media with @image1/@image2/@video1/@audio1 exactly "
        "in upload order; do NOT describe a tagged subject's face/appearance in words — the tag carries identity. "
        "Describe only action, scene, camera, mood."
    ) if uses_tags else (
        "TAG POLICY: this model does NOT understand @tags — never output them. Instead, faithfully DESCRIBE each "
        "reference in words (verbal identity lock): for people — gender, age range, hair style/color, facial hair, "
        "skin tone, exact clothing with colors, accessories; for products/objects — shape, colors, visible text or "
        "branding; for places — architecture, palette, lighting. Refer to them as 'the person from the reference "
        "image' etc., so the model reproduces them faithfully."
    )
    sysmsg = (
        f"You are an elite AI {cat} prompt engineer for the '{model_name}' model.\n"
        f"STEP 1 (silent): analyze EVERY attached reference carefully — subjects, appearance details, environment, "
        f"lighting, colors, style, and (for video frames) implied motion.\n"
        f"STEP 2: write ONE polished English generation prompt that fulfills the user's idea using those references.\n"
        f"LANGUAGE INTELLIGENCE: the user may write in Uzbek (Latin or Cyrillic, ANY dialect - Tashkent, Fergana, Khorezm, Surkhandarya colloquial), Russian, English, or a MIX, often with typos, slang, missing apostrophes (masalan: korish=ko'rish, urish=urish/urmoq context), phonetic spellings and voice-typing artifacts. Confidently infer the TRUE intent - never take a typo literally if context shows otherwise. STAY INSIDE the user's request: include EVERY subject, object and action they asked for, and ADD NOTHING they did not ask for (no new characters, brands, or plot). Your creative freedom is limited to craft: lighting, camera, composition, mood, quality. \n"
        f"HOW THIS MODEL UNDERSTANDS PROMPTS (follow strictly): {_prompt_profile(mid)}\n"
        f"{tag_policy}\n"
        + (f"Aspect ratio {asp}. " if asp else "")
        + (f"Duration {dur}s — pace the action to fit. " if dur else "")
        + "The user's core intent must stay intact: every subject/action they mention MUST appear. "
        "Output ONLY the final prompt text — no explanations, no markdown, max 950 characters."
    )
    content = []
    for i, (b64, rk) in enumerate(media_items, 1):
        label = f"Reference {i}: " + ("first frame of the user's uploaded VIDEO" if rk == "video" else "uploaded IMAGE")
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    utext = ("Improve this existing prompt based on the reference media and the user's wish.\n"
             f"Existing prompt: {old_prompt}\nUser's wish: {idea}") if old_prompt else             f"User's idea: {idea}\nWrite the generation prompt based on the reference media."
    content.append({"type": "text", "text": utext})
    payload = {"model": ENHANCE_MODEL, "max_tokens": 700, "system": sysmsg,
               "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
    jd = r.json()
    parts = [c.get("text", "") for c in (jd.get("content") or []) if c.get("type") == "text"]
    out = " ".join(parts).strip()
    if not out:
        raise RuntimeError(str(jd.get("error") or jd)[:180])
    return out[:1200]


@app.post("/enhance")
async def enhance(request: Request, u=Depends(current_user)):
    """Foydalanuvchi g'oyasini professional promptga aylantiradi.
    Botning tayyor claude_enhance_prompt funksiyasini ishlatadi (Claude API + DeepSeek fallback)."""
    if not u:
        return err("Kirish talab qilinadi", 401)
    b = await request.json()
    idea = str(b.get("prompt") or b.get("idea") or "").strip()[:2000]
    if not idea:
        return err("Avval g'oya yozing")
    mid = b.get("mid")
    meta = get_model(mid) or {}
    model_name = meta.get("name", "AI")
    cat = "video" if meta.get("type") == "video" else "image"
    asp = b.get("asp") or ((meta.get("asp") or ["16:9"])[0])
    res = b.get("res") or None
    dur = b.get("dur") or None
    old_prompt = b.get("old_prompt") or None   # retry: mavjud promptni yaxshilash

    # Reference biriktirilgan bo'lsa — Claude rasm/kadrlarni KO'RIB prompt yozadi
    ref_ids = b.get("refs") or []
    if meta.get("refs_req") and (meta.get("refs") or 0) > 0 and not ref_ids:
        return err("Avval reference yuklang — prompt reference asosida yoziladi")
    if ref_ids and ANTHROPIC_API_KEY:
        media = []
        for rid in ref_ids[:3]:
            row = q("SELECT * FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
            if not row:
                continue
            p = Path(row["path"])
            rk = row["kind"] if "kind" in row.keys() and row["kind"] else _kind_for_ext(p.suffix.lower())
            if rk == "audio":
                continue
            b64 = _ref_to_jpeg_b64(p, rk)
            if b64:
                media.append((b64, rk))
        if media:
            try:
                result = await _vision_enhance(idea, media, model_name, cat,
                                               str(asp) if asp else None,
                                               int(dur) if dur else None,
                                               str(old_prompt) if old_prompt else None, mid=mid or "")
                return {"prompt": result}
            except Exception as e:
                print(f"[enhance-vision] xato, oddiy yo'lga o'tildi: {e}")

    bot = _get_bot()
    if not bot:
        return err("Yaxshilash hozircha ishlamayapti")
    try:
        result = await bot.claude_enhance_prompt(
            idea, model_name, cat,
            asp=str(asp) if asp else "16:9",
            res=str(res) if res else None,
            dur=int(dur) if dur else None,
            old_prompt=str(old_prompt) if old_prompt else None,
            model_id=mid or "",
        )
        result = (result or "").strip()
        if not result:
            return err("Yaxshilash natija bermadi — qayta urinib ko'ring")
        return {"prompt": result}
    except Exception as e:
        print(f"[enhance] xato: {e}")
        return err("Yaxshilashda xato — qayta urinib ko'ring")


# ────────────────────────── HISOBLARNI ULASH (bot <-> sayt, yagona hamyon) ──
@app.post("/link/start")
async def link_start(u=Depends(current_user)):
    """Saytdagi foydalanuvchi bot bilan ulash kodini oladi."""
    if not u:
        return err("Kirish talab qilinadi", 401)
    if u["telegram_id"]:
        return err("Telegram allaqachon ulangan")
    code = uuid.uuid4().hex[:8].upper()
    q("DELETE FROM link_codes WHERE user_id=?", (u["id"],), commit=True)
    q("INSERT INTO link_codes(code,user_id,expires) VALUES(?,?,?)",
      (code, u["id"], int(time.time()) + 900), commit=True)
    return {"code": code, "link": f"https://t.me/{BOT_USERNAME}?start=link_{code}"}


def _internal_ok(request: Request) -> bool:
    return bool(INTERNAL_KEY) and request.headers.get("x-internal-key", "") == INTERNAL_KEY


@app.post("/internal/link")
async def internal_link(request: Request):
    """BOT chaqiradi: /start link_KOD kelganda. Balanslar birlashadi."""
    if not _internal_ok(request):
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    code = str(b.get("code", "")).strip().upper()
    tg_id = int(b.get("telegram_id", 0))
    tg_balance = int(b.get("balance", 0))  # botning lokal balansи shu yerga ko'chadi
    row = q("SELECT * FROM link_codes WHERE code=?", (code,), one=True)
    if not row or row["expires"] < time.time():
        return err("Kod topilmadi yoki eskirgan", 404)
    target = q("SELECT * FROM users WHERE id=?", (row["user_id"],), one=True)
    if not target:
        return err("Foydalanuvchi topilmadi", 404)
    if target["telegram_id"]:
        return err("Bu hisob allaqachon ulangan")
    add = tg_balance
    existing = q("SELECT * FROM users WHERE telegram_id=?", (tg_id,), one=True)
    if existing:  # bot avtomatik yaratgan tg-hamyon bo'lsa — birlashtiramiz
        add += existing["balance"]
        for tbl in ("jobs", "uploads", "orders"):
            q(f"UPDATE {tbl} SET user_id=? WHERE user_id=?", (target["id"], existing["id"]), commit=True)
        q("DELETE FROM users WHERE id=?", (existing["id"],), commit=True)
    q("UPDATE users SET telegram_id=?, balance=balance+? WHERE id=?", (tg_id, add, target["id"]), commit=True)
    q("DELETE FROM link_codes WHERE code=?", (code,), commit=True)
    nb = q("SELECT balance FROM users WHERE id=?", (target["id"],), one=True)["balance"]
    return {"ok": True, "balance": nb, "web_name": target["name"]}


@app.post("/internal/ensure")
async def internal_ensure(request: Request):
    """BOT chaqiradi: tg foydalanuvchi uchun hamyon borligiga ishonch (bo'lmasa yaratadi)."""
    if not _internal_ok(request):
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    tg_id = int(b.get("telegram_id", 0))
    name = str(b.get("name", "Foydalanuvchi"))[:60]
    u = q("SELECT * FROM users WHERE telegram_id=?", (tg_id,), one=True)
    if not u:
        salt = os.urandom(16).hex()
        q("INSERT INTO users(email,name,pass_hash,salt,balance,created,telegram_id) VALUES(?,?,?,?,?,?,?)",
          (f"tg{tg_id}@voro.local", name, "", salt, 0, int(time.time()), tg_id), commit=True)
        u = q("SELECT * FROM users WHERE telegram_id=?", (tg_id,), one=True)
    return {"balance": u["balance"], "user_id": u["id"]}


@app.get("/internal/wallet/{telegram_id}")
async def internal_wallet(telegram_id: int, request: Request):
    if not _internal_ok(request):
        return err("Ruxsat yo'q", 403)
    u = q("SELECT * FROM users WHERE telegram_id=?", (telegram_id,), one=True)
    if not u:
        return err("Hamyon topilmadi", 404)
    return {"balance": u["balance"], "user_id": u["id"]}


@app.post("/internal/deduct")
async def internal_deduct(request: Request):
    """BOT chaqiradi: generatsiyadan oldin tangacha yechish (atomik)."""
    if not _internal_ok(request):
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    tg_id = int(b.get("telegram_id", 0))
    amount = int(b.get("amount", 0))
    if amount <= 0:
        return err("Summa noto'g'ri")
    u = q("SELECT id FROM users WHERE telegram_id=?", (tg_id,), one=True)
    if not u:
        return err("Hamyon topilmadi", 404)
    cur = q("UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?",
            (amount, u["id"], amount), commit=True)
    if cur.rowcount != 1:
        return err("Balans yetarli emas", 402)
    nb = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return {"balance": nb}


@app.post("/internal/credit")
async def internal_credit(request: Request):
    """BOT chaqiradi: Stars to'lovi yoki refund'da tangacha qo'shish."""
    if not _internal_ok(request):
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    tg_id = int(b.get("telegram_id", 0))
    amount = int(b.get("amount", 0))
    if amount <= 0:
        return err("Summa noto'g'ri")
    u = q("SELECT id FROM users WHERE telegram_id=?", (tg_id,), one=True)
    if not u:
        return err("Hamyon topilmadi", 404)
    q("UPDATE users SET balance = balance + ? WHERE id=?", (amount, u["id"]), commit=True)
    nb = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return {"balance": nb}


# ────────────────────────── ADMIN PANEL API ──
def _adm(u):
    return u is not None and bool(ADMIN_EMAIL) and u["email"] == ADMIN_EMAIL


HERO_SLOTS = {"new", "hot", "super", "trend", "top", "star"}
SLOT_MEDIA_EXT = {".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".mov"}
SLOT_MEDIA_MAX = 100 * 1024 * 1024   # 40 MB


def _slot_map():
    rows = q("SELECT slot, url, mid FROM slot_media")
    return {r["slot"]: {"url": r["url"], "mid": r["mid"]} for r in rows}


def _get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


def _set_setting(key, value):
    q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value)), commit=True)


_ATLAS_CATALOG = {"data": None, "ts": 0}
CREDIT_USD = 0.015  # 1 tanga ≈ $0.015 Atlas tannarxi (mavjud narxlar shu kursda)


async def atlas_model_price(mid):
    """Atlas katalogidan modelning base_price'ini oladi (1 soatlik kesh)."""
    try:
        if not _ATLAS_CATALOG["data"] or time.time() - _ATLAS_CATALOG["ts"] > 3600:
            b = _get_bot()
            headers = {"Authorization": f"Bearer {b.ATLASCLOUD_API_KEY}",
                       "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            async with httpx.AsyncClient(timeout=40) as cl:
                r = await cl.get(f"{b.ATLASCLOUD_BASE}/api/v1/models", headers=headers)
            _ATLAS_CATALOG["data"] = {it["model"]: it for it in (r.json().get("data") or [])}
            _ATLAS_CATALOG["ts"] = time.time()
        it = (_ATLAS_CATALOG["data"] or {}).get(mid)
        if it:
            return float(((it.get("price") or {}).get("actual") or {}).get("base_price") or 0)
    except Exception as e:
        print(f"[atlas-price] {e}")
    return 0.0


@app.post("/admin/parse-model-doc")
async def admin_parse_model_doc(request: Request, u=Depends(current_user)):
    """Atlas API hujjati matnidan model konfiguratsiyasini avtomatik chiqaradi (Claude)."""
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    if not ANTHROPIC_API_KEY:
        return err("AI kaliti sozlanmagan")
    body = await request.json()
    doc = str(body.get("doc") or "")[:16000]
    if len(doc) < 100:
        return err("Hujjat matnini to'liq yopishtiring")
    sysmsg = (
        "You extract AI model configuration from AtlasCloud API reference docs. "
        "Return ONLY valid JSON (no markdown) with keys: "
        "mid (model id like google/x/y), type ('video' or 'image'), name (short human name), "
        "res (array of resolution options, [] if none), "
        "asp (array of aspect_ratio options incl 'auto' if supported, [] if not accepted), "
        "dur (array of integer duration options, [] if no duration param), "
        "refs (max number of input images, 0 if none), refs_req (true if images required), "
        "ref_types (array from 'image','audio','video' based on accepted inputs), "
        "per_sec_price (true if output length depends on an input video), "
        "payload_map: {image_field (exact field name for images or null), image_single (true if single string), "
        "video_field (exact field for input video or null), audio_field (or null), "
        "send_duration (bool), duration_key, send_aspect (bool), aspect_key, "
        "send_resolution (bool), resolution_key, endpoint ('generateVideo' or 'generateImage')}. "
        "Also extract price_usd (number) from any Pricing section: the per-second rate for video models "
        "or per-image rate for image models; null if absent. "
        "Use EXACT field names from the doc's Input Schema."
    )
    payload = {"model": ENHANCE_MODEL, "max_tokens": 900, "system": sysmsg,
               "messages": [{"role": "user", "content": doc}]}
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
    jd = r.json()
    txt = " ".join(c.get("text", "") for c in (jd.get("content") or []) if c.get("type") == "text").strip()
    txt = txt.replace("```json", "").replace("```", "").strip()
    try:
        cfg = json.loads(txt)
    except Exception:
        return err("Hujjatni o'qib bo'lmadi — qo'lda to'ldiring")
    # Narx: avval hujjatdan, bo'lmasa Atlas katalogidan
    usd = 0.0
    try:
        usd = float(cfg.get("price_usd") or 0)
    except Exception:
        usd = 0.0
    if usd <= 0:
        usd = await atlas_model_price(str(cfg.get("mid") or ""))
    if usd > 0:
        import math
        cfg["suggested_coin"] = max(1, math.ceil(usd / CREDIT_USD))
        cfg["atlas_usd"] = usd
    return {"parsed": cfg}


@app.get("/custom-models")
async def custom_models_public():
    """Admin qo'shgan modellar — frontend ro'yxatlariga qo'shiladi."""
    out = []
    for r in _custom_rows():
        if not r["enabled"]:
            continue
        try:
            cfg = json.loads(r["config"])
        except Exception:
            continue
        out.append({"id": r["mid"], **{k: cfg.get(k) for k in
                    ("type", "name", "emoji", "hook", "res", "asp", "dur", "refs", "refs_req", "ref_types", "pricing")}})
    return {"models": out}


@app.get("/admin/custom-models")
async def admin_custom_models_list(u=Depends(current_user)):
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    out = []
    for r in _custom_rows():
        try:
            cfg = json.loads(r["config"])
        except Exception:
            cfg = {}
        out.append({"mid": r["mid"], "enabled": bool(r["enabled"]), "config": cfg})
    return {"models": out}


@app.post("/admin/custom-models")
async def admin_custom_models_save(request: Request, u=Depends(current_user)):
    """Yangi model qo'shish yoki mavjudini yangilash. body: {mid, config, enabled}"""
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    mid = str(b.get("mid") or "").strip()
    cfg = b.get("config")
    if not mid or "/" not in mid:
        return err("Atlas model ID noto'g'ri (masalan: google/gemini.../text-to-video)")
    if mid in MODELS:
        return err("Bu model allaqachon tizimda (statik)")
    if not isinstance(cfg, dict) or not cfg.get("name") or cfg.get("type") not in ("video", "image"):
        return err("Konfiguratsiya to'liq emas (nom, tur)")
    pr = cfg.get("pricing") or {}
    if not (pr.get("dur") or pr.get("res") or pr.get("per_sec")):
        return err("Narx kiritilmadi")
    q("""INSERT INTO custom_models(mid,config,enabled,created) VALUES(?,?,?,?)
         ON CONFLICT(mid) DO UPDATE SET config=excluded.config, enabled=excluded.enabled""",
      (mid, json.dumps(cfg, ensure_ascii=False), 1 if b.get("enabled", True) else 0, int(time.time())), commit=True)
    return {"ok": True}


@app.post("/admin/custom-models-delete")
async def admin_custom_models_delete(request: Request, u=Depends(current_user)):
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    q("DELETE FROM custom_models WHERE mid=?", (str(b.get("mid") or ""),), commit=True)
    return {"ok": True}


@app.get("/settings")
async def settings_public():
    """Sayt sozlamalari (hamma ko'radi): e'lon banneri, o'chirilgan modellar."""
    return {
        "announce": _get_setting("announce", {"enabled": False, "uz": "", "ru": "", "en": ""}),
        "disabled_models": _get_setting("disabled_models", []),
    }


@app.post("/admin/settings")
async def admin_settings(request: Request, u=Depends(current_user)):
    """Admin: sozlamani saqlash. body: {key, value}"""
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    key = str(b.get("key") or "")
    if key not in ("announce", "disabled_models"):
        return err("Kalit noto'g'ri")
    _set_setting(key, b.get("value"))
    return {"ok": True}


@app.get("/slot-media")
async def slot_media_public():
    """Bosh sahifa peshtaxtalari: har slot uchun video va biriktirilgan model."""
    return _slot_map()


@app.post("/admin/slot-media")
async def admin_slot_media(slot: str, file: UploadFile = File(...), u=Depends(current_user)):
    """Admin: bosh sahifa kartasiga (slot) video/rasm YUKLASH."""
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    slot = (slot or "").strip().lower()
    if slot not in HERO_SLOTS:
        return err("Slot noto'g'ri")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SLOT_MEDIA_EXT:
        return err("Faqat mp4/webm video yoki jpg/png/webp rasm")
    data = await file.read()
    if len(data) > SLOT_MEDIA_MAX:
        return err("Fayl 100 MB dan katta")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    # Video -> web uchun optimallashtirish (hero har tashrifchiga yuklanadi!)
    if ext in (".mp4", ".webm", ".mov"):
        try:
            import subprocess, tempfile, os
            with tempfile.TemporaryDirectory() as td:
                fin = os.path.join(td, "in" + ext)
                fout = os.path.join(td, "out.mp4")
                with open(fin, "wb") as f:
                    f.write(data)
                r = subprocess.run(["ffmpeg", "-y", "-i", fin,
                                    "-vf", "scale='min(720,iw)':-2",
                                    "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                                    "-an", "-movflags", "+faststart", fout],
                                   capture_output=True, timeout=240)
                if r.returncode == 0 and 1000 < os.path.getsize(fout) < len(data):
                    with open(fout, "rb") as f:
                        newdata = f.read()
                    print(f"[slot-media] siqildi: {len(data)//1024}KB -> {len(newdata)//1024}KB")
                    data, ext = newdata, ".mp4"
        except Exception as e:
            print(f"[slot-media] siqish o'tkazildi: {e}")
        if len(data) > 60 * 1024 * 1024:
            return err("Video siqishdan keyin ham juda katta — qisqaroq video tanlang")
    fname = f"cover_{slot}_{int(time.time())}{ext}"
    (MEDIA_DIR / fname).write_bytes(data)
    # eski faylni tozalaymiz
    old = q("SELECT url FROM slot_media WHERE slot=?", (slot,), one=True)
    if old and str(old["url"]).startswith("/media/"):
        try:
            (MEDIA_DIR / Path(old["url"]).name).unlink(missing_ok=True)
        except Exception:
            pass
    url = f"/media/{fname}"
    q("INSERT INTO slot_media(slot,url,updated) VALUES(?,?,?) ON CONFLICT(slot) DO UPDATE SET url=excluded.url, updated=excluded.updated",
      (slot, url, int(time.time())), commit=True)
    return {"ok": True, "media": _slot_map()}


@app.post("/admin/slot-config")
async def admin_slot_config(request: Request, u=Depends(current_user)):
    """Admin: peshtaxta kartasiga MODEL biriktirish — karta bosilganda shu model ochiladi."""
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    slot = str(b.get("slot") or "").strip().lower()
    mid = str(b.get("mid") or "").strip()
    if slot not in HERO_SLOTS:
        return err("Slot noto'g'ri")
    if mid and not get_model(mid):
        return err("Model topilmadi", 404)
    row = q("SELECT slot FROM slot_media WHERE slot=?", (slot,), one=True)
    if row:
        q("UPDATE slot_media SET mid=?, updated=? WHERE slot=?", (mid or None, int(time.time()), slot), commit=True)
    elif mid:
        q("INSERT INTO slot_media(slot,url,mid,updated) VALUES(?,NULL,?,?)", (slot, mid, int(time.time())), commit=True)
    return {"ok": True, "media": _slot_map()}


@app.post("/admin/slot-media-delete")
async def admin_slot_media_delete(request: Request, u=Depends(current_user)):
    if not u or u["email"] != ADMIN_EMAIL:
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    slot = str(b.get("slot") or "").strip().lower()
    old = q("SELECT url FROM slot_media WHERE slot=?", (slot,), one=True)
    if old and str(old["url"]).startswith("/media/"):
        try:
            (MEDIA_DIR / Path(old["url"]).name).unlink(missing_ok=True)
        except Exception:
            pass
    row = q("SELECT mid FROM slot_media WHERE slot=?", (slot,), one=True)
    if row and row["mid"]:
        q("UPDATE slot_media SET url=NULL, updated=? WHERE slot=?", (int(time.time()), slot), commit=True)
    else:
        q("DELETE FROM slot_media WHERE slot=?", (slot,), commit=True)
    return {"ok": True, "media": _slot_map()}


@app.get("/admin/stats")
async def admin_stats(u=Depends(current_user)):
    if not _adm(u):
        return err("Ruxsat yo'q", 403)
    day = int(time.time()) - 86400
    us = q("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b FROM users", one=True)
    pd = q("SELECT COUNT(*) c, COALESCE(SUM(amount_uzs),0) s FROM orders WHERE status='paid'", one=True)
    pd24 = q("SELECT COUNT(*) c, COALESCE(SUM(amount_uzs),0) s FROM orders WHERE status='paid' AND paid_at>=?", (day,), one=True)
    done = q("SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM jobs WHERE status='done'", one=True)
    fail = q("SELECT COUNT(*) c FROM jobs WHERE status='failed'", one=True)
    j24 = q("SELECT COUNT(*) c FROM jobs WHERE created>=?", (day,), one=True)
    linked = q("SELECT COUNT(*) c FROM users WHERE telegram_id IS NOT NULL", one=True)
    return {"users": us["c"], "linked": linked["c"], "balance_total": us["b"],
            "paid_count": pd["c"], "revenue_uzs": pd["s"],
            "paid24": pd24["c"], "revenue24": pd24["s"],
            "jobs_done": done["c"], "spent_tangacha": done["s"],
            "jobs_failed": fail["c"], "jobs24": j24["c"]}


@app.get("/admin/users")
async def admin_users(query: str = "", limit: int = 50, u=Depends(current_user)):
    if not _adm(u):
        return err("Ruxsat yo'q", 403)
    limit = max(1, min(int(limit), 100))
    like = f"%{query.strip()}%"
    rows = q("""SELECT id,email,name,balance,telegram_id,created FROM users
                WHERE email LIKE ? OR name LIKE ? ORDER BY id DESC LIMIT ?""", (like, like, limit))
    return [{"id": r["id"], "email": r["email"], "name": r["name"], "balance": r["balance"],
             "tg": bool(r["telegram_id"]), "created": r["created"]} for r in rows]


@app.post("/admin/credit")
async def admin_credit(request: Request, u=Depends(current_user)):
    if not _adm(u):
        return err("Ruxsat yo'q", 403)
    b = await request.json()
    uid = int(b.get("user_id", 0))
    amount = int(b.get("amount", 0))
    if amount == 0:
        return err("Summa 0 bo'lmasin")
    t = q("SELECT id FROM users WHERE id=?", (uid,), one=True)
    if not t:
        return err("Foydalanuvchi topilmadi", 404)
    q("UPDATE users SET balance = MAX(0, balance + ?) WHERE id=?", (amount, uid), commit=True)
    nb = q("SELECT balance FROM users WHERE id=?", (uid,), one=True)["balance"]
    return {"balance": nb}


@app.get("/admin/orders")
async def admin_orders(limit: int = 40, u=Depends(current_user)):
    if not _adm(u):
        return err("Ruxsat yo'q", 403)
    limit = max(1, min(int(limit), 100))
    rows = q("""SELECT o.*, us.email FROM orders o LEFT JOIN users us ON us.id=o.user_id
                ORDER BY o.id DESC LIMIT ?""", (limit,))
    return [{"id": r["id"], "email": r["email"], "method": r["method"], "amount_uzs": r["amount_uzs"],
             "tangacha": r["tangacha"] + r["bonus"], "status": r["status"], "created": r["created"]} for r in rows]


@app.get("/admin/jobs")
async def admin_jobs(limit: int = 40, u=Depends(current_user)):
    if not _adm(u):
        return err("Ruxsat yo'q", 403)
    limit = max(1, min(int(limit), 100))
    rows = q("""SELECT j.id,j.mid,j.price,j.status,j.error,j.created, us.email
                FROM jobs j LEFT JOIN users us ON us.id=j.user_id
                ORDER BY j.created DESC LIMIT ?""", (limit,))
    return [{"id": r["id"], "model": (get_model(r["mid"]) or {}).get("name", r["mid"]), "mid": r["mid"],
             "price": r["price"], "status": r["status"], "error": friendly_error(r["error"]) if r["status"] == "failed" else r["error"],
             "email": r["email"], "created": r["created"]} for r in rows]


# ────────────────────────── TO'LOV: buyurtma yaratish ──
@app.post("/pay/create")
async def pay_create(request: Request, u=Depends(current_user)):
    if not u:
        return err("Kirish talab qilinadi", 401)
    b = await request.json()
    try:
        pkg = PACKAGES[int(b.get("pkg"))]
    except Exception:
        return err("Paket noto'g'ri")
    method = b.get("method")
    if method not in ("payme", "click", "octo"):
        return err("To'lov usuli noto'g'ri")
    if method == "octo":
        return err("Visa to'lovi tez orada qo'shiladi — hozircha Payme yoki Click'dan foydalaning", 501)
    if method == "payme" and not PAYME_MERCHANT_ID:
        return err("Payme hali sozlanmagan", 503)
    if method == "click" and not (CLICK_SERVICE_ID and CLICK_MERCHANT_ID):
        return err("Click hali sozlanmagan", 503)

    cur = q("INSERT INTO orders(user_id,tangacha,bonus,amount_uzs,method,status,created) VALUES(?,?,?,?,?,?,?)",
            (u["id"], pkg["t"], pkg["b"], pkg["uzs"], method, "new", int(time.time())), commit=True)
    order_id = cur.lastrowid

    if method == "payme":
        # c= to'lovdan keyin qaytish manzili, l=uz — interfeys tili
        raw = f"m={PAYME_MERCHANT_ID};ac.order_id={order_id};a={pkg['uzs'] * 100};l=uz;c={PUBLIC_BASE_URL}/"
        link = "https://checkout.paycom.uz/" + base64.b64encode(raw.encode()).decode()
        return {"url": link}
    # click
    link = ("https://my.click.uz/services/pay"
            f"?service_id={CLICK_SERVICE_ID}&merchant_id={CLICK_MERCHANT_ID}"
            f"&amount={pkg['uzs']}&transaction_param={order_id}"
            f"&return_url={PUBLIC_BASE_URL}/")
    return {"url": link}


# ────────────────────────── PAYME Merchant API (JSON-RPC) ──
def _payme_err(rid, code, msg):
    return JSONResponse({"jsonrpc": "2.0", "id": rid,
                         "error": {"code": code, "message": {"uz": msg, "ru": msg, "en": msg}}})


def _payme_ok(rid, result):
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _payme_auth_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth[6:]).decode()
    except Exception:
        return False
    for key in (PAYME_KEY, PAYME_TEST_KEY):
        if key and raw == f"Paycom:{key}":
            return True
    return False


@app.post("/pay/payme")
async def payme_rpc(request: Request):
    body = await request.json()
    rid = body.get("id")
    if not _payme_auth_ok(request):
        return _payme_err(rid, -32504, "Avtorizatsiya xatosi")
    method = body.get("method")
    p = body.get("params") or {}
    now_ms = int(time.time() * 1000)

    def order_by_account():
        try:
            oid = int((p.get("account") or {}).get("order_id"))
        except Exception:
            return None
        return q("SELECT * FROM orders WHERE id=? AND method='payme'", (oid,), one=True)

    def order_by_trx():
        return q("SELECT * FROM orders WHERE payme_id=?", (str(p.get("id")),), one=True)

    if method == "CheckPerformTransaction":
        o = order_by_account()
        if not o:
            return _payme_err(rid, -31050, "Buyurtma topilmadi")
        if o["status"] != "new":
            return _payme_err(rid, -31050, "Buyurtma allaqachon yopilgan")
        if int(p.get("amount", 0)) != o["amount_uzs"] * 100:
            return _payme_err(rid, -31001, "Summa noto'g'ri")
        return _payme_ok(rid, {"allow": True})

    PAYME_TIMEOUT_MS = 12 * 3600 * 1000   # M5: Payme talabi — 12 soatdan oshgan state=1 tranzaksiya bekor qilinadi

    if method == "CreateTransaction":
        o = order_by_account()
        if not o:
            return _payme_err(rid, -31050, "Buyurtma topilmadi")
        if int(p.get("amount", 0)) != o["amount_uzs"] * 100:
            return _payme_err(rid, -31001, "Summa noto'g'ri")
        trx = str(p.get("id"))
        if o["payme_id"] and o["payme_id"] != trx:
            return _payme_err(rid, -31050, "Buyurtma boshqa tranzaksiyada band")
        if o["payme_id"] == trx:
            # M5: takroriy chaqiruvda muddati o'tgan bo'lsa — bekor qilamiz
            if o["payme_state"] == 1 and now_ms - o["payme_create_ms"] > PAYME_TIMEOUT_MS:
                q("UPDATE orders SET payme_state=-1, payme_cancel_ms=?, payme_reason=4, status='canceled' WHERE id=?",
                  (now_ms, o["id"]), commit=True)
                return _payme_err(rid, -31008, "Tranzaksiya muddati o'tdi")
            return _payme_ok(rid, {"create_time": o["payme_create_ms"], "transaction": str(o["id"]), "state": o["payme_state"]})
        if o["status"] != "new":
            return _payme_err(rid, -31050, "Buyurtma yopilgan")
        q("UPDATE orders SET payme_id=?, payme_state=1, payme_create_ms=? WHERE id=?",
          (trx, now_ms, o["id"]), commit=True)
        return _payme_ok(rid, {"create_time": now_ms, "transaction": str(o["id"]), "state": 1})

    if method == "PerformTransaction":
        o = order_by_trx()
        if not o:
            return _payme_err(rid, -31003, "Tranzaksiya topilmadi")
        if o["payme_state"] == 2:
            return _payme_ok(rid, {"transaction": str(o["id"]), "perform_time": o["payme_perform_ms"], "state": 2})
        if o["payme_state"] != 1:
            return _payme_err(rid, -31008, "Tranzaksiya holati noto'g'ri")
        # M5: 12 soatdan oshgan tranzaksiya bajarilmaydi — bekor qilinadi
        if now_ms - o["payme_create_ms"] > PAYME_TIMEOUT_MS:
            q("UPDATE orders SET payme_state=-1, payme_cancel_ms=?, payme_reason=4, status='canceled' WHERE id=?",
              (now_ms, o["id"]), commit=True)
            return _payme_err(rid, -31008, "Tranzaksiya muddati o'tdi")
        credit_order(o)
        q("UPDATE orders SET payme_state=2, payme_perform_ms=? WHERE id=?", (now_ms, o["id"]), commit=True)
        return _payme_ok(rid, {"transaction": str(o["id"]), "perform_time": now_ms, "state": 2})

    if method == "CancelTransaction":
        o = order_by_trx()
        if not o:
            return _payme_err(rid, -31003, "Tranzaksiya topilmadi")
        reason = p.get("reason")
        if o["payme_state"] == 1:
            q("UPDATE orders SET payme_state=-1, payme_cancel_ms=?, payme_reason=?, status='canceled' WHERE id=?",
              (now_ms, reason, o["id"]), commit=True)
            return _payme_ok(rid, {"transaction": str(o["id"]), "cancel_time": now_ms, "state": -1})
        if o["payme_state"] == 2:
            total = o["tangacha"] + o["bonus"]
            q("UPDATE users SET balance = MAX(0, balance - ?) WHERE id=?", (total, o["user_id"]), commit=True)
            q("UPDATE orders SET payme_state=-2, payme_cancel_ms=?, payme_reason=?, status='canceled' WHERE id=?",
              (now_ms, reason, o["id"]), commit=True)
            return _payme_ok(rid, {"transaction": str(o["id"]), "cancel_time": now_ms, "state": -2})
        return _payme_ok(rid, {"transaction": str(o["id"]), "cancel_time": o["payme_cancel_ms"], "state": o["payme_state"]})

    if method == "CheckTransaction":
        o = order_by_trx()
        if not o:
            return _payme_err(rid, -31003, "Tranzaksiya topilmadi")
        return _payme_ok(rid, {"create_time": o["payme_create_ms"], "perform_time": o["payme_perform_ms"],
                               "cancel_time": o["payme_cancel_ms"], "transaction": str(o["id"]),
                               "state": o["payme_state"], "reason": o["payme_reason"]})

    if method == "GetStatement":
        rows = q("SELECT * FROM orders WHERE method='payme' AND payme_create_ms BETWEEN ? AND ?",
                 (int(p.get("from", 0)), int(p.get("to", now_ms))))
        txs = [{"id": o["payme_id"], "time": o["payme_create_ms"], "amount": o["amount_uzs"] * 100,
                "account": {"order_id": str(o["id"])}, "create_time": o["payme_create_ms"],
                "perform_time": o["payme_perform_ms"], "cancel_time": o["payme_cancel_ms"],
                "transaction": str(o["id"]), "state": o["payme_state"], "reason": o["payme_reason"]}
               for o in rows if o["payme_id"]]
        return _payme_ok(rid, {"transactions": txs})

    return _payme_err(rid, -32601, "Metod topilmadi")


# ────────────────────────── CLICK SHOP-API ──
def _click_sign_ok(f, with_prepare):
    parts = [str(f.get("click_trans_id", "")), str(f.get("service_id", "")), CLICK_SECRET_KEY,
             str(f.get("merchant_trans_id", ""))]
    if with_prepare:
        parts.append(str(f.get("merchant_prepare_id", "")))
    parts += [str(f.get("amount", "")), str(f.get("action", "")), str(f.get("sign_time", ""))]
    return hashlib.md5("".join(parts).encode()).hexdigest() == str(f.get("sign_string", ""))


@app.post("/pay/click/prepare")
async def click_prepare(request: Request):
    f = dict(await request.form())
    if not _click_sign_ok(f, with_prepare=False):
        return {"error": -1, "error_note": "Imzo noto'g'ri"}
    o = q("SELECT * FROM orders WHERE id=? AND method='click'", (f.get("merchant_trans_id"),), one=True)
    if not o:
        return {"error": -5, "error_note": "Buyurtma topilmadi"}
    if o["status"] == "paid":
        return {"error": -4, "error_note": "Allaqachon to'langan"}
    if o["status"] != "new":
        return {"error": -9, "error_note": "Buyurtma bekor qilingan"}
    try:
        if abs(float(f.get("amount", 0)) - float(o["amount_uzs"])) > 0.01:
            return {"error": -2, "error_note": "Summa noto'g'ri"}
    except Exception:
        return {"error": -2, "error_note": "Summa noto'g'ri"}
    q("UPDATE orders SET click_trans_id=? WHERE id=?", (str(f.get("click_trans_id")), o["id"]), commit=True)
    return {"click_trans_id": f.get("click_trans_id"), "merchant_trans_id": f.get("merchant_trans_id"),
            "merchant_prepare_id": o["id"], "error": 0, "error_note": "Success"}


@app.post("/pay/click/complete")
async def click_complete(request: Request):
    f = dict(await request.form())
    if not _click_sign_ok(f, with_prepare=True):
        return {"error": -1, "error_note": "Imzo noto'g'ri"}
    o = q("SELECT * FROM orders WHERE id=? AND method='click'", (f.get("merchant_trans_id"),), one=True)
    if not o:
        return {"error": -5, "error_note": "Buyurtma topilmadi"}
    if str(f.get("error", "0")) not in ("0", "0.0"):
        q("UPDATE orders SET status='canceled' WHERE id=? AND status='new'", (o["id"],), commit=True)
        return {"error": -9, "error_note": "To'lov bekor qilindi"}
    if o["status"] == "paid":
        return {"error": -4, "error_note": "Allaqachon to'langan"}
    if o["status"] != "new":
        return {"error": -9, "error_note": "Buyurtma bekor qilingan"}
    credit_order(o)
    return {"click_trans_id": f.get("click_trans_id"), "merchant_trans_id": f.get("merchant_trans_id"),
            "merchant_confirm_id": o["id"], "error": 0, "error_note": "Success"}




# ════════════ TELEGRAM MINI APP (zaxiradan tiklandi) ════════════

import urllib.parse as _urlparse

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERS_JSON = os.getenv("BOT_USERS_JSON", "/root/users.json")  # lazy migration manbasi
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Mini App prompt enhance uchun
ENHANCE_MODEL = "claude-haiku-4-5-20251001"
BOT_FILE = os.getenv("BOT_FILE", "/root/bot/voro_creator_bot.py")

TOOLS_LIST = [
    {
        "id": "restore",
        "name": "Eski rasmni tiklash",
        "emoji": "restore",
        "desc": "Eskirgan, shikastlangan rasmni tiklab, ranglar qo'shadi",
        "model_id": "google/nano-banana-2/edit",
        "resolution": "2k",
        "aspect": "3:4",
        "cost": 9,
        "input": "image",
        "prompt": 'Restore and colorize this old damaged photograph. Repair all damage: remove cracks, scratches, stains, fading, dust, and tears. Convert to natural realistic full color. Make it look like a freshly taken modern high-quality photograph sharp, clear, clean, well-lit. CRITICAL preserve identity exactly: keep the person face 100 percent faithful to the original same exact facial features, eyes, nose, mouth, face shape, expression, age, and gender. Do NOT change the face, do NOT beautify only restore and colorize. Keep the same pose, composition, and clothing. PHOTOREALISTIC: natural realistic skin texture, natural lighting.',
    },
    {
        "id": "wedding",
        "name": "To'y taklifnomasi",
        "emoji": "wedding",
        "desc": "Kuyov+kelin rasmidan premium taklifnoma",
        "model_id": "openai/gpt-image-2/edit",
        "resolution": "high",
        "aspect": "2:3",
        "cost": 12,
        "input": "form",
        "images": [{"key": "kuyov_img", "label": "Kuyov rasmi"}, {"key": "kelin_img", "label": "Kelin rasmi"}],
        "fields": [
            {"key": "kuyov", "label": "Kuyov ismi", "placeholder": "Jasur"},
            {"key": "kelin", "label": "Kelin ismi", "placeholder": "Madina"},
            {"key": "kimga", "label": "Kimga", "placeholder": "Aziz aka oilasi bilan"},
            {"key": "sana_vaqt", "label": "Sana va vaqt", "placeholder": "15-avgust 2026, soat 18:00"},
            {"key": "manzil", "label": "Toyxona manzili", "placeholder": "Navroz toyxonasi, Toshkent"},
        ],
        "prompt": 'Create a luxurious elegant wedding invitation card in a refined botanical gold style. Vertical portrait, aspect ratio 2:3. Designed like a premium wedding stationery studio print on soft textured paper. FIXED DESIGN IDENTICAL EVERY TIME: Background soft ivory cream watercolor paper with very subtle warm beige tones and faint watercolor wash. All decorative elements elegant metallic gold. Delicate thin gold ornamental corner flourishes at the very top corners. Large beautiful gold line-art roses and botanical foliage growing from the bottom-left and bottom-right corners and along the lower third, elegant outlined golden rose illustrations. Perfectly symmetrical centered composition, generous soft empty space, airy high-end romantic luxury. LAYOUT top to bottom: 1. Top center small elegant serif line in soft gold: Taklifnoma. 2. Just below in slightly larger elegant gold serif capitals with wide spacing a short warm phrase. 3. Center upper-middle a soft rounded arch photo frame containing a photorealistic waist-up portrait of the couple embracing closely in a warm romantic pose, softly blurred dreamy floral garden background, gentle warm light. Groom on the LEFT in an elegant light suit, his face an EXACT match of the man in the FIRST input image, same identity features and skin tone, do not beautify or alter. Bride on the RIGHT in a beautiful white lace wedding dress, her face an EXACT match of the woman in the SECOND input image, same identity features and skin tone, do not beautify or alter. Tender loving mood. 4. Below the photo the couple names KUYOV_NAME and KELIN_NAME joined elegantly, in large graceful flowing gold calligraphy script, exact spelling, the visual centerpiece. 5. Below a thin elegant gold divider then the date and time beautifully spaced in refined serif: SANA_VAQT. 6. Below in smaller elegant dark bronze serif each on own line: Hurmatli KIMGA_NAME and MANZIL_NAME. TEXT RULES render every provided text exactly as given, correct Uzbek spelling, sharp readable elegant typography. STRICT RULES keep this exact luxurious botanical-gold layout every time, no extra people, no children, no watermark, no logo, no cartoon faces, natural realistic faces, no oversaturated colors, tasteful and elegant.',
    },
]

def verify_tg_init_data(init_data: str, max_age: int = 86400):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa user dict qaytaradi, aks holda None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(_urlparse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    # data_check_string: kalitlar alfavit tartibida, key=value\n bilan
    pairs = sorted(f"{k}={v}" for k, v in parsed.items())
    data_check = "\n".join(pairs)
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, recv_hash):
        return None
    # auth_date eskirmaganini tekshirish
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        if max_age and (time.time() - auth_date) > max_age:
            return None
    except Exception:
        return None
    # user JSON ni ajratib olish
    try:
        user = json.loads(parsed.get("user", "{}"))
    except Exception:
        return None
    if not user.get("id"):
        return None
    return user

def verify_signed_uid(uid: str, sig: str) -> bool:
    """Bot imzolagan uid ni tekshiradi (KeyboardButton Mini App uchun — initData yo'q).

    Bot: sig = HMAC_SHA256(sha256(BOT_TOKEN), str(uid))[:32]. Bir xil algoritm.
    """
    if not uid or not sig or not BOT_TOKEN:
        return False
    try:
        key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        calc = hmac.new(key, str(int(uid)).encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(calc, sig)
    except Exception:
        return False

def tg_get_or_migrate_user(tg_user: dict):
    """Telegram user'ni DB'da topadi. Yo'q bo'lsa bot users.json'dan ko'chiradi (lazy migration).

    Muhim: balans FAQAT birinchi ko'chirishda o'rnatiladi (migrated=1). Keyin
    DB yagona manba bo'ladi — qayta ko'chirilmaydi (balansni ustidan yozmaslik uchun).
    """
    tg_id = int(tg_user["id"])
    name = (tg_user.get("first_name") or "").strip() or "Foydalanuvchi"
    if tg_user.get("last_name"):
        name = (name + " " + tg_user["last_name"]).strip()
    username = tg_user.get("username")

    with _lock:
        d = db()
        row = d.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row:
            # Mavjud — ism/username yangilab, qaytaramiz (balansga TEGMAYMIZ)
            d.execute("UPDATE users SET name=?, tg_username=? WHERE tg_id=?",
                      (name, username, tg_id))
            d.commit()
            return d.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

        # Yo'q — bot users.json'dan qidiramiz (lazy migration)
        bot_users = _load_bot_users()
        bu = bot_users.get(str(tg_id))
        now = int(time.time())
        if bu and isinstance(bu, dict) and "tangacha" in bu:
            bal = int(bu.get("tangacha", 0))
            streak = int(bu.get("streak", 0))
            stars = int(bu.get("stars_paid", 0))
            spent = int(bu.get("total", 0))
            banned = 1 if bu.get("banned") else 0
            d.execute("""INSERT INTO users(email, pass_hash, salt, name, tg_id, tg_username, balance, streak,
                          stars_paid, total_spent, banned, migrated, created)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                      (f"tg{tg_id}@telegram.local", "tg", "tg", name, tg_id, username, bal, streak, stars, spent, banned, now))
        else:
            # Bot'da ham yo'q — yangi user (bonus bilan)
            d.execute("""INSERT INTO users(email, pass_hash, salt, name, tg_id, tg_username, balance, migrated, created)
                         VALUES(?,?,?,?,?,?,?,0,?)""",
                      (f"tg{tg_id}@telegram.local", "tg", "tg", name, tg_id, username, SIGNUP_BONUS, now))
        d.commit()
        return d.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

def tg_user_json(u):
    """Mini App uchun user ma'lumoti (email o'rniga tg maydonlari)."""
    return {
        "name": u["name"],
        "balance": u["balance"],
        "streak": u["streak"] if "streak" in u.keys() else 0,
        "tg_username": u["tg_username"] if "tg_username" in u.keys() else None,
        "lang": _tg_user_lang(u),
    }

async def tg_current_user(request: Request):
    """Mini App so'rovlaridan foydalanuvchini aniqlaydi (yoki None).

    Ikki usul:
      1) initData (inline/menu tugmasidan ochilganda) — Telegram HMAC.
      2) imzolangan uid (KeyboardButton'dan ochilganda — initData bo'sh keladi):
         bot URL'ga &uid=&n=&sig= qo'shadi, sig BOT_TOKEN bilan tekshiriladi.
    """
    # 1-usul: initData
    init_data = request.headers.get("X-Tg-Init-Data", "")
    if not init_data:
        init_data = request.query_params.get("initData", "")
    tg_user = verify_tg_init_data(init_data)
    if tg_user:
        return tg_get_or_migrate_user(tg_user)

    # 2-usul: imzolangan uid (header yoki query)
    uid = request.headers.get("X-Tg-Uid", "") or request.query_params.get("uid", "")
    sig = request.headers.get("X-Tg-Sig", "") or request.query_params.get("sig", "")
    name = request.headers.get("X-Tg-Name", "") or request.query_params.get("n", "")
    if verify_signed_uid(uid, sig):
        import urllib.parse as _up
        fake_user = {"id": int(uid), "first_name": _up.unquote(name) if name else ""}
        return tg_get_or_migrate_user(fake_user)

    return None

def _load_bot_users():
    """Bot users.json ni o'qiydi (lazy migration uchun). Xato bo'lsa bo'sh dict."""
    try:
        with open(BOT_USERS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _tg_user_lang(u):
    try:
        _tgid = u["tg_id"] if "tg_id" in u.keys() else None
        if not _tgid:
            return None
        bu = _load_bot_users().get(str(_tgid), {})
        return bu.get("lang")
    except Exception:
        return None

def _kind_for_ext(e):
    if e in ALLOWED_AUDIO: return "audio"
    if e in ALLOWED_VIDEO: return "video"
    return "image"

_TEMPLATES_CACHE = None

_TT_TR = {
    # TEMPLATES name/desc
    "📸 Fotosessiya": {"ru": "📸 Фотосессия", "en": "📸 Photoshoot"},
    "Bitta rasmdan professional portret": {"ru": "Профессиональный портрет из одного фото", "en": "A professional portrait from one photo"},
    # Fotosessiya style labels
    "📸 Professional": {"ru": "📸 Профессиональный", "en": "📸 Professional"},
    "🌸 Yengil": {"ru": "🌸 Лёгкий", "en": "🌸 Light"},
    "🏛 Klassik": {"ru": "🏛 Классический", "en": "🏛 Classic"},
    "🎬 Kinematik": {"ru": "🎬 Кинематографичный", "en": "🎬 Cinematic"},
    "🌅 Tabiat": {"ru": "🌅 Природа", "en": "🌅 Nature"},
    "🇺🇿 Milliy": {"ru": "🇺🇿 Национальный", "en": "🇺🇿 National"},
    "💎 Glamour": {"ru": "💎 Гламур", "en": "💎 Glamour"},
    "📱 Instagram": {"ru": "📱 Instagram", "en": "📱 Instagram"},
    "💐 Mr X Flowers": {"ru": "💐 Mr X Flowers", "en": "💐 Mr X Flowers"},
    "🏍 Moto · Sport": {"ru": "🏍 Мото · Спорт", "en": "🏍 Moto · Sport"},
    "🏍 Moto": {"ru": "🏍 Мото", "en": "🏍 Moto"},
    "🚗 BMW": {"ru": "🚗 BMW", "en": "🚗 BMW"},
    "🚙 Mercedes": {"ru": "🚙 Mercedes", "en": "🚙 Mercedes"},
    "🎂 Tug'ilgan Kun": {"ru": "🎂 День рождения", "en": "🎂 Birthday"},
    "🦅 Terma A'zosi": {"ru": "🦅 Игрок сборной", "en": "🦅 National Team Player"},
    "🏟 Stadion Selfi": {"ru": "🏟 Селфи на стадионе", "en": "🏟 Stadium Selfie"},
    "🤳 Futbolchi Selfi": {"ru": "🤳 Селфи с футболистом", "en": "🤳 Footballer Selfie"},
    # TOOLS name/desc
    "Eski rasmni tiklash": {"ru": "Реставрация старого фото", "en": "Restore old photo"},
    "Eskirgan, shikastlangan rasmni tiklab, ranglar qo'shadi": {"ru": "Восстанавливает старое повреждённое фото и добавляет цвет", "en": "Restores an old damaged photo and adds color"},
    "To'y taklifnomasi": {"ru": "Свадебное приглашение", "en": "Wedding invitation"},
    "Kuyov+kelin rasmidan premium taklifnoma": {"ru": "Премиум-приглашение из фото жениха и невесты", "en": "A premium invitation from the groom and bride photos"},
    # Wedding form field labels + placeholders
    "Kuyov rasmi": {"ru": "Фото жениха", "en": "Groom photo"},
    "Kelin rasmi": {"ru": "Фото невесты", "en": "Bride photo"},
    "Kuyov ismi": {"ru": "Имя жениха", "en": "Groom name"},
    "Kelin ismi": {"ru": "Имя невесты", "en": "Bride name"},
    "Kimga": {"ru": "Кому", "en": "To whom"},
    "Sana va vaqt": {"ru": "Дата и время", "en": "Date and time"},
    "Toyxona manzili": {"ru": "Адрес зала торжеств", "en": "Venue address"},
}

def _tt(txt, lang):
    """uz matnni lang tiliga o'giradi (lug'atda bo'lsa), aks holda uz qaytaradi."""
    if not lang or lang == "uz":
        return txt
    tr = _TT_TR.get(txt)
    if tr and tr.get(lang):
        return tr[lang]
    return txt


def load_templates():
    global _TEMPLATES_CACHE
    if _TEMPLATES_CACHE is not None:
        return _TEMPLATES_CACHE
    try:
        src = open(BOT_FILE, encoding="utf-8").read()
        i = src.index("TEMPLATES = [")
        # TEMPLATES dan _TEMPLATE_BY_ID gacha bo'lgan blokni olamiz
        end = src.index("_TEMPLATE_BY_ID", i)
        block = src[i:end]
        _ns = {}
        exec(compile(block, "<templates>", "exec"), {}, _ns)
        _TEMPLATES_CACHE = _ns.get("TEMPLATES", [])
    except Exception as e:
        print("load_templates xato:", e)
        _TEMPLATES_CACHE = []
    return _TEMPLATES_CACHE

def build_template_prompt(template_id, style_id):
    for t in load_templates():
        if t.get("id") != template_id: continue
        for st in t.get("styles", []):
            if st.get("id") != style_id: continue
            desc = st.get("desc", "")
            face_lock = " CRITICAL FACE LOCK: The person in the reference image is the subject preserve their face with ZERO alteration: same face shape, bone structure, skin tone, eye shape, nose, lips, jawline every detail identical to the reference. Head angle and gaze direction must match the reference exactly. Do NOT idealize, smooth, beautify or generalize any facial feature."
            prompt = ("The person from the image (same gender and identity as reference), medium close-up shot, face at angle matching the reference photo, matching expression from reference. " + desc + ". Photorealistic, high quality, professional portrait." + face_lock)
            return prompt, t.get("model_id"), t.get("resolution"), t.get("aspect"), t.get("cost")
    return None, None, None, None, None

def get_tool(tid):
    for t in TOOLS_LIST:
        if t["id"] == tid: return t
    return None

# ═══ Har model oilasi qanday promptni "yaxshi tushunadi" — professional profillar ═══
MODEL_PROMPT_PROFILES = {
    "bytedance/seedance": (
        "Seedance responds best to STRUCTURED prompts with reference tags. "
        "Use @image1/@image2/@video1/@audio1 tags to bind uploaded media (identity from images, motion from video, "
        "voice from audio). Never describe a tagged subject's face in words — the tag carries identity. "
        "Structure: [subject + tags] -> [action, specific verbs] -> [camera: shot type, movement] -> "
        "[environment, lighting] -> [mood/style]. Supports lipsync when audio present."
    ),
    "google/gemini-omni-flash": (
        "Gemini Omni prefers a flowing CINEMATIC NARRATIVE paragraph, like a director describing a shot. "
        "No @tags — instead refer to 'the person from the reference image' / 'the product shown in the reference' "
        "and describe them precisely. It generates native AUDIO: include a sound layer — ambient sounds, "
        "SFX, and spoken dialogue in double quotes with the speaker named (e.g. The man says: \"...\"). "
        "Specify camera movement (slow dolly-in, handheld, aerial), lighting quality and color palette."
    ),
    "google/veo": (
        "Veo likes CONCISE, precise prompts: one clear subject, one clear action, explicit camera language "
        "(close-up, tracking shot, low angle), lighting and style keywords. Dialogue in double quotes generates speech. "
        "No reference tags — describe subjects in words. Avoid over-stuffed scenes; one strong idea per prompt."
    ),
    "kwaivgi/kling": (
        "Kling excels at MOTION: lead with strong action verbs and physical detail (how fabric moves, how weight shifts). "
        "Describe the first frame (matching the uploaded image), then the motion that unfolds, then camera movement. "
        "Keep faces consistent by saying 'the same person as in the input image'. No @tags."
    ),
    "minimax/hailuo": (
        "Hailuo prefers dynamic scene descriptions with clear subject-action-camera order and expressive motion. "
        "Use cinematic terms (whip pan, slow motion, rack focus). No @tags — plain descriptive English."
    ),
    "alibaba/wan": (
        "Wan works best with clean visual descriptions: subject, action, setting, lighting, art style. "
        "Concrete nouns and colors over abstract adjectives. No @tags."
    ),
    "google/nano-banana": (
        "Nano Banana is an EDITING model: write an imperative instruction, not a scene description. "
        "Pattern: 'Add/Remove/Replace/Change X ... while keeping the original composition, subject identity, "
        "lighting and background unchanged.' Be surgical: name exactly what changes and exactly what must stay."
    ),
    "bytedance/seedream": (
        "Seedream edit: give a direct transformation instruction referencing the uploaded image(s) — what to change, "
        "what style to apply, what must remain identical (face, pose, layout). For generation: rich single-paragraph "
        "scene with composition, lens, lighting, palette and style keywords."
    ),
    "openai/gpt-image": (
        "GPT Image handles complex detailed scenes and RENDERED TEXT: any words that must appear in the image "
        "go in double quotes with placement (e.g. a neon sign reading \"VORO\"). Describe composition, "
        "style, lighting, and mood in full sentences."
    ),
    "atlascloud/infinitetalk": (
        "InfiniteTalk is lipsync: the prompt should describe the speaker's emotional delivery, head/eye behavior "
        "and framing (e.g. 'speaking warmly to camera, natural head nods, soft studio light'). "
        "Audio carries the words — do not write dialogue text."
    ),
    "xai/grok": (
        "Grok Imagine prefers vivid, punchy scene descriptions with strong style keywords and clear subject focus. No @tags."
    ),
}

def _prompt_profile(mid: str) -> str:
    best = ""
    for pref, rules in MODEL_PROMPT_PROFILES.items():
        if mid.startswith(pref) and len(pref) > len(best):
            best = pref
    if best:
        return MODEL_PROMPT_PROFILES[best]
    return ("Write one vivid, professional English prompt: subject, action, camera, lighting, style. "
            "No reference tags — describe subjects in words.")

def _model_uses_tags(mid: str) -> bool:
    return mid.startswith("bytedance/seedance")


async def enhance_user_prompt(idea: str, mid: str, cat: str,
                               res: str = None, asp: str = None, dur: int = None) -> str:
    """User yozgan g'oyani professional inglizcha promptga aylantiradi (Claude Haiku).
    API key yo'q yoki xato bo'lsa — original g'oyani qaytaradi (hech qachon crash qilmaydi)."""
    idea = (idea or "").strip()
    if not idea or not ANTHROPIC_API_KEY:
        return idea
    meta = get_model(mid) or {}
    model_name = meta.get("name", mid)
    settings = f"Model: {model_name}, Type: {cat}, Aspect: {asp or '16:9'}"
    if res: settings += f", Resolution: {res}"
    if dur: settings += f", Duration: {dur}s"
    system = (
        f"You are a professional AI {cat} prompt writer for the '{model_name}' model. "
        f"The user gives a short idea (may be in Uzbek, Russian, or English). "
        f"Rewrite it as ONE detailed, vivid, professional English prompt that this model will execute well. "
        f"Settings: {settings}.\n"
        f"LANGUAGE INTELLIGENCE: the user may write in Uzbek (Latin or Cyrillic, ANY dialect - Tashkent, Fergana, Khorezm, Surkhandarya colloquial), Russian, English, or a MIX, often with typos, slang, missing apostrophes (masalan: korish=ko'rish, urish=urish/urmoq context), phonetic spellings and voice-typing artifacts. Confidently infer the TRUE intent - never take a typo literally if context shows otherwise. STAY INSIDE the user's request: include EVERY subject, object and action they asked for, and ADD NOTHING they did not ask for (no new characters, brands, or plot). Your creative freedom is limited to craft: lighting, camera, composition, mood, quality. "
        f"HOW THIS SPECIFIC MODEL UNDERSTANDS PROMPTS (follow strictly): {_prompt_profile(mid)}\n"
        f"Rules:\n"
        f"1. Keep the user's core intent exactly - every subject, object, and action they mention MUST appear. "
        f"Stay CLOSE to what the user wrote: enhance and detail it, never replace it with a different scene.\n"
        f"2. CRITICAL: reference tags like @image1, @image2, @video1, @audio1 are functional tokens that bind "
        f"uploaded media. If the user's idea contains ANY @tag, every single one MUST appear VERBATIM "
        f"(same spelling, same case) in your rewritten prompt, used naturally in context "
        f"(e.g. 'the person from @image1 performs the motion of @video1'). Never drop, rename, or merge them. "
        f"If the user did NOT use @tags, do not add any.\n"
        f"3. Add cinematic detail: lighting, camera angle, composition, mood, style, quality.\n"
        f"4. Do NOT invent new named people or brands the user didn't mention.\n"
        f"5. Keep it under 150 words, single paragraph.\n"
        f"6. Respond with ONLY the final English prompt - no explanations, no markdown, no quotes, no prefix."
    )
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ENHANCE_MODEL,
        "max_tokens": 400,
        "system": system,
        "messages": [{"role": "user", "content": f"User's idea:\n{idea}\n\nReturn the professional English prompt:"}],
    }
    try:
        async with httpx.AsyncClient(timeout=25) as cl:
            r = await cl.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
            data = r.json()
            if "content" in data and data["content"]:
                raw = data["content"][0]["text"].strip()
                # @teg himoyasi: user teg ishlatgan bo'lsa, natijada hammasi bo'lishi shart
                import re as _re
                _tags = set(_re.findall(r"@(?:image|video|audio)\d+", idea, _re.I))
                if _tags and not all(t.lower() in raw.lower() for t in _tags):
                    print(f"[enhance] @teg yo'qoldi ({_tags}), original qaytarildi")
                    return idea
                lines = raw.split("\n")
                while lines and (lines[0].lstrip().startswith("#") or lines[0].strip() == ""):
                    lines.pop(0)
                cleaned = "\n".join(lines).strip().strip('"').strip()
                return cleaned if cleaned else idea
    except Exception as e:
        print("enhance_user_prompt xato:", e)
    return idea

def _do_generate(u, b):
    """Generatsiya yadrosi — /generate va /tg/generate uchun umumiy."""
    # ── Tool (vosita) — model/prompt/cost vositadan ──
    tool_id = b.get("tool_id")
    if tool_id:
        tl = get_tool(tool_id)
        if not tl:
            return err("Vosita topilmadi")
        b = dict(b)
        b["mid"] = tl["model_id"]
        b["res"] = tl.get("resolution")
        b["asp"] = tl.get("aspect")
        _tp = tl.get("prompt", "")
        # Form maydonlarini promptga quyamiz (KUYOV_NAME, KELIN_NAME, KIMGA_NAME, SANA_VAQT, MANZIL_NAME)
        _fv = b.get("fields") or {}
        _ph = {"kuyov": "KUYOV_NAME", "kelin": "KELIN_NAME", "kimga": "KIMGA_NAME", "sana_vaqt": "SANA_VAQT", "manzil": "MANZIL_NAME"}
        for _k, _tag in _ph.items():
            _val = str(_fv.get(_k, "")).strip()[:120]
            _tp = _tp.replace(_tag, _val)
        b["prompt"] = _tp
        b["_tool_fixed_cost"] = tl.get("cost")
    # ── Shablon (template) — model/res/asp/prompt/cost shablondan ──
    tpl_id = b.get("template_id")
    tpl_style = b.get("style_id")
    tpl_fixed_cost = None
    if tpl_id and tpl_style:
        tp, tmodel, tres, tasp, tcost = build_template_prompt(tpl_id, tpl_style)
        if not tp:
            return err("Shablon topilmadi")
        b = dict(b)
        b["mid"] = tmodel
        b["res"] = tres
        b["asp"] = tasp
        b["prompt"] = tp
        tpl_fixed_cost = tcost
    mid = b.get("mid")
    meta = get_model(mid)
    _is_tool = b.get("_tool_fixed_cost") is not None
    if not meta or (not meta["web"] and not _is_tool):
        return err("Bu model ilovada mavjud emas")
    res = b.get("res"); asp = b.get("asp"); dur = b.get("dur")
    prompt = str(b.get("prompt") or "").strip()[:3500]
    refs = b.get("refs") or []

    if meta["res"] and str(res) not in [str(x) for x in meta["res"]]:
        return err("Sifat qiymati noto'g'ri")
    if meta["asp"] and asp is not None and str(asp) not in meta["asp"] and tpl_fixed_cost is None and not b.get("_tool_fixed_cost"):
        return err("Nisbat qiymati noto'g'ri")
    is_formula = bool(meta.get("price_formula"))
    if meta["type"] == "video" and not is_formula:
        if not meta["dur"]:
            return err("Bu model ilovada mavjud emas")
        if int(dur or 0) not in meta["dur"]:
            return err("Davomiylik qiymati noto'g'ri")
    elif meta["type"] != "video":
        dur = None
    if is_formula:
        if meta.get("price_formula") == "video_sec":
            # Narx manba video uzunligiga bog'liq (video-edit)
            vdur = 0.0
            for rid in refs:
                row = q("SELECT * FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
                if not row:
                    continue
                rk = row["kind"] if "kind" in row.keys() and row["kind"] else _kind_for_ext(Path(row["path"]).suffix.lower())
                if rk != "video":
                    continue
                vdur = float(row["duration"] or 0) if "duration" in row.keys() else 0.0
                if vdur <= 0:
                    vdur = measure_media_duration(row["path"])
                    if vdur > 0:
                        q("UPDATE uploads SET duration=? WHERE id=?", (vdur, rid), commit=True)
                break
            if vdur <= 0:
                return err("Video uzunligi aniqlanmadi — videoni qayta yuklang")
            if vdur > 30.5:
                return err("Video 30 soniyadan oshmasin")
            dur = vdur
        else:
            # Narx audio uzunligiga bog'liq — DB'dan o'lchangan uzunlikni olamiz
            adur = 0.0
            for rid in refs:
                row = q("SELECT kind, duration FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
                if row and row["kind"] == "audio" and row["duration"]:
                    adur = float(row["duration"]); break
            if adur <= 0:
                return err("Audio uzunligi aniqlanmadi — audio qayta yuklang")
            dur = adur
    if not isinstance(refs, list) or len(refs) > meta["refs"]:
        return err("Reference soni ko'p")
    if meta["refs_req"] and len(refs) == 0:
        return err("Bu model uchun kamida 1 ta reference rasm kerak")
    for rid in refs:
        row = q("SELECT id FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
        if not row:
            return err("Reference topilmadi — qayta yuklang")
    if not prompt and len(refs) == 0:
        return err("Tavsif (prompt) yozing")

    if b.get("_tool_fixed_cost") is not None:
        price = b["_tool_fixed_cost"]
    elif tpl_fixed_cost is not None:
        price = tpl_fixed_cost
    else:
        price = price_for(mid, res, dur)
    if price is None:
        return err("Narx aniqlanmadi")

    cur = q("UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?",
            (price, u["id"], price), commit=True)
    if cur.rowcount != 1:
        return err("Balans yetarli emas", 402)

    job_id = uuid.uuid4().hex
    now = int(time.time())
    q("""INSERT INTO jobs(id,user_id,mid,res,asp,dur,prompt,refs_json,price,status,progress,created,updated)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (job_id, u["id"], mid, str(res) if res else None, str(asp) if asp else None,
       int(dur) if dur else None, prompt, json.dumps(refs), price, "queued", 0, now, now), commit=True)
    asyncio.create_task(run_job(job_id))
    nb = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return {"job_id": job_id, "price": price, "balance": nb}

@app.get("/tg/tools")
async def tg_tools(request: Request):
    lang = request.headers.get("X-Lang")
    if not lang:
        try:
            u = await tg_current_user(request)
            lang = _tg_user_lang(u)
        except Exception:
            lang = None
    out = []
    for t in TOOLS_LIST:
        _imgs = [{**im, "label": _tt(im.get("label", ""), lang)} for im in t.get("images", [])]
        _flds = [{**f, "label": _tt(f.get("label", ""), lang)} for f in t.get("fields", [])]
        out.append({
            "id": t["id"], "name": _tt(t["name"], lang), "emoji": t.get("emoji"),
            "desc": _tt(t.get("desc", ""), lang), "cost": t.get("cost"),
            "input": t.get("input", "image"),
            "images": _imgs,
            "fields": _flds,
        })
    return {"tools": out}

@app.get("/tg/templates")
async def tg_templates(request: Request):
    lang = request.headers.get("X-Lang")
    if not lang:
        try:
            u = await tg_current_user(request)
            lang = _tg_user_lang(u)
        except Exception:
            lang = None
    tpls = load_templates()
    out = []
    for t in tpls:
        styles = [{"id": st.get("id"), "label": _tt(st.get("label", st.get("id")), lang)} for st in t.get("styles", [])]
        out.append({
            "id": t.get("id"),
            "name": _tt(t.get("name"), lang),
            "desc": _tt(t.get("desc", ""), lang),
            "model_id": t.get("model_id"),
            "resolution": t.get("resolution"),
            "aspect": t.get("aspect"),
            "cost": t.get("cost"),
            "refs": t.get("refs", 1),
            "styles": styles,
        })
    return {"templates": out}

@app.get("/tg/me")
async def tg_me(request: Request):
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    return tg_user_json(u)

@app.get("/tg/history")
async def tg_history(request: Request, limit: int = 30):
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    limit = max(1, min(int(limit), 60))
    rows = q("SELECT * FROM jobs WHERE user_id=? AND status='done' ORDER BY created DESC LIMIT ?",
             (u["id"], limit))
    out = []
    for j in rows:
        m = get_model(j["mid"]) or {}
        out.append({"id": j["id"], "mid": j["mid"], "type": m.get("type", "image"),
                    "name": m.get("name", j["mid"]), "emoji": m.get("emoji", "✨"),
                    "price": j["price"], "url": j["result_url"]})
    return out

@app.post("/tg/upload")
async def tg_upload(request: Request, file: UploadFile = File(...)):
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    ext = Path(file.filename or "").suffix.lower()
    kind = _kind_for_ext(ext)
    if ext not in ALLOWED_EXT and ext not in ALLOWED_AUDIO and ext not in ALLOWED_VIDEO:
        return err("Rasm, audio yoki video fayl yuklang")
    data = await file.read()
    limit = 50 * 1024 * 1024 if kind == "video" else (20 * 1024 * 1024 if kind == "audio" else 10 * 1024 * 1024)
    if len(data) > limit:
        return err("Fayl juda katta")
    uid = uuid.uuid4().hex
    fpath = UPLOAD_DIR / f"{uid}{ext}"
    with open(fpath, "wb") as f:
        f.write(data)
    dur = 0.0
    if kind == "audio":
        try:
            mf = _MutagenFile(str(fpath))
            if mf is not None and mf.info is not None:
                dur = float(getattr(mf.info, "length", 0) or 0)
        except Exception:
            dur = 0.0
    q("INSERT INTO uploads(id,user_id,path,created,kind,duration) VALUES(?,?,?,?,?,?)",
      (uid, u["id"], str(fpath), int(time.time()), kind, dur), commit=True)
    return {"upload_id": uid, "url": f"/uploads/{uid}{ext}", "kind": kind, "duration": dur}

@app.post("/tg/enhance")
async def tg_enhance(request: Request):
    u = await tg_current_user(request)
    b = await request.json()
    idea = str(b.get("prompt") or "").strip()[:2000]
    mid = b.get("mid") or ""
    cat = "video" if (get_model(mid) or {}).get("type") == "video" else "image"
    if not idea:
        return err("Avval g'oyangizni yozing")
    # Reference biriktirilgan bo'lsa — Claude rasm/kadrlarni KO'RIB prompt yozadi
    ref_ids = b.get("refs") or []
    _tm = get_model(mid) or {}
    if _tm.get("refs_req") and (_tm.get("refs") or 0) > 0 and not ref_ids:
        return err("Avval reference yuklang — prompt reference asosida yoziladi")
    if u and ref_ids and ANTHROPIC_API_KEY:
        media = []
        for rid in ref_ids[:3]:
            row = q("SELECT * FROM uploads WHERE id=? AND user_id=?", (rid, u["id"]), one=True)
            if not row:
                continue
            p = Path(row["path"])
            rk = row["kind"] if "kind" in row.keys() and row["kind"] else _kind_for_ext(p.suffix.lower())
            if rk == "audio":
                continue
            b64 = _ref_to_jpeg_b64(p, rk)
            if b64:
                media.append((b64, rk))
        if media:
            try:
                mname = (get_model(mid) or {}).get("name", "AI")
                result = await _vision_enhance(idea, media, mname, cat,
                                               str(b.get("asp")) if b.get("asp") else None,
                                               int(b.get("dur")) if b.get("dur") else None, None, mid=mid or "")
                return {"prompt": result}
            except Exception as e:
                print(f"[tg-enhance-vision] xato, oddiy yo'l: {e}")
    enhanced = await enhance_user_prompt(idea, mid, cat, b.get("res"), b.get("asp"), b.get("dur"))
    return {"prompt": enhanced}

@app.post("/tg/generate")
async def tg_generate(request: Request):
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    if u["banned"] if "banned" in u.keys() else 0:
        return err("Hisobingiz bloklangan")
    b = await request.json()
    return _do_generate(u, b)

@app.get("/tg/job/{job_id}")
async def tg_job(job_id: str, request: Request):
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    j = q("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, u["id"]), one=True)
    if not j:
        return err("Topilmadi", 404)
    out = {"status": j["status"], "progress": j["progress"], "result_url": j["result_url"], "error": friendly_error(j["error"]) if j["status"] == "failed" else j["error"]}
    if j["status"] in ("failed", "done"):
        out["balance"] = q("SELECT balance FROM users WHERE id=?", (u["id"],), one=True)["balance"]
    return out

@app.get("/tg/packages")
async def tg_packages(request: Request):
    """Bot/Mini App uchun tangacha paketlari (Payme/Click narxlari)."""
    out = []
    for i, p in enumerate(PACKAGES):
        out.append({"idx": i, "tangacha": p["t"], "bonus": p["b"], "uzs": p["uzs"]})
    return {"packages": out}

@app.post("/tg/pay/create")
async def tg_pay_create(request: Request):
    """Bot/Mini App: Telegram foydalanuvchisi uchun Payme/Click to'lov linki.
    Order voro.uz bilan bir xil bazada — to'langach webhook balansni oshiradi."""
    u = await tg_current_user(request)
    if not u:
        return err("Telegram tekshiruvi o'tmadi", 401)
    b = await request.json()
    try:
        pkg = PACKAGES[int(b.get("pkg"))]
    except Exception:
        return err("Paket noto'g'ri")
    method = b.get("method")
    if method not in ("payme", "click"):
        return err("To'lov usuli noto'g'ri")
    if method == "payme" and not PAYME_MERCHANT_ID:
        return err("Payme hali sozlanmagan", 503)
    if method == "click" and not (CLICK_SERVICE_ID and CLICK_MERCHANT_ID):
        return err("Click hali sozlanmagan", 503)

    cur = q("INSERT INTO orders(user_id,tangacha,bonus,amount_uzs,method,status,created) VALUES(?,?,?,?,?,?,?)",
            (u["id"], pkg["t"], pkg["b"], pkg["uzs"], method, "new", int(time.time())), commit=True)
    order_id = cur.lastrowid

    if method == "payme":
        raw = f"m={PAYME_MERCHANT_ID};ac.order_id={order_id};a={pkg['uzs'] * 100}"
        link = "https://checkout.paycom.uz/" + base64.b64encode(raw.encode()).decode()
        return {"url": link, "order_id": order_id}
    link = ("https://my.click.uz/services/pay"
            f"?service_id={CLICK_SERVICE_ID}&merchant_id={CLICK_MERCHANT_ID}"
            f"&amount={pkg['uzs']}&transaction_param={order_id}"
            f"&return_url={PUBLIC_BASE_URL}/")
    return {"url": link, "order_id": order_id}

# ────────────────────────── LOKAL ISHGA TUSHIRISH ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8788)
