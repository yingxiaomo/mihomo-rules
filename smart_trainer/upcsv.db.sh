# =============================================================================
# upcsv.db.sh — PLD 训练数据周更上传 + 周期轮换 + Telegram 战报（v6，2026-10-28）
#
# 说明：只上传 PLD 数据。官方 collectdata 的 smart_weight_data.csv（40 列、
#       无 reward）是官方模型训练数据，不参与 PLD 训练，**绝不能上传**。
#
# 流程（用户确认的周期轮换设计）：
#   1. 每周一 06:00 cron：cp 快照 → rclone copyto 上传为【带日期的独立库文件】
#      smart_samples-YYYY-MM-DD.db → 远端校验；
#   2. 上传成功后清理网盘：删除稳定名 smart_samples.db（其内容按构造必然
#      ⊂ 最新日期文件，避免训练 glob 重复加载）+ 删除 14 天前的过期周期文件
#      （--min-age 14d，只匹配日期化库文件，网盘始终保留最近两个周期）；
#   3. 上传成功后【本地库轮换】：删除本地 smart_samples.db，内核 collector
#      检测到文件消失后自动重建空库（"从 0 开始"），本地库最多只装 7 天数据；
#   4. 训练侧 train-pld.yml 按 --include "smart_samples*" 下载网盘上保留的
#      两个周期文件，train_pld.py 的 sorted(glob) + concat 天然合并为 ~14 天
#      训练数据、无重叠。
#
# 上传失败 → 不轮换、不清理，本地库继续积累（500MiB 上限可容纳约三周，
# 连续失败也有充足余量），下周重试。
#
# 历史：
#   v3 (2026-08-24) cp 快照解决活跃库被 rclone 拒传；不再自动删云端文件
#                    （曾误删未到期数据进回收站）。
#   v4 (2026-09-15) copyto 上传为 smart_samples.db，修复与训练工作流不匹配。
#   v5 (2026-10-28) 找回 CSV 时代的 TG 战报（v3 迁移时凭据改为空环境变量，
#                    通知静默断了 6 周）；修复远端字节数显示单位。
#   v6 (2026-10-28) 实现周期轮换设计：日期化上传文件名 + 网盘 14 天文件级
#                    保留（找回 CSV 时代的云端清理语义，带 min-age 与日期
#                    模式双保险）+ 本地库上传后从 0 开始。移除 v5 误加的
#                    行级清理（"从 0 开始"取代一切行内删除）。
# =============================================================================
set -e

DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
SNAPSHOT="/tmp/smart_snapshot.db"
REMOTE=rclone
RCONF=/root/.config/rclone/rclone.conf
GDRIVE_DIR=gdrive:smart_data/pld
DATED_NAME="smart_samples-$(date +%F).db"
TG_TOKEN="${TG_TOKEN:-1885281278:AAE0FqQqyj_flhNxFA-0jqv7Zp_VaCt4ytM}"
TG_CHAT_ID="${TG_CHAT_ID:-1442416543}"
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
if [ -f "$DB_FILE" ]; then
  SIZE_KB=$(du -k "$DB_FILE" | awk '{print $1}')
  if [ "$SIZE_KB" -lt 1024 ]; then
    echo "[upcsv.db] skip: db too small ($SIZE_KB KB)"
    exit 1
  fi
  # 一致性快照：绕开 "source file is being updated" 拒传
  cp "$DB_FILE" "$SNAPSHOT"
  SNAP_KB=$(du -k "$SNAPSHOT" | awk '{print $1}')
  # 上传为【带日期的独立库文件】（同日重跑幂等覆盖）
  "$REMOTE" copyto "$SNAPSHOT" "$GDRIVE_DIR/$DATED_NAME" --config "$RCONF" --transfers=1 --log-level ERROR
  REMOTE_SIZE=$(rclone lsjson "$GDRIVE_DIR/$DATED_NAME" --config "$RCONF" --files-only 2>/dev/null | grep -o '"Size":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" -gt 0 ]; then
    rm -f "$SNAPSHOT"
    # 网盘清理（仅在日期文件上传校验成功后执行）：
    #   a) 稳定名 smart_samples.db：内容按构造 ⊂ 刚上传的日期文件，删除以防
    #      训练 glob 重复加载（首次迁移时清掉存量，之后本名不复存在）；
    #   b) --min-age 14d + 日期化模式：删除过期周期，保留最近两个周期。
    "$REMOTE" delete "$GDRIVE_DIR" --config "$RCONF" --include "smart_samples.db" --log-level ERROR || true
    "$REMOTE" delete "$GDRIVE_DIR" --config "$RCONF" --min-age 14d --include "smart_samples-2*.db" --log-level ERROR || true
    OLD_LEFT=$(rclone lsjson "$GDRIVE_DIR" --config "$RCONF" --files-only 2>/dev/null | grep -c '"Name":"smart_samples' || true)
    # 本地库轮换：删除后内核 collector 检测到文件消失自动重建空库（从 0 开始）
    rm -f "$DB_FILE"
    MSG="<b>🛰️ PLD 样本库周更完成</b>
--------------------
📦 本轮归档: <b>$DATED_NAME</b>（$((SNAP_KB/1024)) MB，远端校验 $((REMOTE_SIZE/1024/1024)) MB）
☁️ 网盘保留: ${OLD_LEFT} 个周期文件（>14 天已清）
🔄 本地库已从 0 开始新周期"
    send_telegram "$MSG"
    echo "[upcsv.db] $MSG"
    exit 0
  else
    send_telegram "❌ <b>PLD 样本库上传失败</b>
--------------------
⚠️ 远端校验失败 (remote_size=${REMOTE_SIZE})，本地库未轮换，下周重试"
    MSG="警告：上传后远端校验失败 (remote_size=$REMOTE_SIZE)"
    exit 1
  fi
fi

MSG="警告：$DB_FILE 不存在"
send_telegram "❌ <b>PLD 样本库上传失败</b>
--------------------
⚠️ $DB_FILE 不存在"
echo "[upcsv.db] $MSG"
