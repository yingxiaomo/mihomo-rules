#!/usr/bin/env python3
"""Compare the PLD prior against the causal dual-channel online-state baseline.

Both models are evaluated on the same future 20% time slice. The baseline uses
the exact causal ``node_reward_ema`` feature available before each validation
event (already selected from the small/large channel by request context), and
falls back to the training global mean while that channel is cold. Therefore it
receives the same prior validation-period feedback as LightGBM; a frozen
training-period group mean would be an unfair weaker comparator. The LightGBM
path applies the RobustScaler metadata embedded in PriorModel.bin.
"""

import argparse
from pathlib import Path

import lightgbm as lgb
import numpy as np

try:
    from .train_pld import (
        apply_embedded_transforms,
        chronological_split_indices,
        load_data,
        preprocess_pld,
    )
except ImportError:  # direct script execution
    from train_pld import (
        apply_embedded_transforms,
        chronological_split_indices,
        load_data,
        preprocess_pld,
    )

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_MODEL = PROJECT_ROOT / "PriorModel.bin"


def weighted_metrics(y_true, y_pred, weights):
    y = np.asarray(y_true, dtype=float)
    pred = np.asarray(y_pred, dtype=float)
    w = np.asarray(weights, dtype=float)
    return {
        "rmse": float(np.sqrt(np.average((y - pred) ** 2, weights=w))),
        "mae": float(np.average(np.abs(y - pred), weights=w)),
    }


def online_state_baseline(features, cold_fallback):
    """Predict from the same pre-event dual-channel state exposed to the model."""
    ema = features["node_reward_ema"].to_numpy(dtype=float)
    observed = features["node_sample_count"].to_numpy(dtype=float) > 0.0
    return np.where(observed, ema, float(cold_fallback))


def print_metrics(name, y_true, y_pred, weights, mask):
    count = int(mask.sum())
    if count == 0:
        print(f"  {name:<12} 0 条")
        return
    metrics = weighted_metrics(
        np.asarray(y_true)[mask], np.asarray(y_pred)[mask], np.asarray(weights)[mask]
    )
    print(
        f"  {name:<12} {count:>6} 条 | "
        f"RMSE {metrics['rmse']:.6f} | MAE {metrics['mae']:.6f}"
    )


def main():
    parser = argparse.ArgumentParser(description="Compare PLD prior and dual-channel online baseline")
    parser.add_argument("--data_dir", type=Path, default=SCRIPT_DIR / "data")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()

    if not args.model.is_file():
        raise FileNotFoundError(f"找不到模型: {args.model}")

    df = load_data(args.data_dir)
    X, y, w, groups, event_times = preprocess_pld(df)
    train_idx, test_idx = chronological_split_indices(event_times)

    X_test = X.iloc[test_idx]
    y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
    w_train, w_test = w.iloc[train_idx], w.iloc[test_idx]
    groups_train, groups_test = groups.iloc[train_idx], groups.iloc[test_idx]

    model = lgb.Booster(model_file=str(args.model))
    transformed_test = apply_embedded_transforms(X_test, args.model)
    prior_pred = model.predict(transformed_test, num_iteration=model.best_iteration)

    global_mean = float(np.average(np.asarray(y_train), weights=np.asarray(w_train)))
    baseline_pred = online_state_baseline(X_test, global_mean)

    seen_mask = groups_test.astype(str).isin(set(groups_train.astype(str))).to_numpy()
    regimes = {
        "总体": np.ones(len(test_idx), dtype=bool),
        "已见组合": seen_mask,
        "未见组合": ~seen_mask,
    }

    print(f"训练 {len(train_idx)} 条，未来验证 {len(test_idx)} 条")
    print(f"模型: {args.model}")
    print("\nLightGBM prior（已应用模型内嵌变换）")
    for name, mask in regimes.items():
        print_metrics(name, y_test, prior_pred, w_test, mask)

    print("\n因果双通道在线 Reward EMA baseline（冷启动回退训练期全局均值）")
    for name, mask in regimes.items():
        print_metrics(name, y_test, baseline_pred, w_test, mask)

    prior_all = weighted_metrics(y_test, prior_pred, w_test)
    baseline_all = weighted_metrics(y_test, baseline_pred, w_test)
    rmse_gain = baseline_all["rmse"] - prior_all["rmse"]
    mae_gain = baseline_all["mae"] - prior_all["mae"]
    print("\n差值（正数 = LightGBM 更好）")
    print(f"  RMSE 改善: {rmse_gain:.6f}")
    print(f"  MAE  改善: {mae_gain:.6f}")


if __name__ == "__main__":
    main()
