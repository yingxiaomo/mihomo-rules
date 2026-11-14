#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train the three independent bootstrap priors consumed by ``type: adaptive``.

This is intentionally separate from :mod:`train_pld`: PLD still predicts its
single reward label and keeps publishing ``PriorModel.bin``.  Adaptive predicts
three physical dimensions available to its scorer:

* TCP/UDP connect time, as ``log(milliseconds)``;
* TCP download throughput, as ``log(bits/second)``;
* connection failure probability.

Only signals available before a connection is selected are features.  In
particular, current-row connect time, transfer bytes/rate and cumulative outcome
counters are labels only and can never enter the feature matrix.

The existing PLD archive is used as a conservative bootstrap source: its
``connect_time`` is a post-settlement historical EMA and
``maxdownloadrate_kb`` is a per-connection peak, not Adaptive's exact live
connect/goodput observations.  The manifest therefore describes priors, never
online evidence, and the Go consumer caps their influence at 0.35.  The three
targets remain physically separate and are never reconstructed from PLD reward.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Tuple

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

try:  # package import in tests / direct script execution in Actions
    from smart_trainer.train_pld import (
        fnv1a_hash,
        load_data,
    )
except ModuleNotFoundError:  # pragma: no cover - direct invocation fallback
    from train_pld import (  # type: ignore
        fnv1a_hash,
        load_data,
    )

logger = logging.getLogger("adaptive-prior")

SCHEMA_VERSION = 1
FEATURE_SCHEMA = "adaptive-prior-bootstrap-features-v2"
SOURCE_SCHEMA = "pld-bootstrap-v1"
MAX_PRIOR_CONFIDENCE = 0.35
MIN_TRAIN_ROWS = 20
MIN_VALID_ROWS = 5
MIN_CONFIDENCE_ROWS = 20

# Keep in exact lockstep with adapter/outboundgroup/adaptive_prior.go.
# No node name or learned online state appears here: a same-named replacement
# must not inherit historical identity-specific evidence from the old PLD data.
FEATURE_ORDER = [
    "is_udp",
    "is_tcp",
    "port_log",
    "asn_hash",
    "host_hash",
    "ip_hash",
    "group_hash",
    "node_type",
]

MODEL_PARAMS = {
    "verbosity": -1,
    "learning_rate": 0.035,
    "num_leaves": 24,
    "min_data_in_leaf": 12,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "lambda_l1": 0.1,
    "lambda_l2": 0.5,
    "seed": 20261115,
    "feature_fraction_seed": 20261115,
    "bagging_seed": 20261115,
    "data_random_seed": 20261115,
    "deterministic": True,
    "force_col_wise": True,
    "num_threads": 1,
}


def _text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    value = str(value).strip()
    return "" if value.lower() == "nan" or value == "unknown" else value


def _country(value) -> str:
    text = _text(value)
    return text.split("|", 1)[0] if text else ""


def _numeric(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default).astype(float)


def order_events(df: pd.DataFrame) -> pd.DataFrame:
    ordered = df.copy()
    event_times = []
    for index, row in ordered.iterrows():
        numeric = pd.to_numeric(pd.Series([row.get("ts")]), errors="coerce").iloc[0]
        timestamp_text = _text(row.get("timestamp"))
        parsed = pd.to_datetime(timestamp_text, utc=True, errors="coerce") if timestamp_text else pd.NaT
        numeric_valid = bool(np.isfinite(numeric) and float(numeric) > 0)
        text_valid = not pd.isna(parsed)
        if timestamp_text and not text_valid:
            raise ValueError(f"样本 {index} 的 timestamp 无法解析")
        if not numeric_valid and not text_valid:
            raise ValueError(f"样本 {index} 缺少严格可解析的 ts/timestamp")
        text_seconds = float(parsed.timestamp()) if text_valid else math.nan
        if numeric_valid and text_valid and abs(float(numeric) - text_seconds) > 1.0:
            raise ValueError(f"样本 {index} 的 ts/timestamp 冲突")
        event_times.append(float(numeric) if numeric_valid else text_seconds)
    ordered["_event_time"] = event_times
    tie = _numeric(ordered["id"], 0.0) if "id" in ordered else pd.Series(np.arange(len(ordered)), index=ordered.index)
    ordered["_event_tie"] = tie
    return ordered.sort_values(["_event_time", "_event_tie"], kind="stable").reset_index(drop=True)


def infer_connection_outcomes(df: pd.DataFrame) -> pd.Series:
    """Recover an unambiguous per-row failure label from cumulative counters.

    A row where both success and failure changed is ambiguous under sampled
    collection and is excluded instead of guessed.  The first observation is
    accepted only when exactly one cumulative counter is non-zero.
    """
    result = pd.Series(np.nan, index=df.index, dtype=float)
    previous: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
    for pos, row in df.iterrows():
        key = (_text(row.get("group_name")), _text(row.get("target")), _text(row.get("node_name")))
        success = pd.to_numeric(pd.Series([row.get("success")]), errors="coerce").iloc[0]
        failure = pd.to_numeric(pd.Series([row.get("failure")]), errors="coerce").iloc[0]
        if not (np.isfinite(success) and np.isfinite(failure)):
            continue
        old = previous.get(key)
        previous[key] = (float(success), float(failure))
        if old is None:
            ds, dfail = float(success), float(failure)
        else:
            ds, dfail = float(success) - old[0], float(failure) - old[1]
            if ds < 0 or dfail < 0:  # counter reset: establish a new baseline
                continue
        # Sampled cumulative counters identify exactly one event only when the
        # total delta is one. Larger or mixed gaps are never guessed.
        if ds == 1 and dfail == 0:
            result.iloc[pos] = 0.0
        elif dfail == 1 and ds == 0:
            result.iloc[pos] = 1.0
    return result


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    is_udp = _numeric(df["is_udp"]).clip(0, 1)
    port = df["port_raw"]
    host = df["host_raw"].map(_text)
    asn = df["asn_raw"].map(_text)
    ip = df["ip_raw"].map(_text)
    group = df["group_name"].map(_text)

    features = pd.DataFrame(index=df.index)
    features["is_udp"] = is_udp
    features["is_tcp"] = 1.0 - is_udp

    features["port_log"] = np.log1p(pd.to_numeric(port, errors="coerce").fillna(0).clip(0, 65535))
    features["asn_hash"] = asn.map(lambda value: fnv1a_hash(value, 500))
    features["host_hash"] = host.map(lambda value: fnv1a_hash(value, 1000))
    features["ip_hash"] = ip.map(lambda value: fnv1a_hash(value, 10000))
    features["group_hash"] = group.map(lambda value: fnv1a_hash(value, 200))
    # The PLD producer inserted Smart/SmartPLD at enum slots 13/14. Map only
    # protocols that exist in the Adaptive consumer. In particular PLD Naive=39
    # has no counterpart here (Adaptive itself is 37), so it must be excluded
    # instead of colliding with Adaptive's enum value.
    legacy_type = _numeric(df["node_type"]).clip(0, 1024)
    legacy_to_adaptive = {
        **{value: value for value in range(13)},
        **{value: value - 2 for value in range(15, 39)},
    }
    features["node_type"] = legacy_type.map(legacy_to_adaptive)
    return features[FEATURE_ORDER]


def build_training_tables(df: pd.DataFrame):
    ordered = order_events(df)
    features = build_features(ordered)
    outcome = infer_connection_outcomes(ordered)

    connect_ms = np.expm1(_numeric(ordered["connect_time"]).clip(lower=0))
    throughput_bps = np.expm1(_numeric(ordered["maxdownloadrate_kb"]).clip(lower=0)) * 1024.0 * 8.0
    download_mb = np.expm1(_numeric(ordered["download_mb"]).clip(lower=0))
    is_udp = _numeric(ordered["is_udp"]).clip(0, 1).astype(bool)
    base_weight = _numeric(ordered["sample_weight"], 1.0).clip(0.001, 100.0) if "sample_weight" in ordered else pd.Series(1.0, index=ordered.index)

    tables = {
        "connect": {
            "label": np.log(connect_ms.clip(1, 30_000)),
            "mask": outcome.eq(0) & connect_ms.gt(0) & connect_ms.le(30000),
            "weight": pd.Series(1.0, index=ordered.index),
            "unit": "log_milliseconds",
        },
        "throughput": {
            "label": np.log(throughput_bps.clip(8_192, 1_000_000_000)),
            # Positive TCP payload remains useful even when the eventual
            # connection outcome is failure; this mirrors Adaptive's live
            # goodput observer and keeps the three labels independent.
            "mask": ~is_udp & download_mb.gt(0) & throughput_bps.ge(8192) & throughput_bps.le(1_000_000_000),
            "weight": base_weight,
            "unit": "log_bits_per_second",
        },
        "failure": {
            "label": outcome,
            "mask": outcome.notna(),
            "weight": pd.Series(1.0, index=ordered.index),
            "unit": "probability",
        },
    }
    return ordered, features, tables


def chronological_partitions(times: pd.Series):
    values = np.asarray(times, dtype=float)
    minimum = MIN_TRAIN_ROWS + 3 * MIN_VALID_ROWS
    if len(values) < minimum:
        raise ValueError(f"有效样本至少需要 {minimum} 条")
    if np.any(values[1:] < values[:-1]):
        raise ValueError("事件未按时间排序")
    train_end = max(MIN_TRAIN_ROWS, int(len(values) * 0.60))
    early_end = max(train_end + MIN_VALID_ROWS, int(len(values) * 0.75))
    calibration_end = max(early_end + MIN_VALID_ROWS, int(len(values) * 0.85))
    calibration_end = min(calibration_end, len(values) - MIN_VALID_ROWS)
    if early_end >= calibration_end:
        early_end = calibration_end - MIN_VALID_ROWS
    return (
        np.arange(0, train_end),
        np.arange(train_end, early_end),
        np.arange(early_end, calibration_end),
        np.arange(calibration_end, len(values)),
    )


def _confidence(model_error: float, baseline_error: float) -> float:
    if not (np.isfinite(model_error) and np.isfinite(baseline_error)) or baseline_error <= 1e-12:
        return 0.0
    return round(min(MAX_PRIOR_CONFIDENCE, max(0.0, 1.0 - model_error / baseline_error)), 6)


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not np.any(valid):
        raise ValueError("加权中位数至少需要一个有效正权重样本")
    values = values[valid]
    weights = weights[valid]
    order = np.argsort(values, kind="stable")
    values = values[order]
    weights = weights[order]
    index = int(np.searchsorted(np.cumsum(weights), weights.sum() * 0.5, side="left"))
    return float(values[min(index, len(values) - 1)])


def train_dimension(name: str, ordered: pd.DataFrame, features: pd.DataFrame, spec: dict):
    mask = spec["mask"] & features.notna().all(axis=1) & pd.Series(spec["label"], index=ordered.index).notna()
    X = features.loc[mask].reset_index(drop=True)
    y = pd.Series(spec["label"], index=ordered.index).loc[mask].astype(float).reset_index(drop=True)
    w = pd.Series(spec["weight"], index=ordered.index).loc[mask].astype(float).reset_index(drop=True)
    times = ordered.loc[mask, "_event_time"].reset_index(drop=True)
    train_idx, early_idx, calibration_idx, test_idx = chronological_partitions(times)

    objective = "binary" if name == "failure" else "regression_l2"
    metric = "binary_logloss" if name == "failure" else "l2"
    params = {**MODEL_PARAMS, "objective": objective, "metric": metric}
    train_set = lgb.Dataset(X.iloc[train_idx], label=y.iloc[train_idx], weight=w.iloc[train_idx], feature_name=FEATURE_ORDER)
    early_set = lgb.Dataset(X.iloc[early_idx], label=y.iloc[early_idx], weight=w.iloc[early_idx], reference=train_set, feature_name=FEATURE_ORDER)
    model = lgb.train(
        params,
        train_set,
        num_boost_round=600,
        valid_sets=[early_set],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )
    truth = y.iloc[test_idx].to_numpy(dtype=float)
    weights = w.iloc[test_idx].to_numpy(dtype=float)
    train_truth = y.iloc[train_idx].to_numpy(dtype=float)
    train_weights = w.iloc[train_idx].to_numpy(dtype=float)
    output_transform = "identity"
    calibration_scale = 1.0
    calibration_bias = 0.0
    if name == "failure":
        output_transform = "sigmoid"
        calibration_truth = y.iloc[calibration_idx].to_numpy(dtype=float)
        calibration_weights = w.iloc[calibration_idx].to_numpy(dtype=float)
        raw_calibration = np.asarray(
            model.predict(X.iloc[calibration_idx], num_iteration=model.best_iteration, raw_score=True),
            dtype=float,
        )
        calibrated = len(np.unique(calibration_truth)) == 2
        if calibrated:
            calibrator = LogisticRegression(C=1_000_000.0, solver="lbfgs", random_state=20261115)
            calibrator.fit(raw_calibration.reshape(-1, 1), calibration_truth, sample_weight=calibration_weights)
            calibration_scale = float(calibrator.coef_[0, 0])
            calibration_bias = float(calibrator.intercept_[0])
        raw_test = np.asarray(
            model.predict(X.iloc[test_idx], num_iteration=model.best_iteration, raw_score=True),
            dtype=float,
        )
        logits = np.clip(calibration_scale * raw_test + calibration_bias, -40.0, 40.0)
        pred = 1.0 / (1.0 + np.exp(-logits))
        baseline_value = float(np.average(train_truth, weights=w.iloc[train_idx]))
        model_error = float(np.average((truth - pred) ** 2, weights=weights))
        baseline_error = float(np.average((truth - baseline_value) ** 2, weights=weights))
        metric_name = "brier"
    else:
        calibrated = True
        pred = np.asarray(model.predict(X.iloc[test_idx], num_iteration=model.best_iteration), dtype=float)
        # Weighted MAE is minimized by the weighted median. Using an
        # unweighted median here can inflate the claimed improvement for the
        # throughput head, whose large transfers deliberately carry more
        # sample weight.
        baseline_value = _weighted_median(train_truth, train_weights)
        model_error = float(np.average(np.abs(truth - pred), weights=weights))
        baseline_error = float(np.average(np.abs(truth - baseline_value), weights=weights))
        metric_name = "mae_log"
    confidence = _confidence(model_error, baseline_error) if calibrated and len(test_idx) >= MIN_CONFIDENCE_ROWS else 0.0
    logger.info(
        "%s: train=%d valid=%d %s=%.6f baseline=%.6f confidence=%.3f",
        name, len(train_idx), len(test_idx), metric_name, model_error, baseline_error, confidence,
    )
    return model, {
        "unit": spec["unit"],
        "confidence": confidence,
        "train_samples": int(len(train_idx)),
        "early_stop_samples": int(len(early_idx)),
        "calibration_samples": int(len(calibration_idx)),
        "validation_samples": int(len(test_idx)),
        "validation_metric": metric_name,
        "validation_error": round(model_error, 9),
        "baseline_error": round(baseline_error, 9),
        "output_transform": output_transform,
        "calibration_scale": round(calibration_scale, 12),
        "calibration_bias": round(calibration_bias, 12),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_bundle(output: Path, ordered: pd.DataFrame, models: dict, metadata: dict) -> dict:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.stem
    dimensions = {}
    staging = Path(tempfile.mkdtemp(prefix=f".{stem}-", dir=output.parent))
    created = []
    try:
        for name in ("connect", "throughput", "failure"):
            staged_path = staging / f"{stem}-{name}.lgb"
            models[name].save_model(str(staged_path), num_iteration=models[name].best_iteration)
            digest = _sha256(staged_path)
            model_path = output.with_name(f"{stem}-{name}-{digest[:16]}.lgb")
            if model_path.exists():
                if _sha256(model_path) != digest:
                    raise ValueError(f"内容寻址模型冲突: {model_path.name}")
            else:
                os.replace(staged_path, model_path)
                created.append(model_path)
            dimensions[name] = {
                **metadata[name],
                "model": model_path.name,
                "sha256": digest,
            }

        max_event = int(float(ordered["_event_time"].max()))
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "feature_schema": FEATURE_SCHEMA,
            "source_schema": SOURCE_SCHEMA,
            "feature_order": FEATURE_ORDER,
            "generated_from_event_unix": max_event,
            "max_prior_confidence": MAX_PRIOR_CONFIDENCE,
            "dimensions": dimensions,
        }
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        temp_manifest = output.with_suffix(output.suffix + ".tmp")
        temp_manifest.write_text(payload, encoding="utf-8")
        os.replace(temp_manifest, output)
        return manifest
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def run_training(argv: Iterable[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Train mmihos Adaptive dimensional priors")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "AdaptivePrior.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=None)
    df = load_data(args.data_dir)
    ordered, features, tables = build_training_tables(df)
    models, metadata = {}, {}
    for name in ("connect", "throughput", "failure"):
        models[name], metadata[name] = train_dimension(name, ordered, features, tables[name])
    manifest = save_bundle(args.output, ordered, models, metadata)
    logger.info("Adaptive 分维度先验已保存: %s", args.output.resolve())
    return manifest


if __name__ == "__main__":
    run_training()
