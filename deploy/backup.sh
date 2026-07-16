#!/usr/bin/env bash
# 账本每日备份：sqlite 在线 backup（WAL 安全）到 data/backups/，保留最近 14 份。
# 设了 BACKUP_GCS_URI（如 gs://my-bucket/polycopycat）且机器有 gcloud 时，再传一份到 GCS。
# 由 polycopycat-backup.timer 每日触发（见 setup.sh），手动跑也行：
#   ~/polyCopyCat/deploy/backup.sh [账本路径]
set -euo pipefail

APP_DIR="${POLYCOPYCAT_DIR:-$HOME/polyCopyCat}"
LEDGER="${1:-$APP_DIR/data/paper-week.sqlite3}"
DEST_DIR="$APP_DIR/data/backups"
KEEP=14

if [ ! -f "$LEDGER" ]; then
    echo "账本不存在，跳过备份: $LEDGER"
    exit 0
fi
mkdir -p "$DEST_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST_DIR/$(basename "$LEDGER" .sqlite3)-$STAMP.sqlite3"

# 用 sqlite 的在线 backup API（引擎不用停，WAL 模式安全）
"$APP_DIR/.venv/bin/python" - "$LEDGER" "$OUT" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
    a.backup(b)
print(f"备份完成: {dst}")
PY

# 轮转：只留最近 KEEP 份
ls -1t "$DEST_DIR"/*.sqlite3 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

# 可选异地副本
if [ -n "${BACKUP_GCS_URI:-}" ] && command -v gcloud >/dev/null 2>&1; then
    gcloud storage cp "$OUT" "${BACKUP_GCS_URI%/}/" && echo "已上传: ${BACKUP_GCS_URI%/}/$(basename "$OUT")"
fi
