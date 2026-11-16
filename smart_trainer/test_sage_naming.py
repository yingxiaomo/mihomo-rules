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
        self.assertEqual(trainer.FAILURE_MIN_CLASS_ATTEMPTS,
                         {"train": 8, "early_stop": 8,
                          "calibration": 3, "final_test": 3})
        self.assertEqual(trainer.ALGORITHM_SIGNATURE,
                         "ecc5aa098edcc9d02b73cf16efe63b5ef9079caaf9bbd08b5daeccd7082a29f5")
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
                mock.patch.object(trainer, "preflight_training_tables",
                                  return_value=dict.fromkeys(trainer.DIMENSIONS, {})), \
                mock.patch.object(trainer, "train_dimension",
                                  return_value=("model", {"_evidence": {}})), \
                mock.patch.object(trainer, "save_bundle") as save, \
                mock.patch.object(trainer, "save_training_evidence"):
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
            "smart_trainer/sage_workflow_notify.py",
            "smart_trainer/requirements-sage.txt",
            ".github/workflows/train-sage-prior.yml",
            "python -m unittest discover -s smart_trainer -p 'test_*.py' -v",
            "RELEASE_TAG: sage-prior-model-${{ github.run_number }}",
            "SAGE_CONSUMER_SHA: 9c92dbf80c8997bbfe1a2f4e986aa45f75f3a786",
            "ref: ${{ github.sha }}", "repository: yingxiaomo/mihomo",
            '${REMOTE}/sage-native', "sage-data", "SagePrior.json",
            "SagePrior.evidence.json", "go-version: '1.26.5'",
            "cache-dependency-path: go-consumer/go.sum",
            'gh release edit "$RELEASE_TAG" --notes "$RELEASE_NOTES" --draft=false --latest=false',
            'gh release create "$RELEASE_TAG" --title "Sage Dimensional Priors" --notes "$RELEASE_NOTES" --target "$GITHUB_SHA" --draft',
            "SAGE_PRIOR_BUNDLE=prior-integration/SagePrior.json",
            "TestSagePriorExternalBundle", "SagePrior-v1.zip",
            "SagePrior-v1.zip.sha256", "name: sage-prior-bundle",
            "needs: train_and_deploy", "PIPELINE_RESULT:", "if: always()",
            "Record final result for independent notification", "FAILED_STEP:",
            "--status success", "--status failure",
            "gh release download", "persist-credentials: false",
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16",
            "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
        ):
            with self.subTest(literal=literal):
                self.assertIn(literal, workflow)
        self.assertNotRegex(workflow, "adaptive|Adaptive|ADAPTIVE")
        self.assertNotIn("--branch dev-sage", workflow)
        self.assertNotIn("ref: main", workflow)


if __name__ == "__main__":
    unittest.main()
