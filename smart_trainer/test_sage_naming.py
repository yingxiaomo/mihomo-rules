"""Literal Sage ABI/name regressions, independent of trainer fixture constants."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# The fallback makes the pre-rename red run exercise the real old reader.
MODULE = (
    "smart_trainer.train_sage_prior"
    if (ROOT / "smart_trainer/train_sage_prior.py").is_file()
    else "smart_trainer.train_adaptive_prior"
)
trainer = importlib.import_module(MODULE)


def write_literal_ready(root, run="run", sequence=1, part=1,
                        source="sage-native-events-v1",
                        header_feature="sage-prior-native-features-v1",
                        event_feature="sage-prior-native-features-v1",
                        prefix="sage-native-v1"):
    """One complete file; deliberately never copies constants from the reader."""
    rows = [
        {"event_type": "file_header", "source_schema": source,
         "feature_schema": header_feature, "run_id": run},
        {"event_type": "decision", "source_schema": source,
         "feature_schema": event_feature, "run_id": run,
         "attempt_id": f"{run}:a{sequence}", "event_id": f"{run}:{sequence}",
         "event_seq": sequence, "decision_unix_nano": sequence,
         "observed_unix_nano": sequence + 1,
         "features": [0.0, 1.0, 6.0, 2.0, 3.0, 4.0, 5.0, 3.0]},
        {"event_type": "file_footer", "source_schema": source,
         "run_id": run, "dropped_control": 0, "dropped_segment": 0,
         "incomplete_attempts": 0, "written_events": 1,
         "first_event_seq": sequence, "last_event_seq": sequence},
    ]
    path = Path(root) / f"{prefix}.{run}.{part}.1.jsonl.ready"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8", newline="\n")
    return path


class SageLiteralContractTests(unittest.TestCase):
    def test_literal_sage_schemas_and_numeric_version(self):
        self.assertEqual(trainer.SCHEMA_VERSION, 2)
        self.assertEqual(trainer.SOURCE_SCHEMA, "sage-native-events-v1")
        self.assertEqual(trainer.FEATURE_SCHEMA, "sage-prior-native-features-v1")

    def test_literal_sage_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_literal_ready(tmp)
            frame = trainer.load_native_events(tmp)
            self.assertEqual(frame.event_id.tolist(), ["run:1"])
            self.assertEqual(frame.source_schema.tolist(), ["sage-native-events-v1"])

    def test_old_source_is_rejected_without_feature_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_literal_ready(tmp, source="adaptive-native-events-v1")
            with self.assertRaisesRegex(ValueError, "source_schema 不兼容"):
                trainer.load_native_events(tmp)

    def test_wholly_old_adaptive_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_literal_ready(tmp, source="adaptive-native-events-v1",
                                header_feature="adaptive-prior-native-features-v1",
                                event_feature="adaptive-prior-native-features-v1",
                                prefix="adaptive-native-v1")
            with self.assertRaisesRegex(ValueError, "source_schema 不兼容"):
                trainer.load_native_events(tmp)

    def test_old_header_feature_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_literal_ready(tmp, header_feature="adaptive-prior-native-features-v1")
            with self.assertRaisesRegex(ValueError, "header 无效"):
                trainer.load_native_events(tmp)

    def test_old_event_feature_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_literal_ready(tmp, event_feature="adaptive-prior-native-features-v1")
            with self.assertRaisesRegex(ValueError, "事件 ABI 无效"):
                trainer.load_native_events(tmp)

    def test_sage_markers_exclude_entire_run_not_other_runs(self):
        for suffix in ("failed", "recovered"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                write_literal_ready(tmp, run="damaged")
                write_literal_ready(tmp, run="damaged", sequence=2, part=2)
                write_literal_ready(tmp, run="healthy")
                nested = Path(tmp) / "nested"
                nested.mkdir()
                (nested / f"sage-native-v1.damaged.3.1.jsonl.{suffix}").touch()
                frame = trainer.load_native_events(tmp)
                self.assertEqual(frame.event_id.tolist(), ["healthy:1"])
                self.assertEqual(frame.run_id.tolist(), ["healthy"])

    def test_sage_marker_only_run_leaves_no_training_data(self):
        for suffix in ("failed", "recovered"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                write_literal_ready(tmp)
                (Path(tmp) / f"sage-native-v1.run.2.1.jsonl.{suffix}").touch()
                with self.assertRaisesRegex(ValueError, "未找到 Sage 原生"):
                    trainer.load_native_events(tmp)

    def test_old_marker_is_not_silently_reinterpreted(self):
        for suffix in ("failed", "recovered"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                write_literal_ready(tmp)
                (Path(tmp) / f"adaptive-native-v1.run.2.1.jsonl.{suffix}").touch()
                with self.assertRaisesRegex(ValueError, "无法识别损坏采集文件"):
                    trainer.load_native_events(tmp)

    def test_pld_csv_is_not_training_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "smart_samples.csv").write_text(
                "id,success,reward\n1,1,0.5\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "原生"):
                trainer.load_native_events(tmp)

    def test_pld_row_disguised_as_ready_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sage-native-v1.run.1.1.jsonl.ready").write_text(
                '{"id":1,"success":1,"reward":0.5}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_schema 不兼容"):
                trainer.load_native_events(tmp)

    def test_four_dimensions_and_eight_features_are_literal(self):
        self.assertEqual(trainer.DIMENSIONS,
                         ("connect", "response", "throughput", "failure"))
        self.assertEqual(trainer.FEATURE_ORDER,
                         ["is_udp", "is_tcp", "port_log", "asn_hash", "host_hash",
                          "ip_hash", "group_hash", "node_type"])

    def test_training_parameters_are_frozen(self):
        self.assertEqual(trainer.MAX_PRIOR_CONFIDENCE, 0.35)
        self.assertEqual((trainer.MIN_TRAIN_ROWS, trainer.MIN_VALID_ROWS,
                          trainer.MIN_CONFIDENCE_ROWS), (20, 5, 20))
        self.assertEqual(trainer.PARTITIONS,
                         ("train", "early_stop", "calibration", "final_test"))
        self.assertEqual(trainer.MODEL_PARAMS, {
            "verbosity": -1, "learning_rate": 0.035, "num_leaves": 24,
            "min_data_in_leaf": 12, "feature_fraction": 0.9,
            "bagging_fraction": 0.9, "bagging_freq": 1,
            "lambda_l1": 0.1, "lambda_l2": 0.5, "seed": 20261115,
            "feature_fraction_seed": 20261115, "bagging_seed": 20261115,
            "data_random_seed": 20261115, "deterministic": True,
            "force_col_wise": True, "num_threads": 1,
        })

    def test_cli_default_data_and_bundle_names(self):
        # Stop at the IO boundary while executing the real argument parser.
        with mock.patch.object(trainer, "load_native_events", side_effect=RuntimeError("stop")) as load:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                trainer.run_training([])
            self.assertEqual(load.call_args.args[0],
                             ROOT / "smart_trainer/sage-data")
        with mock.patch.object(trainer, "load_native_events", return_value=object()), \
                mock.patch.object(trainer, "build_training_tables",
                                  return_value=("attempts", dict.fromkeys(
                                      ("connect", "response", "throughput", "failure")))), \
                mock.patch.object(trainer, "train_dimension", return_value=("model", {})), \
                mock.patch.object(trainer, "save_bundle") as save:
            trainer.run_training([])
            self.assertEqual(save.call_args.args[0],
                             ROOT / "smart_trainer/SagePrior.json")


class SageRepositoryNamingTests(unittest.TestCase):
    def test_old_module_and_paths_do_not_exist(self):
        self.assertEqual(MODULE, "smart_trainer.train_sage_prior")
        self.assertIsNone(importlib.util.find_spec("smart_trainer.train_adaptive_prior"))
        for old, new in (
            ("smart_trainer/train_adaptive_prior.py", "smart_trainer/train_sage_prior.py"),
            ("smart_trainer/test_train_adaptive_prior.py", "smart_trainer/test_train_sage_prior.py"),
            ("smart_trainer/requirements-adaptive.txt", "smart_trainer/requirements-sage.txt"),
            (".github/workflows/train-adaptive-prior.yml", ".github/workflows/train-sage-prior.yml"),
        ):
            with self.subTest(path=new):
                self.assertFalse((ROOT / old).exists())
                self.assertTrue((ROOT / new).is_file())

    def test_workflow_uses_literal_sage_names_and_original_remote(self):
        new = ROOT / ".github/workflows/train-sage-prior.yml"
        workflow = (new if new.exists() else ROOT / ".github/workflows/train-adaptive-prior.yml").read_text(encoding="utf-8")
        for literal in (
            "Train and Deploy Sage Dimensional Priors",
            "smart_trainer/train_sage_prior.py",
            "smart_trainer/test_train_sage_prior.py",
            "smart_trainer/requirements-sage.txt",
            ".github/workflows/train-sage-prior.yml",
            "smart_trainer.test_train_sage_prior",
            "RELEASE_TAG: sage-prior-model-${{ github.run_number }}",
            "SAGE_REPO_URL: https://github.com/yingxiaomo/mihomo",
            '${REMOTE}/sage-native', "sage-data", "SagePrior.json",
            '--branch dev-sage "$SAGE_REPO_URL" /tmp/mmihos-sage',
            "go-version: '1.26.5'", "/tmp/mmihos-sage/go.sum",
            'gh release edit "$RELEASE_TAG" --draft=false --latest=false',
            "SAGE_PRIOR_BUNDLE=/tmp/mmihos-sage/prior-integration/SagePrior.json",
            "TestSagePriorExternalBundle", "SagePrior-v1.zip",
            "SagePrior-v1.zip.sha256", "name: sage-prior-bundle",
        ):
            with self.subTest(literal=literal):
                self.assertIn(literal, workflow)
        self.assertNotRegex(workflow, "adaptive|Adaptive|ADAPTIVE")

    def test_renamed_files_preserve_baseline_except_explicit_release_wiring(self):
        # Baseline 67bd61d; only four explicit deployment-wiring differences.
        # Algorithms, dependencies and every other workflow byte stay frozen.
        expected = {
            "smart_trainer/train_sage_prior.py": "da9fa336cf2cc758b4723bd5fd1781774d9fe751a80846805072268db89957e6",
            "smart_trainer/test_train_sage_prior.py": "2bcfc8e26619f75f7ae99cf640177aca97df7c04c6d505516bb87021b86d7750",
            "smart_trainer/requirements-sage.txt": "38d286fe59ff2c5e034aa1b91bc294f6f172a2458df6a981dca42ad61b6cf0d8",
            ".github/workflows/train-sage-prior.yml": "5f3d2435ee9ba106ab8c3336d11b39cc6cff1867de80cde84c99a78bdcbae122",
        }
        for name, digest in expected.items():
            with self.subTest(path=name):
                path = ROOT / name
                self.assertTrue(path.is_file())
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, "adaptive|Adaptive|ADAPTIVE")
                if name == ".github/workflows/train-sage-prior.yml":
                    for released, baseline in (
                        ("--branch dev-sage ", "--branch sage-v1 "),
                        ("go-version: '1.26.5'", "go-version-file: /tmp/mmihos-sage/go.mod"),
                        ("--draft=false --latest=false", "--draft=false --latest"),
                        ("SAGE_PRIOR_BUNDLE=/tmp/mmihos-sage/prior-integration/SagePrior.json",
                         "SAGE_PRIOR_BUNDLE=prior-integration/SagePrior.json"),
                    ):
                        self.assertEqual(text.count(released), 1)
                        text = text.replace(released, baseline)
                normalized = text.replace("sage", "adaptive").replace("Sage", "Adaptive").replace("SAGE", "ADAPTIVE")
                self.assertEqual(hashlib.sha256(normalized.encode("utf-8")).hexdigest(), digest)

    def test_pld_files_match_frozen_baseline_hashes(self):
        expected = {
            ".github/workflows/train-pld.yml": "23dc648c896d8f4dade277bb3b8a2e5260b3f3ee25b774f95c3c6149cd7cc3e1",
            "smart_trainer/train_pld.py": "9cd344a5c68b56e7815728ccedd052c0804cd6ad35c37075e3710c4e8d7b8331",
            "smart_trainer/test_train_pld.py": "a7628d7d0f4c33b31f0581635db3c90d97db05fe1f509fc0e3de3e7e00073f90",
            "smart_trainer/requirements.txt": "8ad552649eac0e05f1798ec28d098ff7350abb77d1a68139ea9ab0e3bfd2e6f3",
        }
        for name, digest in expected.items():
            with self.subTest(path=name):
                # Normalize checkout newlines only; no PLD content substitution.
                data = (ROOT / name).read_text(encoding="utf-8").encode("utf-8")
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
