import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from smart_trainer.compare_prior_baseline import online_state_baseline
from smart_trainer.analyze_regret import unambiguous_success_mask

from smart_trainer.train_pld import (
    PLD_FEATURE_ORDER,
    apply_embedded_transforms,
    build_plf_features,
    chronological_split_indices,
    drop_legacy_poisoned_samples,
    fnv1a_hash,
    order_pld_events,
    update_reward_ema,
)


def row(
    *,
    ts,
    reward,
    download_mb=0.1,
    max_kbps=100.0,
    group="group-a",
    target="example",
    node="节点A",
    host="www.example.com",
    success=None,
    failure=0,
    duration_sec=1.0,
):
    if success is None:
        success = ts
    return {
        "id": ts,
        "ts": ts,
        "timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Shanghai").isoformat(),
        "asn_raw": "AS13335 Cloudflare",
        "is_udp": 0,
        "host_raw": host,
        "ip_raw": "1.1.1.1",
        "geoip_raw": "US|CA",
        "port_raw": "443",
        "target": target,
        "group_name": group,
        "node_type": 8.0,
        "node_delay": 120.0,
        "node_name": node,
        "reward": reward,
        "success": success,
        "failure": failure,
        # Collector stores these legacy feature columns after math.Log1p.
        "download_mb": math.log1p(download_mb),
        "maxdownloadrate_kb": math.log1p(max_kbps),
        "duration_minutes": math.log1p(duration_sec / 60.0),
    }


class FeatureParityTests(unittest.TestCase):
    def test_feature_order_matches_kernel_abi(self):
        expected = [
            "asn_feature",
            "is_udp",
            "hour_sin",
            "hour_cos",
            "is_peak",
            "domain_feature",
            "geo_feature",
            "port_feature",
            "traffic_class",
            "asn_hash",
            "host_hash",
            "ip_hash",
            "node_reward_ema",
            "node_sample_count",
            "node_hash",
            "node_delay",
            "node_reward_var",
            "target_hash",
            "node_type",
            "group_hash",
            "domain_traffic_level",
            "reserved_21",
            "reserved_22",
            "reserved_23",
            "reserved_24",
            "reserved_25",
            "reserved_26",
            "reserved_27",
            "reserved_28",
            "reserved_29",
        ]
        self.assertEqual(PLD_FEATURE_ORDER, expected)

    def test_fnv_uses_utf8_bytes_like_go(self):
        self.assertEqual(fnv1a_hash("节点A", 1000), 741.0)
        self.assertEqual(fnv1a_hash("策略组", 200), 55.0)
        self.assertEqual(fnv1a_hash("hello", 1000), 724.0)

    def test_geo_uses_first_collector_country(self):
        frame = order_pld_events(pd.DataFrame([row(ts=1, reward=0.5)]))
        features = build_plf_features(frame)
        self.assertEqual(features.loc[0, "geo_feature"], 7)  # US

    def test_online_features_are_pre_event_and_decay_matches_go(self):
        first_mean, first_var, first_count = update_reward_ema(0.0, 0.0, 0, 0.5)
        second_mean, second_var, second_count = update_reward_ema(
            first_mean, first_var, first_count, 0.9
        )
        third_mean, _, _ = update_reward_ema(second_mean, second_var, second_count, 0.9)
        frame = order_pld_events(
            pd.DataFrame(
                [
                    row(ts=3, reward=third_mean),
                    row(ts=1, reward=first_mean),
                    row(ts=2, reward=second_mean),
                ]
            )
        )
        features = build_plf_features(frame)
        self.assertEqual(features.loc[0, "node_reward_ema"], 0.0)
        self.assertEqual(features.loc[0, "node_sample_count"], 0.0)
        self.assertAlmostEqual(features.loc[1, "node_reward_ema"], 0.5)
        self.assertAlmostEqual(features.loc[1, "node_sample_count"], math.log1p(1))

        self.assertAlmostEqual(features.loc[2, "node_reward_ema"], second_mean)
        self.assertAlmostEqual(features.loc[2, "node_reward_var"], second_var / 2.0)
        self.assertAlmostEqual(features.loc[2, "node_sample_count"], math.log1p(second_count))
        # Runtime refreshes the target cache after feature construction: one-selection lag.
        self.assertEqual(features["domain_traffic_level"].tolist(), [0.0, 0.0, 1.0])

    def test_future_reward_cannot_change_current_or_past_features(self):
        original = order_pld_events(
            pd.DataFrame([row(ts=1, reward=0.2), row(ts=2, reward=0.4), row(ts=3, reward=0.6)])
        )
        changed = original.copy()
        changed.loc[2, "reward"] = -2.0
        left = build_plf_features(original)[PLD_FEATURE_ORDER]
        right = build_plf_features(changed)[PLD_FEATURE_ORDER]
        pd.testing.assert_frame_equal(left, right)

    def test_group_scope_and_failure_dual_channel(self):
        scoped = order_pld_events(
            pd.DataFrame(
                [
                    row(ts=1, reward=0.5, group="a"),
                    row(ts=2, reward=0.8, group="b"),
                ]
            )
        )
        scoped_features = build_plf_features(scoped)
        self.assertEqual(scoped_features["node_sample_count"].tolist(), [0.0, 0.0])

        dual = order_pld_events(
            pd.DataFrame(
                [
                    row(ts=1, reward=0.5, download_mb=0.1),
                    row(
                        ts=2,
                        reward=update_reward_ema(0.5, 0.0, 1, -2.0)[0],
                        download_mb=0.0,
                        success=1,
                        failure=1,
                    ),
                    row(
                        ts=3,
                        reward=0.8,
                        download_mb=5.0,
                        host="r1.googlevideo.com",
                        success=2,
                        failure=1,
                    ),
                ]
            )
        )
        dual_features = build_plf_features(dual)
        # The failure populated the large channel; inference clamps its -2 EMA to 0.
        self.assertAlmostEqual(dual_features.loc[2, "node_sample_count"], math.log1p(1))
        self.assertEqual(dual_features.loc[2, "node_reward_ema"], 0.0)
        self.assertEqual(dual_features.loc[2, "domain_traffic_level"], 3.0)

    def test_large_partial_success_does_not_poison_small_channel(self):
        frame = order_pld_events(
            pd.DataFrame(
                [
                    row(ts=1, reward=0.8, download_mb=0.1, success=1, failure=0),
                    row(
                        ts=2,
                        reward=0.4,
                        download_mb=5.0,
                        duration_sec=1.0,
                        success=1,
                        failure=1,
                    ),
                    row(ts=3, reward=0.8, download_mb=0.1, success=2, failure=1),
                ]
            )
        )
        features = build_plf_features(frame)
        self.assertAlmostEqual(features.loc[2, "node_reward_ema"], 0.8)
        self.assertAlmostEqual(features.loc[2, "node_sample_count"], math.log1p(1))


class SplitAndTransformTests(unittest.TestCase):
    def test_success_classification_uses_counters_not_reward_ema(self):
        frame = pd.DataFrame(
            {
                "group_name": ["g"] * 4,
                "target": ["t"] * 4,
                "node_name": ["n"] * 4,
                "success": [1, 1, 2, 3],
                "failure": [0, 1, 1, 2],
                # A mature channel can remain positive after a hard failure.
                "reward": [0.8, 0.5, 0.6, 0.4],
            }
        )
        self.assertEqual(
            unambiguous_success_mask(frame).tolist(), [True, False, True, False]
        )

    def test_baseline_uses_same_causal_online_channel_state(self):
        features = pd.DataFrame(
            {"node_reward_ema": [0.1, 0.8], "node_sample_count": [0.0, math.log1p(3)]}
        )
        np.testing.assert_allclose(online_state_baseline(features, 0.4), [0.4, 0.8])

    def test_mixed_sqlite_csv_rows_use_each_rows_real_time(self):
        mixed = pd.DataFrame(
            [
                {**row(ts=100, reward=0.5), "timestamp": "1970-01-01T00:01:40Z"},
                {**row(ts=200, reward=-2.0, download_mb=5.0), "ts": np.nan,
                 "timestamp": "2030-01-01T00:00:00Z"},
                {**row(ts=50, reward=0.4), "ts": np.nan,
                 "timestamp": "1970-01-01T00:00:50Z"},
            ]
        )
        ordered = order_pld_events(mixed)
        self.assertEqual(ordered["_event_time"].tolist()[:2], [50.0, 100.0])
        # The 2030 CSV failure is after the legacy cutoff and must be retained.
        self.assertEqual(len(drop_legacy_poisoned_samples(mixed)), 3)

    def test_chronological_split_uses_future_tail(self):
        ordered = order_pld_events(pd.DataFrame([row(ts=t, reward=0.5) for t in [5, 1, 4, 2, 3]]))
        train, test = chronological_split_indices(ordered["_event_time"])
        self.assertEqual(train.tolist(), [0, 1, 2, 3])
        self.assertEqual(test.tolist(), [4])
        self.assertLessEqual(ordered.iloc[train]["ts"].max(), ordered.iloc[test]["ts"].min())

    def test_split_rejects_unsorted_events(self):
        with self.assertRaises(ValueError):
            chronological_split_indices(pd.Series([1.0, 3.0, 2.0]))

    def test_embedded_robust_transform_is_applied(self):
        frame = pd.DataFrame(np.zeros((2, len(PLD_FEATURE_ORDER))), columns=PLD_FEATURE_ORDER)
        frame.iloc[:, 13] = [3.0, 7.0]
        model_tail = """robust_features=13
robust_center=3
robust_scale=2
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PriorModel.bin"
            path.write_text(model_tail, encoding="utf-8")
            transformed = apply_embedded_transforms(frame, path)
        self.assertEqual(transformed.iloc[:, 13].tolist(), [0.0, 2.0])
        self.assertEqual(frame.iloc[:, 13].tolist(), [3.0, 7.0])


if __name__ == "__main__":
    unittest.main()
