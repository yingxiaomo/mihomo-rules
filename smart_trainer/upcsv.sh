#!/bin/sh
# =============================================================================
# upcsv.sh — 官方 Smart 训练数据上传（原版 Smart 训练线）
#
# 上传内核 collectdata 产生的 smart_weight_data.csv 到
# gdrive:smart_data/official/（rclone 配置见 /root/.config/rclone/rclone.conf），
# 附 15 天保留期清理（本地+云端），TG 通知（硬编码默认凭据，可被环境变量覆盖）。
#
# 用法：crontab 定时或手动执行。重构版（原脚本无存档，按行为文档重建）。
# =============================================================================
set -e

CSV_FILE="${CSV_FILE:-/etc/nikki/run/smart_weight_data.csv}"
REMOTE_DIR="gdrive:smart_data/official"
RETENTION_DAYS=15
TG_TOKEN="${TG_TOKEN:-1885281278:AAE0FqQqyj_flhNxFA-0jqv7Zp_VaCt4ytM}"
TG_CHAT_ID="${TG_CHAT_ID:-1442416543}"
PROXY="http://127.0.0.1:7890"

send_telegram() {
    [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT_ID" ] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" -d "parse_mode=HTML" \
        --data-urlencode "text=$1" --connect-timeout 5 --max-time 15 \
        --retry 2 --retry-delay 2 >/dev/null 2>&1 || true
}

if [ ! -f "$CSV_FILE" ]; then
    echo "[upcsv] $CSV_FILE 不存在，跳过"
    exit 0
fi

SIZE=$(wc -c < "$CSV_FILE")
echo "[upcsv] 上传 $CSV_FILE ($SIZE 字节) -> $REMOTE_DIR/"
rclone copy "$CSV_FILE" "$REMOTE_DIR/" --header "Referer:" >/dev/null 2>&1 || {
    echo "[upcsv] 上传失败"
    send_telegram "❌ <b>[upcsv] 官方 Smart CSV 上传失败</b>"
    exit 1
}

# 远端校验
REMOTE_SIZE=$(rclone ls "$REMOTE_DIR" --include "$(basename "$CSV_FILE")" 2>/dev/null | awk '{print $1}')
if [ "$REMOTE_SIZE" != "$SIZE" ]; then
    echo "[upcsv] 远端校验失败（本地 $SIZE / 远端 ${REMOTE_SIZE:-0}）"
    send_telegram "❌ <b>[upcsv] 官方 Smart CSV 远端校验失败</b>"
    exit 1
fi

# 保留期清理：云端删 15 天前
rclone delete "$REMOTE_DIR/" --min-age "${RETENTION_DAYS}d" >/dev/null 2>&1 || true

MSG="✅ <b>[upcsv] 官方 Smart CSV 已上传</b>
📦 $(basename "$CSV_FILE") ($SIZE 字节)
☁️ $REMOTE_DIR/"
echo "[upcsv] 完成"
send_telegram "$MSG"
