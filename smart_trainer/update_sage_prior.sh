#!/bin/sh
# =============================================================================
# update_sage_prior.sh — Sage 分维度先验模型更新（v1，2026-11-15）
#
# 从 GitHub Releases 拉取最新的 sage-prior-model-* 发布：
#   SagePrior-v1.zip（内含 SagePrior.json + 四个维度 .lgb）+ sha256
# 校验通过后原子换装到 /etc/nikki/run/sage-prior/，仅当内容变化时重启。
#
# 与 update_model.sh（旧 PriorModel.bin）的关系：sage 内核不读
# PriorModel.bin，旧脚本由本脚本取代（旧文件保留未删）。
# 首轮运行若发布不存在，安静退出——等训练仓 push 后 CI 产出再拉。
# =============================================================================
set -e

REPO=yingxiaomo/mihomo-rules
TAG_PREFIX=sage-prior-model-
DST=/etc/nikki/run/sage-prior
MARKER=$DST/.release_tag
TMP=/tmp/sage-prior.new
PROXY="http://127.0.0.1:7890"
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

send_telegram() {
  local msg="$1"
  [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT_ID" ] && return 0
  export https_proxy="$PROXY" http_proxy="$PROXY"
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${msg}" \
    --connect-timeout 5 --max-time 15 \
    --retry 2 --retry-delay 2 >/dev/null 2>&1 || true
  unset https_proxy http_proxy
}

# 1. 找最新的 sage-prior-model-* 发布
LATEST=$(curl -s --proxy "$PROXY" --max-time 20 \
  "https://api.github.com/repos/${REPO}/releases?per_page=30" | python3 -c '
import json, sys
rels = json.load(sys.stdin)
for r in rels:
    if str(r.get("tag_name", "")).startswith("sage-prior-model-"):
        print(r["tag_name"])
        break
' 2>/dev/null) || LATEST=""

if [ -z "$LATEST" ]; then
  echo "[upsage-prior] 无 sage-prior-model-* 发布，跳过"
  exit 0
fi

# 2. 已是最新：跳过（不重启）
CUR=$(cat "$MARKER" 2>/dev/null || echo "")
if [ "$CUR" = "$LATEST" ]; then
  echo "[upsage-prior] 已是最新 ($LATEST)，跳过"
  exit 0
fi

# 3. 下载 + 校验
rm -rf "$TMP"
mkdir -p "$TMP"
cd "$TMP"
curl -sL --proxy "$PROXY" --max-time 180 \
  -o SagePrior-v1.zip \
  "https://github.com/${REPO}/releases/download/${LATEST}/SagePrior-v1.zip"
curl -sL --proxy "$PROXY" --max-time 30 \
  -o SagePrior-v1.zip.sha256 \
  "https://github.com/${REPO}/releases/download/${LATEST}/SagePrior-v1.zip.sha256"
sha256sum -c SagePrior-v1.zip.sha256 >/dev/null 2>&1 || {
  echo "[upsage-prior] sha256 校验失败，保留现有模型"
  exit 1
}
unzip -o -q SagePrior-v1.zip
python3 -c "import json; d = json.load(open('SagePrior.json')); assert d.get('dimensions'), 'manifest invalid'"

# 4. 原子换装
rm -rf "${DST}.old"
if [ -d "$DST" ]; then
  mv "$DST" "${DST}.old"
fi
mkdir -p "$DST"
cp SagePrior.json "$DST/"
cp *.lgb "$DST/"
echo "$LATEST" > "$MARKER"
cd /
rm -rf "$TMP" "${DST}.old"

# 5. 重启生效（节点学习状态经 cachefile 持久化，重启不失忆）
/etc/init.d/nikki restart >/dev/null 2>&1

MSG="🔄 <b>[sage-prior] 分维度先验已更新</b>
🏷️ $LATEST
📦 SagePrior.json + 四维 .lgb 已换装，内核已重启"
echo "[upsage-prior] $MSG"
send_telegram "$MSG"
