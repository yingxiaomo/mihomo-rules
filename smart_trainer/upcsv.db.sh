# =============================================================================
# upcsv.db.sh — PLD 训练数据上传 + 保留期清理（v5，2026-10-28）
#
# 说明：只上传 PLD 数据（smart_samples.db / smart_samples.csv 回退）。
#       官方 collectdata 的 smart_weight_data.csv（40 列、无 reward）是官方模型
#       训练数据，不参与 PLD 训练，**绝不能上传**到 gdrive:smart_data。
#
# 2026-08-24 v3：SQLite 活跃库直接 rclone 会因 "source file is being updated"
#   被拒传（内核持续写样本），这就是 8/18、8/23 训练 CI 失败的根因。
#   新流程：cp 快照 → 校验快照大小 → 上传快照 → 远端校验。
#   不再自动 delete 云端文件（曾误删未到期数据进回收站）。
#
# 2026-09-15 v4：修复上传文件名与训练工作流不匹配的 bug。
#   旧版用 rclone copy 上传为 smart_snapshot.db，但 train-pld.yml 用
#   --include "smart_samples*" 下载，导致训练永远拿到旧的 smart_samples.db。
#   改用 rclone copyto 上传为 smart_samples.db，确保训练工作流能拉到最新数据。
#
# 2026-10-28 v5：找回 CSV 时代 upcsv.sh 的保留期清理策略（CSV→SQLite 迁移时
#   遗失，导致 smart_samples.db 一路涨到 100MB 上限附近，触顶后内核 AddSample
#   静默停止采样且无可见告警）。上传成功并远端校验通过后，清理本地库 14 天前
#   旧样本（= 保留两个上传周期）并 VACUUM；上传失败绝不删数据。
#   顺带修复远端校验字节数的显示单位（原版把字节当 KB 除了一次）。
# =============================================================================
set -e

DATA_DIR=/etc/nikki/run
DB_FILE="$DATA_DIR/smart_samples.db"
CSV_FALLBACK="$DATA_DIR/smart_samples.csv"
SNAPSHOT="/tmp/smart_snapshot.db"
RETENTION_DAYS=14
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
  # v4: copyto 上传为 smart_samples.db，与 train-pld.yml 的 --include "smart_samples*" 匹配
  "$REMOTE" copyto "$SNAPSHOT" "$GDRIVE_DIR/smart_samples.db" --config "$RCONF" --transfers=1 --log-level ERROR
  REMOTE_SIZE=$(rclone lsjson "$GDRIVE_DIR/smart_samples.db" --config "$RCONF" --files-only 2>/dev/null | grep -o '"Size":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" -gt 0 ]; then
    MSG="smart_samples.db 已上传 (本地 $((SIZE_KB/1024)) MB, 快照 $((SNAP_KB/1024)) MB, 远端 $((REMOTE_SIZE/1024/1024)) MB)"
    rm -f "$SNAPSHOT"
    # v5: 上传成功后才清理本地旧样本（保留 14 天 = 两个上传周期）
    CLEAN_MSG=$(python3 - "$DB_FILE" "$RETENTION_DAYS" <<'PYEOF'
import sqlite3, sys, time
db_path, days = sys.argv[1], int(sys.argv[2])
cutoff = int(time.time()) - days * 86400
db = sqlite3.connect(db_path, timeout=60)
db.execute("PRAGMA busy_timeout=60000")
cur = db.cursor()
n = cur.execute("SELECT COUNT(*) FROM samples WHERE ts < ?", (cutoff,)).fetchone()[0]
if n > 0:
    cur.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    db.commit()
    t0 = time.time()
    db.execute("VACUUM")
    db.commit()
    print("已清理 %d 条 %d 天前旧样本并 VACUUM (%.1fs), 截止 %s UTC" % (n, days, time.time()-t0, time.strftime('%Y-%m-%d %H:%M', time.gmtime(cutoff))))
else:
    print("无 %d 天前旧样本, 无需清理" % days)
db.close()
PYEOF
)
    MSG="$MSG；$CLEAN_MSG"
  else
    MSG="警告：上传后远端校验失败 (remote_size=$REMOTE_SIZE)"
    exit 1
  fi
fi

if [ -f "$CSV_FALLBACK" ]; then
  "$REMOTE" copyto "$CSV_FALLBACK" "$GDRIVE_DIR/smart_samples.csv" --config "$RCONF" --transfers=1 --log-level ERROR
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
