import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from smart_trainer.train_adaptive_prior import (
    FEATURE_ORDER,
    MAX_PRIOR_CONFIDENCE,
    _weighted_median,
    build_features,
    build_training_tables,
    infer_connection_outcomes,
    order_events,
    save_bundle,
    train_dimension,
)


def row(
    index: int,
    *,
    success: int,
    failure: int,
    udp: int = 0,
    connect_ms: float = 120.0,
    kbps: float = 2048.0,
    download_mb: float = 2.0,
    node_delay: float = 80.0,
):
    return {
        "id": index,
        "ts": 1_700_000_000 + index,
        "timestamp": "",
        "success": success,
        "failure": failure,
        "connect_time": math.log1p(connect_ms),
        "maxdownloadrate_kb": math.log1p(kbps),
        "download_mb": math.log1p(download_mb),
        "sample_weight": 1.0 + math.log1p(download_mb),
        "is_udp": udp,
        "asn_raw": "AS13335 Cloudflare",
        "host_raw": "r1.googlevideo.com",
        "ip_raw": "1.1.1.1",
        "geoip_raw": "US|CA",
        "port_raw": "443",
        "target": ".googlevideo.com",
        "group_name": "adaptive-main",
        "node_name": "节点A",
        "node_delay": node_delay,
        "node_type": 3.0,
    }


class AdaptivePriorFeatureTests(unittest.TestCase):
    def test_feature_abi_and_utf8_hashes(self):
        self.assertEqual(len(FEATURE_ORDER), 8)
        frame = order_events(pd.DataFrame([row(1, success=1, failure=0)]))
        features = build_features(frame)
        self.assertEqual(features.columns.tolist(), FEATURE_ORDER)
        self.assertEqual(features.loc[0, "asn_hash"], 66.0)
        self.assertEqual(features.loc[0, "host_hash"], 464.0)
        self.assertEqual(features.loc[0, "node_type"], 3.0)

        legacy_proxy = frame.copy()
        legacy_proxy.loc[0, "node_type"] = 15.0
        self.assertEqual(build_features(legacy_proxy).loc[0, "node_type"], 13.0)
        legacy_proxy.loc[0, "node_type"] = 39.0
        self.assertTrue(math.isnan(build_features(legacy_proxy).loc[0, "node_type"]))

    def test_current_outcome_is_not_a_feature(self):
        left = order_events(pd.DataFrame([row(1, success=1, failure=0, connect_ms=20, kbps=10)]))
        right = order_events(pd.DataFrame([row(1, success=0, failure=1, connect_ms=5000, kbps=900000)]))
        pd.testing.assert_frame_equal(build_features(left), build_features(right))

    def test_unambiguous_counter_deltas_and_resets(self):
        frame = pd.DataFrame(
            [
                row(1, success=1, failure=0),
                row(2, success=1, failure=1),
                row(3, success=2, failure=2),  # both changed: ambiguous
                row(4, success=0, failure=0),  # reset: baseline only
                row(5, success=1, failure=0),
            ]
        )
        outcomes = infer_connection_outcomes(frame)
        self.assertEqual(outcomes.iloc[0], 0.0)
        self.assertEqual(outcomes.iloc[1], 1.0)
        self.assertTrue(math.isnan(outcomes.iloc[2]))
        self.assertTrue(math.isnan(outcomes.iloc[3]))
        self.assertEqual(outcomes.iloc[4], 0.0)

    def test_sparse_counter_gap_and_bad_timestamp_are_rejected(self):
        sparse = pd.DataFrame([
            row(1, success=1, failure=0),
            row(2, success=6, failure=1),
        ])
        outcomes = infer_connection_outcomes(sparse)
        self.assertTrue(math.isnan(outcomes.iloc[1]))

        broken = pd.DataFrame([row(1, success=1, failure=0)])
        broken.loc[0, "ts"] = float("nan")
        broken.loc[0, "timestamp"] = "bad"
        with self.assertRaisesRegex(ValueError, "timestamp"):
            order_events(broken)

    def test_three_labels_are_independent_and_udp_has_no_throughput_label(self):
        frame = pd.DataFrame(
            [
                row(1, success=1, failure=0, connect_ms=10, kbps=100),
                row(2, success=1, failure=1, connect_ms=9000, kbps=90000),
                row(3, success=2, failure=1, udp=1, connect_ms=50, kbps=5000),
            ]
        )
        _, _, tables = build_training_tables(frame)
        self.assertEqual(tables["connect"]["mask"].tolist(), [True, False, True])
        self.assertEqual(tables["throughput"]["mask"].tolist(), [True, True, False])
        self.assertEqual(tables["failure"]["label"].tolist(), [0.0, 1.0, 0.0])
        self.assertNotEqual(
            float(tables["connect"]["label"].iloc[0]),
            float(tables["throughput"]["label"].iloc[0]),
        )


class AdaptivePriorTrainingTests(unittest.TestCase):
    def _training_frame(self):
        rows = []
        success = failure = 0
        for i in range(1, 181):
            failed = i % 7 == 0
            if failed:
                failure += 1
            else:
                success += 1
            item = row(
                i,
                success=success,
                failure=failure,
                connect_ms=60 + (i % 11) * 15,
                kbps=600 + (i % 17) * 200,
                download_mb=0.5 + (i % 9),
                node_delay=40 + (i % 13) * 10,
            )
            item["host_raw"] = f"cdn{i % 5}.example.com"
            rows.append(item)
        return pd.DataFrame(rows)

    def test_weighted_mae_baseline_uses_weighted_median(self):
        values = np.asarray([0.0, 10.0, 20.0, 30.0])
        weights = np.asarray([1.0, 1.0, 10.0, 1.0])
        self.assertEqual(_weighted_median(values, weights), 20.0)
        weighted_error = np.average(np.abs(values - 20.0), weights=weights)
        wrong_error = np.average(np.abs(values - np.median(values)), weights=weights)
        self.assertLess(weighted_error, wrong_error)

    def test_trains_three_models_and_writes_self_checking_manifest(self):
        ordered, features, tables = build_training_tables(self._training_frame())
        models, metadata = {}, {}
        for name in ("connect", "throughput", "failure"):
            models[name], metadata[name] = train_dimension(name, ordered, features, tables[name])
            self.assertGreater(models[name].best_iteration, 0)
            self.assertGreaterEqual(metadata[name]["confidence"], 0)
            self.assertLessEqual(metadata[name]["confidence"], MAX_PRIOR_CONFIDENCE)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "AdaptivePrior.json"
            manifest = save_bundle(output, ordered, models, metadata)
            parsed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(parsed, manifest)
            self.assertEqual(parsed["feature_order"], FEATURE_ORDER)
            self.assertEqual(set(parsed["dimensions"]), {"connect", "throughput", "failure"})
            self.assertEqual(parsed["source_schema"], "pld-bootstrap-v1")
            for name, dimension in parsed["dimensions"].items():
                model = output.parent / dimension["model"]
                self.assertTrue(model.is_file())
                self.assertEqual(len(dimension["sha256"]), 64)
                self.assertIn(dimension["sha256"][:16], model.name)
                self.assertEqual(dimension["output_transform"], "sigmoid" if name == "failure" else "identity")

    def test_training_is_deterministic_and_failed_generation_preserves_manifest(self):
        frame = self._training_frame()
        bundles = []
        trained = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for generation in ("one", "two"):
                ordered, features, tables = build_training_tables(frame)
                models, metadata = {}, {}
                for name in ("connect", "throughput", "failure"):
                    models[name], metadata[name] = train_dimension(name, ordered, features, tables[name])
                output = root / generation / "AdaptivePrior.json"
                manifest = save_bundle(output, ordered, models, metadata)
                payloads = {output.name: output.read_bytes()}
                for dimension in manifest["dimensions"].values():
                    model_path = output.parent / dimension["model"]
                    payloads[model_path.name] = model_path.read_bytes()
                bundles.append(payloads)
                trained.append((ordered, models, metadata))
            self.assertEqual(bundles[0], bundles[1])

            output = root / "atomic" / "AdaptivePrior.json"
            output.parent.mkdir(parents=True)
            output.write_text("old-manifest\n", encoding="utf-8")

            class BrokenModel:
                best_iteration = 1

                def save_model(self, *_args, **_kwargs):
                    raise RuntimeError("injected write failure")

            ordered, models, metadata = trained[0]
            broken = {**models, "throughput": BrokenModel()}
            with self.assertRaisesRegex(RuntimeError, "injected"):
                save_bundle(output, ordered, broken, metadata)
            self.assertEqual(output.read_text(encoding="utf-8"), "old-manifest\n")


if __name__ == "__main__":
    unittest.main()
