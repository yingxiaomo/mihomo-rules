#!/bin/sh
# =============================================================================
# update_model.sh — 官方 Smart 模型更新（原版 Smart 训练线）
#
# 从 mihomo-rules 的 smart-model release 下载 Model.bin，
# 校验后原子换装到 /etc/nikki/run/Model.bin，重启生效。
# 重建版（原脚本无存档，按行为文档重建：检查 release → 下载 → 校验 → 换装 → 重启）。
# =============================================================================
set -e

REPO=yingxiaomo/mihomo-rules
TAG=smart-model
ASSET=Model.bin
MODEL_PATH=/etc/nikki/run/Model.bin
TMP=/tmp/Model.new
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

mkdir -p /tmp && rm -f "$TMP"
curl -sL --max-time 120 --proxy "$PROXY" -o "$TMP" \
    "https://github.com/${REPO}/releases/download/${TAG}/${ASSET}" || {
    echo "[update-model] 下载失败"
    send_telegram "❌ <b>[update-model] Model.bin 下载失败</b>"
    exit 1
}

if [ -f "$MODEL_PATH" ] && cmp -s "$TMP" "$MODEL_PATH"; then
    echo "[update-model] 模型无变化，跳过"
    rm -f "$TMP"
    exit 0
fi

mv -f "$TMP" "$MODEL_PATH"
echo "[update-model] Model.bin 已更新，重启 mihomo 生效"
/etc/init.d/nikki restart >/dev/null 2>&1

MSG="🔄 <b>[update-model] 官方 Smart 模型已更新</b>
📦 ${ASSET}"
echo "[update-model] 完成"
send_telegram "$MSG"
