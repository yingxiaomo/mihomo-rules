#!/usr/bin/env python3
"""Regret 分析：量化 PLD 实际选路 vs 同窗口最优节点的差距。

定义（每个 target × 4h 时间桶内，只比较成功结算的连接）：
- 大流量（download_mb >= 1）：吞吐指标 = maxdownloadrate_kb（KB/s 峰值速率）。
  桶内最优 = 该桶所有节点中的最大吞吐；单样本遗憾 = (最优 - 实际) KB/s，
  按持续时长折算成 MB 损失。桶遗憾% = 损失MB / 最优可达MB。
- 小流量（0 < download_mb < 1）：连接指标 = expm1(connect_time) 还原的真实毫秒
  （schema 教训：connect_time/latency 列是 log1p 编码）。遗憾 = 实际ms - 桶内最快ms。

输出：全局遗憾、Top 遗憾目标、逐日趋势——结果用于判断 Phase 2 的方向权重。

用法: python analyze_regret.py <smart_samples.db 或所在目录>
"""
import glob
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BUCKET_SEC = 4 * 3600
LARGE_MB = 1.0


def load(db_path: str) -> pd.DataFrame:
    files = [db_path] if db_path.endswith(".db") else sorted(glob.glob(str(Path(db_path) / "smart_samples*.db")))
    if not files:
        raise FileNotFoundError(db_path)
    dfs = []
    for f in files:
        conn = sqlite3.connect(f)
        df = pd.read_sql("SELECT * FROM samples", conn)
        conn.close()
        dfs.append(df)
    merged = pd.concat(dfs, ignore_index=True)
    # 与 train_pld.load_data 相同的噪声排除（保持口径一致）
    t = merged["target"].astype(str)
    merged = merged[~(t.str.contains(".doh.", regex=False, na=False) | t.str.contains("谷歌FCM", regex=False, na=False))]
    merged = merged.reset_index(drop=True)
    return merged


def decode_ms(col: pd.Series) -> pd.Series:
    """connect_time/latency 列是 log1p(毫秒)，expm1 还原。"""
    return col.map(lambda v: math.expm1(v) if pd.notna(v) and v > 0 else 0.0)


def mb_moved(rate_kb: float, dur_min: float) -> float:
    """KB/s × 分钟 → MB。"""
    return rate_kb * dur_min * 60.0 / 1024.0


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "."
    df = load(db)
    df["bucket"] = df["ts"] // BUCKET_SEC
    ok = df[df["reward"] > -1.99]  # 排除硬失败结算

    print(f"样本 {len(df)} 条（排除噪声后），成功结算 {len(ok)} 条")
    print("=" * 72)

    # ---------- 大流量遗憾 ----------
    large = ok[ok["download_mb"] >= LARGE_MB].copy()
    large = large[large["maxdownloadrate_kb"] > 0]
    large["dur_min"] = large["duration_minutes"].clip(lower=0.01)
    lost_total, best_total, achieved_total = 0.0, 0.0, 0.0
    target_stat = {}
    day_stat = {}
    for (tgt, bucket), g in large.groupby(["target", "bucket"]):
        if g["node_name"].nunique() < 2 or len(g) < 4:
            continue
        best = g["maxdownloadrate_kb"].max()
        g = g.copy()
        g["lost_kb"] = best - g["maxdownloadrate_kb"]
        g["mb_best"] = g.apply(lambda r: mb_moved(best, r["dur_min"]), axis=1)
        g["mb_ach"] = g.apply(lambda r: mb_moved(r["maxdownloadrate_kb"], r["dur_min"]), axis=1)
        g["mb_lost"] = g["mb_best"] - g["mb_ach"]
        lost_total += g["mb_lost"].sum()
        best_total += g["mb_best"].sum()
        achieved_total += g["mb_ach"].sum()
        st = target_stat.setdefault(tgt, [0.0, 0.0, 0, g["node_name"].nunique()])
        st[0] += g["mb_lost"].sum()
        st[1] += g["mb_best"].sum()
        st[2] += len(g)
        st[3] = max(st[3], g["node_name"].nunique())
        day = pd.to_datetime(bucket * BUCKET_SEC, unit="s").strftime("%m-%d")
        ds = day_stat.setdefault(day, [0.0, 0.0])
        ds[0] += g["mb_lost"].sum()
        ds[1] += g["mb_best"].sum()

    print("\n【大流量吞吐遗憾】(download>=1MB, 仅成功结算, target×4h 桶内对比)")
    if best_total > 0:
        print(f"\n  实际传输 {achieved_total/1024:.2f} GB | 最优可达 {best_total/1024:.2f} GB | "
              f"遗憾 {lost_total/1024:.2f} GB | **峰值预言机遗憾 {100*lost_total/best_total:.2f}%**")

    # 稳定预言机：桶内按节点平均吞吐取最优（剔除单连接突发干扰）。
    # PLD 的 EMA 选路优化的是期望值，不是单桶峰值——稳定预言机才是
    # "选路本可以持续更好"的公平上界。
    s_lost, s_best = 0.0, 0.0
    for (tgt, bucket), g in large.groupby(["target", "bucket"]):
        if g["node_name"].nunique() < 2 or len(g) < 4:
            continue
        node_mean = g.groupby("node_name")["maxdownloadrate_kb"].mean()
        best_mean = node_mean.max()
        g2 = g.assign(node_mean=g["node_name"].map(node_mean))
        s_best += (best_mean * g2["dur_min"]).sum() * 60.0 / 1024.0
        s_lost += ((best_mean - g2["node_mean"]).clip(lower=0) * g2["dur_min"]).sum() * 60.0 / 1024.0
    if s_best > 0:
        print(f"  稳定预言机遗憾（桶内节点均值基准）：**{100*s_lost/s_best:.2f}%** "
              f"(损失 {s_lost/1024:.2f} GB / 最优可达 {s_best/1024:.2f} GB)")
    print(f"  峰值口径样本基数小（{int(large.shape[0])} 条），百分比波动大，重点看趋势与目标排序。")
    rows = sorted(target_stat.items(), key=lambda kv: -kv[1][0])[:10]
    print(f"  {'目标':<34}{'损失MB':>9}{'遗憾%':>8}{'样本':>7}{'节点数':>6}")
    for tgt, (lost, best, n, nodes) in rows:
        pct = 100 * lost / best if best > 0 else 0
        print(f"  {tgt[:32]:<34}{lost:>9.1f}{pct:>7.1f}%{n:>7}{nodes:>6}")
    print("  逐日趋势:")
    for day in sorted(day_stat):
        lost, best = day_stat[day]
        print(f"    {day}: 遗憾 {100*lost/best if best else 0:5.2f}%")

    # ---------- 小流量连接遗憾 ----------
    small = ok[(ok["download_mb"] > 0) & (ok["download_mb"] < LARGE_MB)].copy()
    small["conn_ms"] = decode_ms(small["connect_time"])
    small = small[small["conn_ms"] > 0]
    regs, slow_share_n, slow_total = [], 0, 0
    for (tgt, bucket), g in small.groupby(["target", "bucket"]):
        if g["node_name"].nunique() < 2 or len(g) < 4:
            continue
        best = g["conn_ms"].min()
        r = g["conn_ms"] - best
        regs.extend(r.tolist())
        slow_total += len(r)
        slow_share_n += int((r > 100).sum())
    print("\n【小流量连接遗憾】(0<download<1MB, 桶内最快连接为基准)")
    if regs:
        s = pd.Series(regs)
        print(f"  可比样本 {len(s)} | 遗憾中位数 {s.median():.0f}ms | p90 {s.quantile(0.9):.0f}ms | "
              f">100ms 占比 {100*slow_share_n/slow_total:.1f}%")

    # ---------- 节点侧归因 ----------
    print("\n【节点侧遗憾归因】(大流量：该节点被选中时造成的损失)")
    if "node_name" in large.columns and len(large):
        best_map = large.groupby(["target", "bucket"])["maxdownloadrate_kb"].transform("max")
        large2 = large.assign(best_kb=best_map)
        large2["lost_mb"] = large2.apply(
            lambda r: mb_moved(max(0.0, r["best_kb"] - r["maxdownloadrate_kb"]), r["dur_min"]), axis=1)
        node_lost = large2.groupby("node_name")["lost_mb"].sum().sort_values(ascending=False)[:8]
        for node, lost in node_lost.items():
            if lost > 1:
                print(f"  {node[:40]:<42}{lost:>9.1f} MB")


if __name__ == "__main__":
    main()
