# -*- coding: utf-8 -*-
"""Deploy'dan keyingi jonli tekshiruv (VPS'da):
    VORO_BOT_SECRET=... python3 smoke_test.py [uid]
"""
import hashlib, hmac, json, os, sys, time, urllib.request

BASE = os.environ.get("VAGENT_BASE", "http://127.0.0.1:8788")
SECRET = os.environ.get("VORO_BOT_SECRET", "")
UID = sys.argv[1] if len(sys.argv) > 1 else "1"

exp = str(int(time.time()) + 300)
sig = hmac.new(SECRET.encode(), f"{UID}:{exp}".encode(), hashlib.sha256).hexdigest()

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

h = get("/vagent/health")
print("health:", json.dumps(h, ensure_ascii=False))
assert h.get("ok"), "❌ health: kalitlardan biri yo'q!"

me = get(f"/vagent/me?uid={UID}&exp={exp}&sig={sig}")
print("me:", json.dumps(me, ensure_ascii=False))
assert me.get("ok"), "❌ auth ishlamadi — BOT_SECRET bot bilan bir xilmi?"

bad = get(f"/vagent/me?uid={UID}&exp={exp}&sig=notogri")
assert not bad.get("ok"), "❌ XAVFLI: noto'g'ri imzo qabul qilindi!"

print("\n✅ SMOKE TEST O'TDI — Vagent jonli va himoyalangan.")
