# 说明：只上传 PLD 数据（smart_samples.db / smart_samples.csv 回退）。
#       官方 collectdata 的 smart_weight_data.csv（40 列、无 reward）是官方模型
#       训练数据，不参与 PLD 训练，**绝不能上传**到 gdrive:smart_data。
#
# 2026-08-24 v3：SQLite 活跃库直接 rclone 会因 "source file is being updated"
#   被拒传（内核持续写样本），这就是 8/18、8/23 训练 CI 失败的根因。
#   新流程：cp 快照 → 校验快照大小 → 上传快照 → 远端校验。
#   不再自动 delete 云端文件（曾误删未到期数据进回收站）。
# =============================================================================
set -e

DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
SNAPSHOT="/tmp/smart_snapshot.db"
REMOTE=rclone
RCONF=/root/.config/rclone/rclone.conf
GDRIVE_DIR=gdrive:smart_data/pld
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

MSG=""
if [ -f "$DB_FILE" ]; then
  SIZE_KB=$(du -k "$DB_FILE" | awk '{print $1}')
  if [ "$SIZE_KB" -lt 1024 ]; then
    echo "[upcsv.db] skip: db too small ($SIZE_KB KB)"
    exit 1
  fi
  # 一致性快照：绕开 "source file is being updated" 拒传
  cp "$DB_FILE" "$SNAPSHOT"
  SNAP_KB=$(du -k "$SNAPSHOT" | awk '{print $1}')
  "$REMOTE" copy "$SNAPSHOT" "$GDRIVE_DIR"/ --config "$RCONF" --transfers=1 --log-level ERROR
  REMOTE_SIZE=$(rclone lsjson "$GDRIVE_DIR/smart_snapshot.db" --config "$RCONF" --files-only 2>/dev/null | grep -o '"Size":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" -gt 0 ]; then
    MSG="smart_samples.db 已上传 (本地 $((SIZE_KB/1024)) MB, 快照 $((SNAP_KB/1024)) MB, 远端校验通过)"
    rm -f "$SNAPSHOT"
  else
    MSG="警告：上传后远端校验失败"
    exit 1
  fi
fi

if [ -f "$CSV_FALLBACK" ]; then
  "$REMOTE" copy "$CSV_FALLBACK" "$GDRIVE_DIR"/ --config "$RCONF" --transfers=1 --log-level ERROR
  MSG="${MSG}；smart_samples.csv 已上传"
fi

if [ -z "$MSG" ]; then
  MSG="警告：$DB_FILE / $CSV_FALLBACK 均不存在"
fi

if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TG_CHAT_ID" -d text="$MSG" >/dev/null 2>&1 || true
fi

echo "[upcsv.db] $MSG"
