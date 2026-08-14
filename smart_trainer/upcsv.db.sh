#!/bin/sh
# =============================================================================
# upcsv.db.sh —— smart 训练数据收割脚本（SQLite 版）
#
# 用途：替换路由器上的 /root/smart/upcsv.sh（原版收割 CSV）。
# 新内核 collector 写入 smart_samples.db（单文件，保留 14 天两周期），
# 本脚本每周一把 db 上传到 gdrive:smart_data，供 train-pld 工作流拉取训练。
#
# 部署：
#   1) 把本文件放到路由器 /root/smart/upcsv.db.sh 并 chmod +x
#   2) crontab 改为：0 6 * * 1 /root/smart/upcsv.db.sh
#   3) 环境变量（rclone 配置已由原脚本/系统配置）：TG_BOT_TOKEN / TG_CHAT_ID
# 说明：旧 CSV（smart_weight_data.csv）不再产生，训练侧兼容读取旧文件，
#       若本地仍留有旧 CSV 可一并上传一次（第 7 行注释可取消）。
# =============================================================================
set -e

# 内核 HomeDir（ImmortalWrt nikki）—— 与内核 C.Path.HomeDir() 一致
DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
REMOTE=rclone
GDRIVE_DIR=gdrive:smart_data
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

# 云端保留两周期（15 天），更早的切片删除
"$REMOTE" delete "$GDRIVE_DIR" --min-age 15d --include 'smart_samples*.db' --log-level ERROR 2>/dev/null || true

# 旧 CSV 首次迁移（可选）：有旧数据时上传一次，训练侧会与 db 合并
# if [ -f "$DATA_DIR/smart_weight_data.csv" ]; then
#   "$REMOTE" copy "$DATA_DIR/smart_weight_data.csv" "$GDRIVE_DIR"/ --log-level ERROR
# fi

if [ -f "$DB_FILE" ]; then
  SIZE_KB=$(du -k "$DB_FILE" | awk '{print $1}')
  "$REMOTE" copy "$DB_FILE" "$GDRIVE_DIR"/ --log-level ERROR
  MSG="smart_samples.db 已上传 ($((SIZE_KB / 1024)) MB)"
else
  MSG="警告：$DB_FILE 不存在，本次未上传"
fi

# Telegram 通知
if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TG_CHAT_ID" -d text="$MSG" >/dev/null 2>&1 || true
fi

echo "[upcsv.db] $MSG"
