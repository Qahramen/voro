#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voro_web_api.py — Seedance/video natija YO'QOLISH bug fix (pul yo'qotmaslik).
Sabab: (1) atlas_poll try/except'siz -> bitta transient 502/HTML poll -> job FAILED
(Atlas esa tugatadi). (2) run_job fail'dan oldin Atlas'ni oxirgi tekshirmaydi.
(3) crash-recovery restart'da in-progress jobni Atlas'ni tekshirmasdan fail+refund.
Yechim: poll bardoshli + safety-net + crash-recovery RESUME. Xavfsiz: backup+py_compile."""
import time, shutil, py_compile, sys

PATH = "/opt/voro-web/voro_web_api.py"
s = open(PATH, encoding="utf-8").read()

if "_finish_job" in s:
    print("SKIP: allaqachon qo'llanilgan"); sys.exit(0)

bak = f"{PATH}.bak-jobfix-{int(time.time())}"
shutil.copy(PATH, bak); print("Zaxira:", bak)

# ─── 1) atlas_poll: HTTP+json ni try/except (transient -> processing) ───────────
old_poll = (
    "    async with httpx.AsyncClient(timeout=30) as cl:\n"
    "        r = await cl.get(url, headers=headers)\n"
    "        data = r.json()\n"
    "\n"
    "    st = str((data.get(\"data\") or {}).get(\"status\", \"\"))"
)
new_poll = (
    "    try:\n"
    "        async with httpx.AsyncClient(timeout=30) as cl:\n"
    "            r = await cl.get(url, headers=headers)\n"
    "        data = r.json()\n"
    "    except Exception:\n"
    "        # TRANSIENT (502/503/HTML/timeout) — poll'ni TO'XTATMAYMIZ, job o'lmasin.\n"
    "        # Atlas ishlashda davom etadi; keyingi poll ushlaydi.\n"
    "        return {\"status\": \"processing\"}\n"
    "\n"
    "    st = str((data.get(\"data\") or {}).get(\"status\", \"\"))"
)
assert s.count(old_poll) == 1, f"atlas_poll anchor soni {s.count(old_poll)}"
s = s.replace(old_poll, new_poll, 1)
print("1) atlas_poll bardoshli (transient -> processing)")

# ─── 2) run_job -> _finish_job (safety-net bilan) + slim run_job ────────────────
old_runjob = '''async def run_job(job_id):
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
        set_job(job_id, status="failed", error=str(e)[:200])'''

new_runjob = '''async def _download_retry(job_id, url, jtype, tries=3):
    """Yuklab olishni bir necha marta urinadi (transient tarmoq xatosi natijani yo'qotmasin)."""
    last = None
    for i in range(tries):
        try:
            return await download_result(job_id, url, jtype)
        except Exception as e:
            last = e
            await asyncio.sleep(3 + i * 3)
    raise last


async def _finish_job(job_id, atlas_id, user_id, price, mid):
    """atlas_id ni poll qilib natijani yetkazadi (yoki fail+refund).
    MUHIM: fail'dan OLDIN Atlas'ni oxirgi marta tekshiradi — Atlas tugatgan bo'lsa
    natijani BERAMIZ (transient poll xatosi/restart tufayli pul+natija yo'qolmasin).
    run_job VA crash-recovery resume shuni ishlatadi."""
    jtype = (get_model(mid) or {}).get("type", "video")
    deadline = time.time() + 900  # maks 15 daqiqa
    prog = 15
    try:
        while time.time() < deadline:
            await asyncio.sleep(4)
            st = await atlas_poll(atlas_id)
            if st["status"] == "done":
                if not st.get("output_url"):
                    raise RuntimeError("Natija URL topilmadi")
                set_job(job_id, progress=95)
                local = await _download_retry(job_id, st["output_url"], jtype)
                set_job(job_id, status="done", progress=100, result_url=local)
                return
            if st["status"] == "failed":
                raise RuntimeError(st.get("error") or "Generatsiya amalga oshmadi")
            prog = min(90, prog + 3)
            set_job(job_id, progress=prog)
        raise TimeoutError("Vaqt tugadi (15 daqiqa)")
    except Exception as e:
        # SAFETY NET: fail'dan oldin Atlas'ni oxirgi marta tekshiramiz.
        # Agar Atlas tugatgan bo'lsa — natijani beramiz (pul+natija yo'qolmasin).
        for _ in range(6):
            try:
                st = await atlas_poll(atlas_id)
                if st.get("status") == "done" and st.get("output_url"):
                    local = await _download_retry(job_id, st["output_url"], jtype)
                    set_job(job_id, status="done", progress=100, result_url=local)
                    return
                if st.get("status") == "failed":
                    break
            except Exception:
                pass
            await asyncio.sleep(5)
        refund(user_id, price)
        set_job(job_id, status="failed", error=str(e)[:200])


async def run_job(job_id):
    job = q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    if not job:
        return
    try:
        set_job(job_id, status="processing", progress=8)
        atlas_id = await atlas_submit(job)
        set_job(job_id, atlas_id=atlas_id, progress=15)
    except Exception as e:
        refund(job["user_id"], job["price"])
        set_job(job_id, status="failed", error=str(e)[:200])
        return
    await _finish_job(job_id, atlas_id, job["user_id"], job["price"], job["mid"])'''

assert s.count(old_runjob) == 1, f"run_job anchor soni {s.count(old_runjob)}"
s = s.replace(old_runjob, new_runjob, 1)
print("2) run_job -> _finish_job + safety-net + download retry")

# ─── 3) crash-recovery: atlas_id bor stuck jobni RESUME (fail emas) ─────────────
old_cr = '''    stuck = q("SELECT id, user_id, price FROM jobs WHERE status IN ('queued','processing')")
    for j in stuck:
        refund(j["user_id"], j["price"])
        set_job(j["id"], status="failed", error="Server qayta ishga tushdi — tangacha qaytarildi")'''
new_cr = '''    stuck = q("SELECT id, user_id, price, atlas_id, mid FROM jobs WHERE status IN ('queued','processing')")
    for j in stuck:
        _aid = None
        try:
            _aid = j["atlas_id"]
        except Exception:
            _aid = None
        if _aid:
            # Atlas'da DAVOM etyapti — fail EMAS, RESUME (poll qilib natijani yetkazamiz).
            # Restart natijani yo'qotmasin; Atlas tugatgan bo'lsa user oladi.
            asyncio.create_task(_finish_job(j["id"], _aid, j["user_id"], j["price"], j["mid"]))
        else:
            # hech submit qilinmagan (atlas_id yo'q) — fail+refund
            refund(j["user_id"], j["price"])
            set_job(j["id"], status="failed", error="Server qayta ishga tushdi — tangacha qaytarildi")'''
assert s.count(old_cr) == 1, f"crash-recovery anchor soni {s.count(old_cr)}"
s = s.replace(old_cr, new_cr, 1)
print("3) crash-recovery: atlas_id bor jobni RESUME qiladi")

open(PATH, "w", encoding="utf-8").write(s)
py_compile.compile(PATH, doraise=True)
print("PATCH OK — py_compile o'tdi. voro_web restart kerak.")
