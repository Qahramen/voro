# -*- coding: utf-8 -*-
"""
VAGENT TEST TO'PLAMI — deploy'dan oldin har safar yuritiladi:
    cd /opt/voro-web && python3 -m pytest test_vagent.py -q
Hammasi yashil bo'lmasa — deploy QILINMAYDI.
"""

import json
import os
import time
import hmac
import hashlib
import tempfile

# Test muhiti — real ma'lumotlarga tegmaydi
_TMP = tempfile.mkdtemp(prefix="vagent_test_")
os.environ["VORO_DATA_DIR"] = _TMP
os.environ["VORO_BOT_SECRET"] = "test_secret_key_123"

import vagent_api as V  # noqa: E402
import vagent_analytics as A  # noqa: E402


def _sig(uid, exp):
    return hmac.new(b"test_secret_key_123", f"{uid}:{exp}".encode(),
                    hashlib.sha256).hexdigest()


# ---------------- AUTH ----------------

def test_auth_togri_imzo():
    exp = str(int(time.time()) + 600)
    assert V.verify_auth("42", exp, _sig("42", exp)) is True


def test_auth_muddati_otgan():
    exp = str(int(time.time()) - 10)
    assert V.verify_auth("42", exp, _sig("42", exp)) is False


def test_auth_buzilgan_imzo():
    exp = str(int(time.time()) + 600)
    assert V.verify_auth("42", exp, _sig("43", exp)) is False
    assert V.verify_auth("42", exp, "abc") is False


# ---------------- NARX ----------------

def test_narx_rasm():
    assert V.calc_price("image", "nano-banana-2") == 2
    assert V.calc_price("image", "gpt-image-2") == 5


def test_narx_video_resolution():
    assert V.calc_price("video", "seedance-2.0", "480p", 5) == 20
    assert V.calc_price("video", "seedance-2.0", "720p", 5) == 30
    assert V.calc_price("video", "seedance-2.0", "1080p", 10) == 100


def test_narx_notogri_model():
    assert V.calc_price("video", "yoq-model") is None
    assert V.calc_price("video", "seedance-2.0", "4K") is None


def test_hot_reload_config():
    """vagent_models.json o'zgarsa narx deploysiz yangilanadi."""
    cfg = {"pricing": {"image": {"nano-banana-2": {"base": 99}},
                       "video": {}},
           "hints": {"nano-banana-2": "test"}}
    with open(V.MODELS_JSON, "w") as f:
        json.dump(cfg, f)
    os.utime(V.MODELS_JSON, (time.time() + 5, time.time() + 5))
    assert V.calc_price("image", "nano-banana-2") == 99
    os.remove(V.MODELS_JSON)
    V._models_cache["mtime"] = 0  # keshni tozalash -> fallback qaytadi
    V._models_cache["data"] = V._FALLBACK_MODELS
    assert V.calc_price("image", "nano-banana-2") == 2


# ---------------- BALANS ----------------

def test_balans_yechish_qaytarish():
    json.dump({"7": {"credits": 10}}, open(V.USERS_JSON, "w"))
    assert V.get_balance("7") == 10
    assert V.deduct_credits("7", 4) is True
    assert V.get_balance("7") == 6
    assert V.deduct_credits("7", 100) is False   # yetmasa yechmaydi
    assert V.get_balance("7") == 6
    V.refund_credits("7", 4)
    assert V.get_balance("7") == 10


def test_balans_tangacha_kaliti():
    """Eski users.json 'tangacha' kalitida bo'lsa ham ishlaydi."""
    json.dump({"8": {"tangacha": 5}}, open(V.USERS_JSON, "w"))
    assert V.get_balance("8") == 5
    V.deduct_credits("8", 2)
    assert json.load(open(V.USERS_JSON))["8"]["tangacha"] == 3


# ---------------- XOTIRA ----------------

def test_xotira_fakt_va_takror():
    V.memory_add_fact("9", "Ismi Aziz")
    V.memory_add_fact("9", "Ismi Aziz")     # takror yozilmaydi
    V.memory_add_fact("9", "Do'koni bor")
    m = V.memory_get("9")
    assert m["facts"].count("Ismi Aziz") == 1
    assert len(m["facts"]) == 2


def test_xotira_tarix_limit():
    for i in range(30):
        V.memory_log_job("9", {"label": f"ish {i}"})
    assert len(V.memory_get("9")["history"]) == 25   # oxirgi 25 tasi


# ---------------- INBOX (crash-recovery) ----------------

def test_inbox_since_filtri():
    V.inbox_push("11", {"label": "eski", "kind": "image", "url": "u", "price": 1})
    mid = int(time.time())
    time.sleep(1.1)
    V.inbox_push("11", {"label": "yangi", "kind": "image", "url": "u", "price": 1})
    items = V.inbox_pull("11", since_ts=mid)
    assert len(items) == 1 and items[0]["label"] == "yangi"


# ---------------- RETSEPTLAR ----------------

def test_skills_qidiruv_mavjud():
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vagent_skills.json")
    if os.path.exists(src):
        import shutil
        shutil.copy(src, V.SKILLS_JSON)
        skills = json.load(open(V.SKILLS_JSON))
        assert len(skills) >= 12, "kamida 12 retsept bo'lishi kerak"


# ---------------- RATE LIMIT ----------------

def test_rate_limit():
    uid = "spam_user"
    oks = sum(1 for _ in range(20) if V.rate_ok(uid))
    assert oks == 15                       # 15 tadan keyin to'xtaydi
    V._RATE[uid].clear()
    assert V.rate_ok(uid) is True          # oyna tozalansa yana ishlaydi


# ---------------- ANALITIKA ----------------

def test_analitika_voronka():
    A.track("21", "msg")
    A.track("21", "quote_shown", {"total": 10})
    A.track("21", "confirmed", {"total": 10})
    A.track("21", "gen_ok", {"price": 10, "model": "m", "kind": "image"})
    s = A.compute_stats(1)
    assert s["voronka"]["tasdiqlandi"] >= 1
    assert s["generatsiya"]["sarflangan_tangacha"] >= 10
    assert "VAGENT" in A.daily_digest_text()
