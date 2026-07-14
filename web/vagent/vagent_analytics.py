# -*- coding: utf-8 -*-
"""
VAGENT ANALYTICS — o'lchov dvigateli
=====================================
Har bir muhim voqea append-only log'ga yoziladi (JSONL).
Bu Vagent'ni yillar davomida yaxshilashning asosi: voronka qayerda
uzilayotganini, qaysi xato ko'payganini, daromad qanday o'sayotganini
ko'rsatadi.

Voqealar (funnel tartibida):
  msg           — foydalanuvchi xabar yubordi
  quote_shown   — narx kartasi ko'rsatildi        {total}
  confirmed     — foydalanuvchi tasdiqladi         {total}
  declined      — foydalanuvchi rad etdi           {total}
  gen_ok        — generatsiya muvaffaqiyatli       {price, model, kind}
  gen_fail      — generatsiya xato (refund)        {price, model, error}
  voice         — ovozli buyruq ishlatildi
  upload        — referens rasm yuklandi
"""

import json
import os
import time
import fcntl
from collections import Counter
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("VORO_DATA_DIR", "/opt/voro")
EVENTS_LOG = os.path.join(DATA_DIR, "vagent_events.jsonl")


def track(uid: str, event: str, props: dict | None = None) -> None:
    """Voqeani yozish — hech qachon xato bilan asosiy oqimni to'xtatmaydi."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        line = json.dumps({"ts": int(time.time()), "uid": str(uid),
                           "ev": event, **(props or {})}, ensure_ascii=False)
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass  # analitika hech qachon mahsulotni buzmasin


def _read_events(days: int) -> list[dict]:
    if not os.path.exists(EVENTS_LOG):
        return []
    cutoff = time.time() - days * 86400
    out = []
    with open(EVENTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                if e.get("ts", 0) >= cutoff:
                    out.append(e)
            except Exception:
                continue
    return out


def compute_stats(days: int = 7) -> dict:
    """Voronka, daromad, xatolar — oxirgi N kun."""
    evs = _read_events(days)
    users = {e["uid"] for e in evs}
    c = Counter(e["ev"] for e in evs)

    spent = sum(e.get("price", 0) for e in evs if e["ev"] == "gen_ok")
    refunded = sum(e.get("price", 0) for e in evs if e["ev"] == "gen_fail")
    models = Counter(e.get("model", "?") for e in evs if e["ev"] == "gen_ok")
    errors = Counter((e.get("error") or "?")[:60] for e in evs if e["ev"] == "gen_fail")

    quotes = c["quote_shown"] or 1
    return {
        "davr_kun": days,
        "faol_foydalanuvchilar": len(users),
        "xabarlar": c["msg"],
        "voronka": {
            "narx_korsatildi": c["quote_shown"],
            "tasdiqlandi": c["confirmed"],
            "rad_etildi": c["declined"],
            "tasdiq_foizi": round(100 * c["confirmed"] / quotes, 1),
        },
        "generatsiya": {
            "muvaffaqiyatli": c["gen_ok"],
            "xato": c["gen_fail"],
            "sarflangan_tangacha": spent,
            "qaytarilgan_tangacha": refunded,
        },
        "ovoz_ishlatilishi": c["voice"],
        "rasm_yuklashlar": c["upload"],
        "top_modellar": dict(models.most_common(5)),
        "top_xatolar": dict(errors.most_common(5)),
    }


def daily_digest_text() -> str:
    """Egaga yuboriladigan kunlik o'zbekcha hisobot matni.
    Bot har kuni ertalab GET /vagent/admin/digest dan olib,
    Qahramonga Telegram orqali yuborishi mumkin."""
    s = compute_stats(1)
    w = compute_stats(7)
    v = s["voronka"]; g = s["generatsiya"]

    lines = [
        "📊 VAGENT — kunlik hisobot",
        f"👥 Faol: {s['faol_foydalanuvchilar']} kishi | ✉️ {s['xabarlar']} xabar",
        f"💳 Voronka: {v['narx_korsatildi']} taklif → {v['tasdiqlandi']} tasdiq ({v['tasdiq_foizi']}%)",
        f"🪙 Sarflandi: {g['sarflangan_tangacha']} tangacha"
        + (f" | ⚠️ qaytarildi: {g['qaytarilgan_tangacha']}" if g['qaytarilgan_tangacha'] else ""),
        f"🎬 Generatsiya: {g['muvaffaqiyatli']} ✓ / {g['xato']} ✗",
    ]
    if s["top_xatolar"]:
        top_err = next(iter(s["top_xatolar"]))
        lines.append(f"🔥 Eng ko'p xato: {top_err}")
    if v["narx_korsatildi"] >= 5 and v["tasdiq_foizi"] < 40:
        lines.append("💡 Signal: tasdiq foizi past — narxlar yoki taklif uslubini ko'rib chiq.")
    lines.append(f"7 kunlik: {w['faol_foydalanuvchilar']} faol, "
                 f"{w['generatsiya']['sarflangan_tangacha']} tangacha aylanma")
    return "\n".join(lines)
