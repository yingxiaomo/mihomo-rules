import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from smart_trainer.sage_workflow_notify import build_message, main, send_message


class SageWorkflowNotifyTests(unittest.TestCase):
    def args(self, **overrides):
        values = {
            "status": "success",
            "failed_step": "",
            "run_url": "https://github.com/example/actions/runs/1",
            "release_tag": "sage-prior-v2-model-1",
            "training_commit": "1" * 40,
            "consumer_commit": "2" * 40,
            "bundle_sha": "3" * 64,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_success_and_failure_messages_are_final_and_escaped(self):
        success = build_message(self.args(release_tag="tag<&"))
        self.assertIn("流程成功", success)
        self.assertIn("tag&lt;&amp;", success)
        failure = build_message(self.args(status="failure", failed_step="Train <head>"))
        self.assertIn("流程失败", failure)
        self.assertIn("Train &lt;head&gt;", failure)

    def test_bot_api_requires_http_200_and_ok_true(self):
        accepted = MagicMock()
        accepted.__enter__.return_value.status = 200
        accepted.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch.dict(os.environ, {"TG_BOT_TOKEN": "secret", "TG_CHAT_ID": "1"}), \
                patch("smart_trainer.sage_workflow_notify.urllib.request.urlopen", return_value=accepted):
            self.assertTrue(send_message("hello", retries=1))

        rejected = MagicMock()
        rejected.__enter__.return_value.status = 200
        rejected.__enter__.return_value.read.return_value = b'{"ok":false}'
        with patch.dict(os.environ, {"TG_BOT_TOKEN": "secret", "TG_CHAT_ID": "1"}), \
                patch("smart_trainer.sage_workflow_notify.urllib.request.urlopen", return_value=rejected):
            self.assertFalse(send_message("hello", retries=1))

    def test_cli_notification_failure_never_changes_job_result(self):
        argv = ["--status", "failure", "--failed-step", "tests", "--training-commit", "1" * 40, "--consumer-commit", "2" * 40]
        with patch("smart_trainer.sage_workflow_notify.send_message", return_value=False):
            self.assertEqual(main(argv), 0)


if __name__ == "__main__":
    unittest.main()
