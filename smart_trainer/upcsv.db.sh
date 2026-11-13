# =============================================================================
# upcsv.db.sh — PLD 训练数据周更上传 + 周期轮换 + Telegram 战报（v8）
#
# 只上传 PLD 的 smart_samples.db / smart_samples.csv；官方
# smart_weight_data.csv 不属于这条训练链路，绝不能上传。
#
# 周期：生成一致快照 → 时间戳+进程号唯一命名上传 → 字节数校验 → 清理云端旧周期 →
# SQLite 只删除快照已覆盖的 id 并 VACUUM；快照后的新样本始终留在活跃库。
# CSV 无法与活跃 writer 事务协调，上传成功后保留本地累计文件，绝不冒险截断。
# SQLite 必须通过 sqlite3 在线备份 API 取快照，禁止直接复制活跃主库。
# CSV 回退是追加文件：仅在复制前后源文件大小稳定且快照字节数一致时放行。
# =============================================================================
set -eu

DATA_DIR="${DATA_DIR:-/etc/nikki/run}"
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
RCLONE_BIN="${RCLONE_BIN:-rclone}"
RCONF="${RCONF:-/root/.config/rclone/rclone.conf}"
GDRIVE_DIR="${GDRIVE_DIR:-gdrive:smart_data/pld}"
MIN_SIZE_KB="${MIN_SIZE_KB:-1024}"
TG_TOKEN="${TG_BOT_TOKEN:-${TG_TOKEN:-}}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
TG_PROXY="${TG_PROXY:-http://127.0.0.1:7890}"
SNAPSHOT=""
LOCAL_SAFETY="本地数据未轮换"

cleanup_snapshot() {
  if [ -n "$SNAPSHOT" ]; then
    rm -f "$SNAPSHOT"
  fi
}
trap cleanup_snapshot EXIT

send_telegram() {
  local msg="$1"
  if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
    return 0
  fi
  if [ -n "$TG_PROXY" ]; then
    export https_proxy="$TG_PROXY" http_proxy="$TG_PROXY"
  fi
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${msg}" \
    --connect-timeout 5 --max-time 15 \
    --retry 2 --retry-delay 2 >/dev/null 2>&1 || true
  if [ -n "$TG_PROXY" ]; then
    unset https_proxy http_proxy
  fi
}

fail_upload() {
  local reason="$1"
  send_telegram "❌ <b>PLD 样本上传失败</b>
--------------------
⚠️ ${reason}；${LOCAL_SAFETY}"
  echo "[upcsv.db] ERROR: $reason" >&2
  exit 1
}

SOURCE_FILE=""
DATED_NAME=""
SOURCE_TYPE=""
SNAPSHOT_MAX_ID=""
RUN_ID="$(date -u +%F-%H%M%S)-$$"
if [ -f "$DB_FILE" ]; then
  SOURCE_FILE="$DB_FILE"
  SOURCE_TYPE=db
  SNAPSHOT="/tmp/smart_snapshot-${RUN_ID}.db"
  DATED_NAME="smart_samples-${RUN_ID}.db"
  command -v sqlite3 >/dev/null 2>&1 || fail_upload "缺少 sqlite3，无法安全备份活跃数据库"
  rm -f "$SNAPSHOT"
  if ! sqlite3 "$DB_FILE" ".timeout 5000" ".backup '$SNAPSHOT'"; then
    fail_upload "SQLite 在线备份失败"
  fi
  CHECK_RESULT=$(sqlite3 "$SNAPSHOT" 'PRAGMA quick_check;' 2>/dev/null || true)
  [ "$CHECK_RESULT" = "ok" ] || fail_upload "SQLite 快照完整性检查失败"
  SNAPSHOT_MAX_ID=$(sqlite3 "$SNAPSHOT" 'SELECT COALESCE(MAX(id), 0) FROM samples;' 2>/dev/null || true)
  case "$SNAPSHOT_MAX_ID" in
    ''|*[!0-9]*) fail_upload "无法读取 SQLite 快照最大 id" ;;
    0) fail_upload "SQLite 快照没有可归档样本" ;;
  esac
elif [ -f "$CSV_FALLBACK" ]; then
  SOURCE_FILE="$CSV_FALLBACK"
  SOURCE_TYPE=csv
  SNAPSHOT="/tmp/smart_snapshot-${RUN_ID}.csv"
  DATED_NAME="smart_samples-${RUN_ID}.csv"
  rm -f "$SNAPSHOT"
  BEFORE_SIZE=$(wc -c < "$CSV_FALLBACK" | tr -d ' ')
  cp "$CSV_FALLBACK" "$SNAPSHOT"
  AFTER_SIZE=$(wc -c < "$CSV_FALLBACK" | tr -d ' ')
  SNAP_SIZE=$(wc -c < "$SNAPSHOT" | tr -d ' ')
  if [ "$BEFORE_SIZE" -ne "$AFTER_SIZE" ] || [ "$SNAP_SIZE" -ne "$BEFORE_SIZE" ]; then
    fail_upload "CSV 在复制期间发生写入，请下次重试"
  fi
  HEADER=$(sed -n '1p' "$SNAPSHOT")
  case ",$HEADER," in *,reward,*) ;; *) fail_upload "CSV 快照缺少 reward 列" ;; esac
  case ",$HEADER," in *,target,*) ;; *) fail_upload "CSV 快照缺少 target 列" ;; esac
  case ",$HEADER," in *,sample_weight,*) ;; *) fail_upload "CSV 快照缺少 sample_weight 列" ;; esac
else
  fail_upload "$DB_FILE 与 $CSV_FALLBACK 均不存在"
fi

SIZE_KB=$(du -k "$SNAPSHOT" | awk '{print $1}')
[ "$SIZE_KB" -ge "$MIN_SIZE_KB" ] || fail_upload "快照过小 (${SIZE_KB} KB)"
LOCAL_SIZE=$(wc -c < "$SNAPSHOT" | tr -d ' ')

if ! "$RCLONE_BIN" copyto "$SNAPSHOT" "$GDRIVE_DIR/$DATED_NAME" \
  --config "$RCONF" --transfers=1 --log-level ERROR; then
  fail_upload "rclone copyto 失败"
fi

REMOTE_JSON=$("$RCLONE_BIN" lsjson "$GDRIVE_DIR/$DATED_NAME" \
  --config "$RCONF" --files-only 2>/dev/null || true)
REMOTE_SIZE=$(printf '%s' "$REMOTE_JSON" | \
  sed -n 's/.*"Size":[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1 || true)
if [ -z "$REMOTE_SIZE" ] || [ "$REMOTE_SIZE" -ne "$LOCAL_SIZE" ]; then
  fail_upload "远端字节数校验失败 (local=${LOCAL_SIZE}, remote=${REMOTE_SIZE:-missing})"
fi

"$RCLONE_BIN" delete "$GDRIVE_DIR" --config "$RCONF" \
  --include "smart_samples.db" --include "smart_samples.csv" --log-level ERROR || true
"$RCLONE_BIN" delete "$GDRIVE_DIR" --config "$RCONF" --min-age 14d \
  --include "smart_samples-2*.db" --include "smart_samples-2*.csv" --log-level ERROR || true
OLD_LEFT=$("$RCLONE_BIN" lsjson "$GDRIVE_DIR" --config "$RCONF" --files-only 2>/dev/null | \
  grep -c '"Name":[[:space:]]*"smart_samples' || true)

if [ "$SOURCE_TYPE" = db ]; then
  # 在线备份结束后 collector 仍可追加。只清理快照覆盖的旧 id，绝不删除活跃
  # 数据库；AUTOINCREMENT 保证快照后的新行 id 更大，不会被这次轮换误删。
  if ! sqlite3 "$DB_FILE" ".timeout 30000" \
    "BEGIN IMMEDIATE; DELETE FROM samples WHERE id <= $SNAPSHOT_MAX_ID; COMMIT;"; then
    fail_upload "远端归档成功，但本地已归档行清理失败"
  fi
  LOCAL_SAFETY="已归档行已安全保存在远端，本地新样本仍保留"
  if ! sqlite3 "$DB_FILE" ".timeout 30000" "VACUUM;"; then
    fail_upload "远端归档成功且旧行已清理，但本地数据库压缩失败"
  fi
  ROTATION_STATUS="SQLite 已清理 id≤${SNAPSHOT_MAX_ID}；快照后新样本保留"
else
  ROTATION_STATUS="CSV 活跃源文件已保留（不执行不安全截断）"
fi

MSG="<b>🛰️ PLD 样本周更完成</b>
--------------------
📦 本轮归档: <b>$DATED_NAME</b>（$((SIZE_KB/1024)) MB，远端校验 $((REMOTE_SIZE/1024/1024)) MB）
☁️ 网盘保留: ${OLD_LEFT} 个周期文件（>14 天已清）
🔄 ${ROTATION_STATUS}"
send_telegram "$MSG"
echo "[upcsv.db] $MSG"
exit 0
