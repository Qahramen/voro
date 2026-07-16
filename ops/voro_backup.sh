#!/bin/bash
# ==========================================================================
# VORO KUNLIK ZAXIRA (backup)
# Eng muhim: voro_web.db (web user tangalar = PUL) + /root/bot/users.json (bot
# tangalar) + vagent shardlar (chat/xotira/inbox) + analitika. Kuniga 1 marta
# cron bilan; oxirgi 7 nusxa saqlanadi. Local (bir disk) — 1-qatlam himoya
# (bug/o'chirish/buzilishдан). Disk-buzilish uchun off-site alohida qo'shiladi.
# ==========================================================================
set -e
BDIR=/opt/voro_backups
mkdir -p "$BDIR"
TS=$(date +%Y%m%d_%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 1) SQLite XAVFSIZ snapshot (yozilayotgan bo'lsa ham izchil) — .backup, cp zaxira
sqlite3 /opt/voro-web/voro_web.db ".backup '$TMP/voro_web.db'" 2>/dev/null \
  || cp /opt/voro-web/voro_web.db "$TMP/voro_web.db" 2>/dev/null || true

# 2) bot balanslari (PUL)
[ -f /root/bot/users.json ] && cp /root/bot/users.json "$TMP/bot_users.json" || true

# 3) vagent ma'lumoti (per-user shardlar + analitika + eski yagona fayllar)
for p in vagent_memory_d vagent_chats_d vagent_inbox_d \
         vagent_events.jsonl vagent_memory.json vagent_chats.json vagent_inbox.json \
         vagent_models.json vagent_skills.json; do
  [ -e "/opt/voro/$p" ] && cp -r "/opt/voro/$p" "$TMP/" 2>/dev/null || true
done

# 4) arxiv (siqilgan)
OUT="$BDIR/voro_backup_$TS.tar.gz"
tar -czf "$OUT" -C "$TMP" --exclude='*.lock' . 2>/dev/null

# 5) rotatsiya: faqat oxirgi 7 nusxa qoladi
ls -1t "$BDIR"/voro_backup_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "OK: $(basename "$OUT") ($(du -h "$OUT" | cut -f1)) | jami $(ls -1 "$BDIR"/voro_backup_*.tar.gz 2>/dev/null | wc -l) nusxa"
