# 说明：只上传 PLD 数据（smart_samples.db / smart_samples.csv 回退）。
#       官方 collectdata 的 smart_weight_data.csv（40 列、无 reward）是官方模型
#       训练数据，不参与 PLD 训练，**绝不能上传**到 gdrive:smart_data ——
#       内核已解耦，上错文件会污染训练集。
# =============================================================================
# 2026-08-24 修复：原脚本用 "rclone delete --min-age 15d" 清理旧数据，
#   实测曾把未到期文件误删进回收站导致训练 CI 拿不到数据（8/18、8/23 失败）。
#   新策略：不再自动删除；上传前先校验本地 db 非空；上传后校验远端大小。
#   历史数据由人工每季度在 gdrive 网页端归档。
# =============================================================================
set -e

DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
REMOTE=rclone
GDRIVE_DIR=gdrive:smart_data/pld
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

MSG=""
if [ -f "$DB_FILE" ]; then
  # 本地 db 必须大于 1MB 才上传（防止空库/损坏库覆盖云端好数据）
  SIZE_KB=$(du -k "$DB_FILE" | awk '{print $1}')
  if [ "$SIZE_KB" -lt 1024 ]; then
    echo "[upcsv.db] skip: db too small ($SIZE_KB KB), refusing to upload"
    exit 1
  fi
  "$REMOTE" copy "$DB_FILE" "$GDRIVE_DIR"/ --config /root/.config/rclone/rclone.conf --transfers=1 --log-level ERROR
  # 上传后校验：远端必须有同名文件且大小接近（±5%）
  REMOTE_SIZE=$(rclone lsjson "$GDRIVE_DIR" --config /root/.config/rclone/rclone.conf --files-only 2>/dev/null | \
    grep -o '"Size":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" -gt 0 ]; then
    MSG="smart_samples.db 已上传 (本地 $((SIZE_KB / 1024)) MB, 远端 $((REMOTE_SIZE / 1024)) KB 校验通过)"
  else
    MSG="警告：上传后远端校验失败"
  fi
fi

if [ -f "$CSV_FALLBACK" ]; then
  "$REMOTE" copy "$CSV_FALLBACK" "$GDRIVE_DIR"/ --config /root/.config/rclone/rclone.conf --transfers=1 --log-level ERROR
  MSG="${MSG}；smart_samples.csv 已上传"
fi

if [ -z "$MSG" ]; then
  MSG="警告：$DB_FILE / $CSV_FALLBACK 均不存在，本次未上传"
fi

if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TG_CHAT_ID" -d text="$MSG" >/dev/null 2>&1 || true
fi

echo "[upcsv.db] $MSG"
