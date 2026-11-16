"""Naming-only runtime checks; no real cloud, Telegram or service calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class RuntimeNamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sage-ops-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.shell = shutil.which("sh")
        self.assertIsNotNone(self.shell, "POSIX shell is required")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""), TG_TOKEN="", TG_CHAT_ID="")
        self.env.pop("https_proxy", None)
        self.env.pop("http_proxy", None)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def script(self, name):
        # The production file is not changed: absolute paths are remapped only
        # in this isolated test copy, including destructive cleanup targets.
        source = (HERE / name).read_text(encoding="utf-8")
        source = source.replace("/etc/nikki/run", (self.root / "home").as_posix())
        source = source.replace("/root/smart", (self.root / "smart").as_posix())
        source = source.replace("/root/.config/rclone/rclone.conf", (self.root / "rclone.conf").as_posix())
        source = source.replace("/tmp/sage-prior-v2.new", (self.root / "download").as_posix())
        source = source.replace("gdrive:smart_data/sage-native-v2", (self.root / "remote").as_posix())
        source = source.replace("/etc/init.d/nikki", "nikki")
        # No unexpected network endpoint can be contacted: curl always fails
        # unless a particular test supplies a fake response.
        self.stub("curl", "exit 91\n")
        self.stub("nikki", 'printf "unexpected service call\\n" >&2\nexit 92\n')
        path = self.root / name
        path.write_text(source, encoding="utf-8", newline="\n")
        return path

    def run_script(self, path, **extra):
        return subprocess.run([self.shell, str(path)], cwd=self.root, env={**self.env, **extra}, capture_output=True, text=True, timeout=20)

    def test_syntax_and_name_contract(self):
        expected = {
            "upsage.sh": ["/etc/nikki/run/sage-events-v2", "/root/smart/sage-archive-v2", "gdrive:smart_data/sage-native-v2", "sage-native-v2.*.jsonl.ready"],
            "update_sage_prior.sh": ["sage-prior-v2-model-", "/etc/nikki/run/sage-prior-v2", "SagePrior-v2.zip", "SagePrior.json"],
        }
        for name, tokens in expected.items():
            source = (HERE / name).read_text(encoding="utf-8")
            self.assertNotIn("adaptive", source.lower())
            self.assertIn('TG_TOKEN="${TG_TOKEN:-}"', source)
            self.assertIn('TG_CHAT_ID="${TG_CHAT_ID:-}"', source)
            for token in tokens:
                self.assertIn(token, source)
            result = subprocess.run([self.shell, "-n", str(HERE / name)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_docs_use_isolated_v2_namespace_and_pld_stays_separate(self):
        runtime = (HERE / "SAGE_RUNTIME.md").read_text(encoding="utf-8")
        profile = (HERE / "sage-profile.example.yaml").read_text(encoding="utf-8")
        cron = (HERE / "sage-cron.example").read_text(encoding="utf-8")
        for token in ("sage-native-events-v2", "sage-prior-native-features-v2",
                      "sage-native-v2.key", "sage-events-v2", "sage-native-v2",
                      "sage-prior-v2", "SagePrior-v2.zip", "sage-prior-v2-model-"):
            self.assertIn(token, runtime)
        self.assertIn("sage-prior-v2/SagePrior.json", profile)
        self.assertIn("sage-events-v2/sage-native-v2", cron)
        root = HERE.parent
        for pld_path in (root / ".github/workflows/train-pld.yml", HERE / "train_pld.py"):
            self.assertNotIn("sage-native-v2", pld_path.read_text(encoding="utf-8"))

    def test_upload_closed_files_preserves_live_and_legacy(self):
        script = self.script("upsage.sh")
        self.stub("rclone", 'case "$1" in\ncopy) mkdir -p "$3"; cp "$2"/* "$3"/ ;;\nlsl) wc -c < "$2" ;;\n*) exit 90 ;;\nesac\n')
        group = self.root / "home/sage-events-v2/g-one"
        group.mkdir(parents=True)
        payload = b"closed-script-path-fixture\n"
        ready = group / "sage-native-v2.run.1.1.jsonl.ready"
        ready.write_bytes(payload)
        live = group / "sage-native-v2.run.1.2.jsonl.open"
        live.write_bytes(b"live")
        legacy = self.root / "home/adaptive-events/g-old"
        legacy.mkdir(parents=True)
        old = legacy / "adaptive-native-v1.old.1.1.jsonl.ready"
        old.write_bytes(b"legacy")
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "remote" / ready.name).read_bytes(), payload)
        self.assertFalse(ready.exists())
        self.assertFalse((self.root / "smart/sage-archive-v2" / ready.name).exists())
        self.assertEqual(live.read_bytes(), b"live")
        self.assertEqual(old.read_bytes(), b"legacy")

    def test_upload_failure_retains_closed_archive(self):
        script = self.script("upsage.sh")
        self.stub("rclone", "exit 8\n")
        group = self.root / "home/sage-events-v2/g-one"
        group.mkdir(parents=True)
        name = "sage-native-v2.run.1.1.jsonl.ready"
        (group / name).write_bytes(b"must survive")
        result = self.run_script(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "smart/sage-archive-v2" / name).read_bytes(), b"must survive")

    def test_updater_no_release_is_noop(self):
        script = self.script("update_sage_prior.sh")
        self.stub("curl", "printf '[]'\n")
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("跳过", result.stdout)
        self.assertFalse((self.root / "download").exists())

    def test_updater_same_release_is_noop(self):
        script = self.script("update_sage_prior.sh")
        tag = "sage-prior-v2-model-123"
        self.stub("curl", "printf '%s' '" + json.dumps([{"tag_name": tag}]) + "'\n")
        dest = self.root / "home/sage-prior-v2"
        dest.mkdir(parents=True)
        (dest / ".release_tag").write_text(tag + "\n")
        model = dest / "SagePrior.json"
        model.write_bytes(b"existing")
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已是最新", result.stdout)
        self.assertEqual(model.read_bytes(), b"existing")
        self.assertFalse((self.root / "download").exists())


if __name__ == "__main__":
    unittest.main()
