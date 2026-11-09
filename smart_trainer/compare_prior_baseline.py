#!/usr/bin/env python3
"""经验贝叶斯查表基线 vs LightGBM 先验：同拆分、同验证集对比。

回答一个问题：LightGBM 先验是否真的优于简单 (target,node) 查表？
- 沿用 train_pld 的 load_data / preprocess_pld / GroupShuffleSplit(random_state=42)
  —— 与周期训练完全同协议。
- EB 查表：分层收缩 pair均值 → target均值 → 全局均值（K=10，对应内核 α0/decay 语义）。
- 查表无法泛化到训练集没见过的 (target,node) 组合——这正是模型必须挣饭吃的地方，
  所以额外输出 seen/unseen 两个 regime 的分解。

用法: python compare_prior_baseline.py <data_dir 或 .db 路径>
"""
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import GroupShuffleSplit

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()

sys.path.insert(0, str(Path(__file__).parent))
from train_pld import load_data, preprocess_pld, train_model  # noqa: E402

K_SHRINK = 10.0


def rmse(y, p):
    return float(np.sqrt(mean_squared_error(y, p)))


def main():
    data = sys.argv[1] if len(sys.argv) > 1 else "."
    df = load_data(Path(data))
    X, y, w, groups = preprocess_pld(df)

    # 还原每个样本的 target/node（\x1f 分隔，pandas 3 不会吞；与 train_pld 组键同构）
    key = (
        df.loc[X.index, "target"].fillna("").astype(str)
        + "\x1f"
        + df.loc[X.index, "node_name"].astype(str)
    )
    assert (key.values == groups.values).all(), "键还原失败，索引未对齐"

    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    tr, te = next(gss.split(X, y, groups=groups))
    logger.info(f"训练 {len(tr)} 条 / 验证 {len(te)} 条（组合拆分，协议同 train_pld）")

    y_tr, y_te = y.iloc[tr], y.iloc[te]
    key_tr, key_te = key.iloc[tr], key.iloc[te]

    # ---------- LightGBM（与 train_model 同参同早停） ----------
    model = train_model(X.iloc[tr], y.iloc[tr], w.iloc[tr], X.iloc[te], y.iloc[te], w.iloc[te])
    pred_lgb = model.predict(X.iloc[te])
    logger.info(f"LightGBM 最佳迭代 {model.best_iteration}")

    # ---------- 经验贝叶斯查表（分层收缩 × 通道维度） ----------
    # 关键：查表必须带通道维度（内核 ChannelStats 就是按通道的），否则同一
    # (target,node) 的小/大流量 reward 混均值、方差爆炸，对查表不公平。
    ch = (df.loc[X.index, "download_mb"] >= 1.0).map({True: "L", False: "S"})
    key3 = key + "\x1f" + ch
    pair3_n = key3.iloc[tr].groupby(key3.iloc[tr]).count()
    pair3_mean = y_tr.groupby(key3.iloc[tr]).mean()
    tgt3 = key3.iloc[tr].str.split("\x1f").str[0] + "\x1f" + key3.iloc[tr].str.split("\x1f").str[2]
    tgt3_mean = y_tr.groupby(tgt3).mean()
    ch_global = y_tr.groupby(ch.iloc[tr]).mean()
    global_mean = float(y_tr.mean())

    def eb3_pred(k):
        tgt, node, c = k.split("\x1f")
        n = int(pair3_n.get(k, 0))
        if n > 0:
            return (n * float(pair3_mean[k]) + K_SHRINK * float(tgt3_mean.get(tgt + "\x1f" + c, global_mean))) / (n + K_SHRINK)
        tk = tgt + "\x1f" + c
        tn = int((tgt3 == tk).sum())
        if tn > 0:
            return (tn * float(tgt3_mean[tk]) + K_SHRINK * global_mean) / (tn + K_SHRINK)
        return float(ch_global.get(c, global_mean))

    pred_eb3 = np.array([eb3_pred(k) for k in key3.iloc[te]])
    pred_g = np.full(len(y_te), global_mean)
    is_large = (key3.iloc[te].str.split("\x1f").str[2] == "L").values

    logger.info("")
    logger.info(f"{'预测器':<28}{'整体RMSE':>10}{'小流量':>10}{'大流量':>10}")
    for name, p in [("全局均值", pred_g), ("EB 查表(3维分层)", pred_eb3), ("LightGBM 先验", pred_lgb)]:
        r_all = rmse(y_te, p)
        r_s = rmse(y_te[~is_large], p[~is_large]) if (~is_large).any() else float("nan")
        r_l = rmse(y_te[is_large], p[is_large]) if is_large.any() else float("nan")
        logger.info(f"{name:<28}{r_all:>10.4f}{r_s:>10.4f}{r_l:>10.4f}")


if __name__ == "__main__":
    main()
