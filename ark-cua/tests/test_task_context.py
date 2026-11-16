# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A follow-up request continues the context of the task that `delegate` started."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua  # noqa: E402


def task(task_id, context_id, outcome="completed"):
    return {"invocation_id": task_id, "outcome": outcome, "platform": {"context_id": context_id}}


class DelegateContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.session_file = Path(directory.name) / "session.json"
        environment = patch.dict(os.environ, {
            "AP_CUA_SKILL_AUTH_FILE": str(Path(directory.name) / "auth.json"),
            "AP_CUA_SKILL_SESSION_FILE": str(self.session_file),
        })
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("AP_CUA_SKILL_API_BASE_URL", "CUA_SKILL_API_BASE_URL"):
            os.environ.pop(name, None)
        self.calls, self.replies = [], {}

    def gateway(self, state, base_url, method, path, body=None, **unused):
        self.calls.append((method, path, body))
        return self.replies[(method, path)]

    def run_cli(self, *argv):
        """Run one CLI command, as the agent does, and return its JSON output."""
        out = io.StringIO()
        with patch.object(cua.cua_auth, "authorized_call", side_effect=self.gateway), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cua.main(list(argv))
        return json.loads(out.getvalue())

    def test_a_follow_up_goes_to_the_context_of_the_delegated_task(self):
        # An earlier `task run` left its task and context as the latest ones.
        self.session_file.write_text(json.dumps(
            {"last_task_id": "task_old", "last_invocation_id": "task_old", "last_context_id": "ctx_old"}))
        self.replies = {
            ("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress"),
            ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1"),
            ("POST", "/v1/contexts/ctx_1/tasks"): task("task_2", "ctx_1"),
        }
        self.assertTrue(self.run_cli("delegate", "--objective", "在云电脑上打开 B 站")["ok"])
        self.assertEqual(self.run_cli("watch", "--last")["data"]["outcome"], "completed")
        follow_up = self.run_cli("task", "continue", "--last-context", "--objective", "第一个视频是谁发的")

        self.assertEqual(follow_up["data"]["invocation_id"], "task_2")
        self.assertEqual([call[:2] for call in self.calls], [
            ("POST", "/v1/tasks"), ("POST", "/v1/invocations/task_1/watch"), ("POST", "/v1/contexts/ctx_1/tasks"),
        ])
        self.assertEqual(self.calls[0][2], {"objective": "在云电脑上打开 B 站", "wait_ms": 0})
        saved = json.loads(self.session_file.read_text())
        self.assertEqual((saved["last_task_id"], saved["last_invocation_id"], saved["last_context_id"]),
                         ("task_2", "task_2", "ctx_1"))

    def test_last_context_says_when_no_context_is_saved(self):
        reply = self.run_cli("task", "continue", "--last-context", "--objective", "继续")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("No context has been saved", reply["error"]["message"])
        self.assertIn("--context-id", reply["error"]["message"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
