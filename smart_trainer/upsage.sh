# =============================================================================
# upsage.sh — Sage 原生事件周更上传 + 本地轮换 + Telegram 战报（v1，2026-11-15）
#
# 只上传 Sage 内核 collect-events 产出的原生事件 JSONL。与训练合同对齐：
#   train-sage-prior.yml 从 ${REMOTE}/sage-native 只取
#   *.jsonl.ready / *.jsonl.failed / *.jsonl.recovered（.open 活跃文件不取）。
#   文件名含 epoch+序号，天然唯一，无需改名。
#
# 流程：
#   1. 归集 g-*/ 下的已关闭事件文件（含上次遗留）到本地归档目录；
#   2. rclone copy 上传到 gdrive:smart_data/sage-native；
#   3. 远端校验通过后本地轮换：删除归档目录中已上传文件。
#      .open 活跃文件不动，收集器按 64MB/1h 继续轮转出新的 .ready。
#
# 上传失败 → 不轮换，文件继续积累（每小时轮转、64MB/个，失败数周也有充足
# 余量），下次运行重试。TG 战报与 upcsv.db.sh 同款。
#
# 与 upcsv.db.sh 的关系：PLD 样本库已冻结（sage 期不产样本），本脚本
# 取代其 cron 位置；旧脚本文件保留不删。
# =============================================================================
set -e

SRC_BASE=/etc/nikki/run/sage-events
ARCHIVE=/root/smart/sage-archive
REMOTE=rclone
RCONF=/root/.config/rclone/rclone.conf
GDRIVE_DIR=gdrive:smart_data/sage-native
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
TG_PROXY="http://127.0.0.1:7890"

send_telegram() {
  local msg="$1"
  [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT_ID" ] && return 0
  export https_proxy="$TG_PROXY" http_proxy="$TG_PROXY"
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${msg}" \
    --connect-timeout 5 --max-time 15 \
    --retry 2 --retry-delay 2 >/dev/null 2>&1 || true
  unset https_proxy http_proxy
}

MSG=""
mkdir -p "$ARCHIVE" "$SRC_BASE"

# 1. 归集：g-*/ 下的已关闭事件文件 → ARCHIVE（扁平化，名字含 epoch+序号唯一）
MOVED=0
for f in "$SRC_BASE"/*/*.jsonl.ready "$SRC_BASE"/*/*.jsonl.failed "$SRC_BASE"/*/*.jsonl.recovered \
         "$ARCHIVE"/*.jsonl.ready "$ARCHIVE"/*.jsonl.failed "$ARCHIVE"/*.jsonl.recovered; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  [ -f "$ARCHIVE/$base" ] && continue
  mv "$f" "$ARCHIVE/$base"
  MOVED=$((MOVED+1))
done

# 2. 无待传文件：安静退出（采集器每小时轮转，正常周不该发生；出现说明采集停了）
if [ "$MOVED" -eq 0 ] && [ -z "$(ls "$ARCHIVE" 2>/dev/null)" ]; then
  echo "[upsage] 无待上传的原生事件文件。若采集已开启超过 1 小时，请检查 collect-events 与事件目录。"
  exit 1
fi

# 3. 上传 ARCHIVE 全部内容（含上次失败遗留，幂等覆盖）
BYTES=$(cat "$ARCHIVE"/*.jsonl.ready "$ARCHIVE"/*.jsonl.failed "$ARCHIVE"/*.jsonl.recovered 2>/dev/null | wc -c)
FILES=$(ls "$ARCHIVE" | wc -l)
echo "[upsage] 上传 $FILES 个文件 / $BYTES 字节 → $GDRIVE_DIR"
rclone copy "$ARCHIVE" "$GDRIVE_DIR" --config "$RCONF" --transfers=4 --checkers=8 --log-level=INFO

# 4. 远端校验：本次归档的每个文件在远端存在且字节数一致
MISS=0
for f in "$ARCHIVE"/*; do
  base=$(basename "$f")
  local_size=$(wc -c < "$f")
  remote_size=$(rclone lsl "$GDRIVE_DIR/$base" --config "$RCONF" 2>/dev/null | awk '{print $1}')
  if [ -z "$remote_size" ] || [ "$remote_size" != "$local_size" ]; then
    echo "[upsage] 远端校验失败: $base local=$local_size remote=${remote_size:-missing}"
    MISS=$((MISS+1))
  fi
done
if [ "$MISS" -gt 0 ]; then
  MSG="⚠️ <b>[upsage] 远端校验失败 $MISS 个文件</b>（本地归档保留，下周重试）"
  send_telegram "$MSG"
  echo "[upsage] $MSG"
  exit 1
fi

# 5. 校验通过 → 本地轮换：清空归档目录（文件已安全在远端）
rm -f "$ARCHIVE"/*.jsonl.ready "$ARCHIVE"/*.jsonl.failed "$ARCHIVE"/*.jsonl.recovered 2>/dev/null || true

MSG="🛰️ <b>[upsage] Sage 原生事件周更完成</b>
📦 本轮归档: <b>$FILES 个事件文件</b>（$((BYTES/1024/1024)) MB，远端校验一致）
☁️ 网盘路径: sage-native
🔄 已关闭事件文件本地轮换，活跃 .open 不动"
echo "[upsage] 完成: $FILES 个文件 / $BYTES 字节已上传并校验"
send_telegram "$MSG"
