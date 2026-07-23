#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot native Vagent'ni WEB YADROga (vagent_api) ulaydi — vagent_bot_bridge orqali.
Eski _vagent_call_claude/_vagent_run_tool/vagent_handler O'RNIGA web yadroni chaqiradi.
Xavfsiz: backup + anchor assert + py_compile. Ishlatgach botni restart qiling."""
import shutil, time, py_compile, sys

PATH     = "/root/bot/voro_creator_bot.py"
HANDLERS = "/root/bot/vagent_bridge_handlers.py"

s = open(PATH, encoding="utf-8").read()
if "vagent_bot_bridge" in s or "_vagent_handler_legacy" in s:
    print("SKIP: allaqachon qo'llanilgan")
    sys.exit(0)

bak = f"{PATH}.eski-vgbridge-{int(time.time())}"
shutil.copy(PATH, bak)
print("Zaxira:", bak)

new_funcs = open(HANDLERS, encoding="utf-8").read()

# ─── 1) eski vagent_handler → _vagent_handler_legacy, oldiga yangi kod ──────────
old_def = "async def vagent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:"
n = s.count(old_def)
assert n == 1, f"vagent_handler def soni {n} (1 kutilgan) — to'xtatildi"
legacy_def = "async def _vagent_handler_legacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:"
s = s.replace(old_def, new_funcs + "\n\n" + legacy_def, 1)
print("1) vagent_handler → legacy + yangi kod kiritildi")

# ─── 2) VAGENT_MODE state'ga vg: callback handler ─────────────────────────────
state_anchor = (
    "                MessageHandler(filters.TEXT & ~filters.COMMAND, vagent_handler),\n"
    "                CallbackQueryHandler(on_button),\n"
    "            ],"
)
n2 = s.count(state_anchor)
assert n2 == 1, f"VAGENT_MODE state anchor soni {n2} (1 kutilgan) — to'xtatildi"
state_new = (
    "                MessageHandler(filters.TEXT & ~filters.COMMAND, vagent_handler),\n"
    "                CallbackQueryHandler(vagent_cb_handler, pattern=\"^vg:\"),\n"
    "                CallbackQueryHandler(on_button),\n"
    "            ],"
)
s = s.replace(state_anchor, state_new, 1)
print("2) VAGENT_MODE state'ga vg: callback handler qo'shildi")

open(PATH, "w", encoding="utf-8").write(s)
py_compile.compile(PATH, doraise=True)
print("PATCH OK — py_compile o'tdi. Botni restart qiling.")
