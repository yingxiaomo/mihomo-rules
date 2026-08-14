# 说明：只上传 PLD 数据（smart_samples.db / smart_samples.csv 回退）。
#       官方 collectdata 的 smart_weight_data.csv（40 列、无 reward）是官方模型
#       训练数据，不参与 PLD 训练，**绝不能上传**到 gdrive:smart_data ——
#       内核已解耦，上错文件会污染训练集。
# =============================================================================
set -e

# 内核 HomeDir（ImmortalWrt nikki）—— 与内核 C.Path.HomeDir() 一致
DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
REMOTE=rclone
GDRIVE_DIR=gdrive:smart_data/pld   # PLD 数据独立分区（official/ 预留官方数据）
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

# 云端保留两周期（15 天），更早的 PLD 切片（db + csv 回退）删除
"$REMOTE" delete "$GDRIVE_DIR" --min-age 15d --include 'smart_samples*' --log-level ERROR 2>/dev/null || true

MSG=""
if [ -f "$DB_FILE" ]; then
  SIZE_KB=$(du -k "$DB_FILE" | awk '{print $1}')
  "$REMOTE" copy "$DB_FILE" "$GDRIVE_DIR"/ --log-level ERROR
  MSG="smart_samples.db 已上传 ($((SIZE_KB / 1024)) MB)"
fi

# SQLite 不可用的架构（mips 等）由内核回退写 smart_samples.csv
if [ -f "$CSV_FALLBACK" ]; then
  "$REMOTE" copy "$CSV_FALLBACK" "$GDRIVE_DIR"/ --log-level ERROR
  MSG="${MSG}；smart_samples.csv 已上传"
fi

if [ -z "$MSG" ]; then
  MSG="警告：$DB_FILE / $CSV_FALLBACK 均不存在，本次未上传"
fi

# Telegram 通知
if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TG_CHAT_ID" -d text="$MSG" >/dev/null 2>&1 || true
fi

echo "[upcsv.db] $MSG"

