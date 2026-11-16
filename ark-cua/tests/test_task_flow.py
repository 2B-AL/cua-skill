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

"""The task commands an agent runs, end to end against a fake gateway.

A follow-up continues the context of the delegated task, the user's words
reach the gateway unchanged, a task id and an invocation id name the same
task, a call lost to a dropped connection is retried, and a task that lost
model authorization is resumed by sending its creation request again.
"""

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua  # noqa: E402
import cua_http  # noqa: E402
from cua_util import SkillError  # noqa: E402

# What a runtime without keyed creation answers to a keyed request, before accepting a task.
IDEMPOTENCY_UNAVAILABLE = SkillError("TaskIdempotencyUnavailable", "Gateway request failed (TaskIdempotencyUnavailable).",
                                     upstream_code="TaskIdempotencyUnavailable", upstream_status=409)


def task(task_id, context_id, outcome="completed", **extra):
    return {"invocation_id": task_id, "outcome": outcome, "platform": {"context_id": context_id}, **extra}


class Stdin:
    """Standard input as a heredoc delivers it: UTF-8 bytes."""

    def __init__(self, text):
        self.buffer = io.BytesIO(text.encode("utf-8"))


class TaskFlowTests(unittest.TestCase):
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
        """Answer from self.replies; a list answers successive calls, an exception is raised."""
        self.calls.append((method, path, body))
        reply = self.replies[(method, path)]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def saved(self):
        return json.loads(self.session_file.read_text()) if self.session_file.exists() else {}

    def run_cli(self, *argv, stdin=""):
        """Run one CLI command, as the agent does, and return its JSON output."""
        out = io.StringIO()
        # The fake answers single attempts, so the CLI's own retries still run.
        with patch.object(cua.cua_auth, "_authorized_call_once", side_effect=self.gateway), \
                patch.object(cua.cua_auth.time, "sleep"), patch.object(sys, "stdin", Stdin(stdin)), \
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
        self.assertEqual({k: v for k, v in self.calls[0][2].items() if k != "idempotency_key"},
                         {"objective": "在云电脑上打开 B 站", "wait_ms": 0})
        saved = self.saved()
        self.assertEqual((saved["last_task_id"], saved["last_invocation_id"], saved["last_context_id"]),
                         ("task_2", "task_2", "ctx_1"))

    def test_a_follow_up_can_name_the_earlier_task_instead_of_its_context(self):
        self.replies = {
            ("GET", "/v1/tasks/task_1"): task("task_1", "ctx_1"),
            ("POST", "/v1/contexts/ctx_1/tasks"): task("task_2", "ctx_1"),
        }
        reply = self.run_cli("task", "continue", "--invocation-id", "task_1", "--objective", "再告诉我播放量")
        self.assertEqual(reply["data"]["invocation_id"], "task_2")
        self.assertEqual([call[:2] for call in self.calls],
                         [("GET", "/v1/tasks/task_1"), ("POST", "/v1/contexts/ctx_1/tasks")])

    def test_last_context_says_when_no_context_is_saved(self):
        reply = self.run_cli("task", "continue", "--last-context", "--objective", "继续")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("No context has been saved", reply["error"]["message"])
        self.assertIn("--context-id", reply["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_the_users_words_reach_the_gateway_unchanged_through_stdin(self):
        words = "在云电脑上输入“你好，CUA”，再看标题里有没有 'Example'、$HOME 和 `ls`"
        self.replies = {("POST", "/v1/tasks"): task("task_1", "ctx_1")}
        self.assertTrue(self.run_cli("delegate", "--objective", "-", stdin=words + "\n")["ok"])
        self.assertEqual(self.calls[0][2]["objective"], words)

    def test_an_answer_reaches_the_gateway_unchanged_through_stdin(self):
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1"}))
        self.replies = {("POST", "/v1/invocations/task_1/answer"): task("task_1", "ctx_1")}
        self.assertTrue(self.run_cli("answer", "--last", "--answer", "-", stdin="写：“今天天气不错”\n")["ok"])
        self.assertEqual(self.calls[0][2]["answer"], "写：“今天天气不错”")

    def test_empty_stdin_is_reported_before_anything_is_sent(self):
        reply = self.run_cli("delegate", "--objective", "-")
        self.assertEqual((reply["ok"], reply["error"]["code"]), (False, "VALIDATION_ERROR"))
        self.assertIn("heredoc", reply["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_a_question_comes_with_a_reply_command_that_keeps_the_words_unchanged(self):
        question = task("task_1", "ctx_1", outcome="needs_input", input_request={"question": "写什么？"})
        self.replies = {("POST", "/v1/tasks"): question}
        reply = self.run_cli("delegate", "--objective", "打开记事本，先问我写什么")
        self.assertIn("answer --invocation-id task_1 --answer - <<'EOF'\n", reply["next"]["command"])
        self.assertIn("never answer it yourself", reply["next"]["agent_hint"])

    def test_a_task_id_and_an_invocation_id_name_the_same_task(self):
        self.replies = {
            ("GET", "/v1/tasks/task_1/artifacts"): {"task_id": "task_1", "artifacts": []},
            ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1"),
        }
        self.assertTrue(self.run_cli("artifact", "list", "--invocation-id", "task_1")["ok"])
        self.assertTrue(self.run_cli("watch", "--task-id", "task_1")["ok"])
        self.assertEqual([call[:2] for call in self.calls],
                         [("GET", "/v1/tasks/task_1/artifacts"), ("POST", "/v1/invocations/task_1/watch")])

    def test_listed_artifacts_come_with_the_command_that_saves_them(self):
        self.replies = {("GET", "/v1/tasks/task_1/artifacts"): {
            "task_id": "task_1", "agent_hint": "Use artifact content_url to download raw artifact bytes.",
            "artifacts": [{"id": "art_1", "kind": "screenshot", "content_url": "/v1/artifacts/art_1/content?task_id=task_1"}],
        }}
        reply = self.run_cli("artifact", "list", "--task-id", "task_1")
        self.assertIn("artifact save --task-id task_1 --artifact-id art_1", reply["next"]["command"])
        self.assertNotIn("agent_hint", reply["data"])

    def test_saving_the_last_artifact_says_when_none_is_saved(self):
        reply = self.run_cli("artifact", "save", "--last")
        self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("No artifact has been saved", reply["error"]["message"])
        self.assertEqual(self.calls, [])


    def test_each_task_is_created_under_its_own_key_kept_until_the_task_ends(self):
        self.replies = {("POST", "/v1/tasks"): [task("task_1", "ctx_1", outcome="in_progress"), task("task_2", "ctx_2")],
                        ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1")}
        self.run_cli("delegate", "--objective", "第一个")
        kept = self.saved()["submissions"]["task_1"]
        self.assertEqual((kept["path"], kept["body"]), ("/v1/tasks", self.calls[0][2]))
        self.run_cli("watch", "--last")
        self.run_cli("delegate", "--objective", "第二个")
        first, second = self.calls[0][2]["idempotency_key"], self.calls[2][2]["idempotency_key"]
        self.assertTrue(first.startswith("ark-cua-") and second.startswith("ark-cua-") and first != second)
        self.assertEqual(self.saved()["submissions"], {})

    def test_a_lost_creation_response_is_retried_under_the_same_key(self):
        self.replies = {("POST", "/v1/tasks"): [SkillError("NETWORK", "The connection dropped."),
                                                task("task_1", "ctx_1", outcome="in_progress")]}
        self.assertEqual(self.run_cli("delegate", "--objective", "打开记事本")["data"]["invocation_id"], "task_1")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][2], self.calls[1][2])

    def test_a_dropped_connection_is_a_network_error_that_watch_retries(self):
        dropped = http.client.RemoteDisconnected("Remote end closed connection without response")
        with patch.object(cua_http._OPENER, "open", side_effect=dropped), self.assertRaises(SkillError) as raised:
            cua_http.request("POST", "https://gateway.example", "/v1/invocations/task_1/watch", body={"wait_ms": 1})
        self.assertEqual(raised.exception.code, "NETWORK")
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1"}))
        self.replies = {("POST", "/v1/invocations/task_1/watch"): [raised.exception, task("task_1", "ctx_1")]}
        self.assertEqual(self.run_cli("watch", "--last")["data"]["outcome"], "completed")

    def test_a_runtime_without_keyed_creation_gets_the_request_without_a_key(self):
        self.replies = {("POST", "/v1/tasks"): [IDEMPOTENCY_UNAVAILABLE, task("task_1", "ctx_1", outcome="in_progress")]}
        self.assertEqual(self.run_cli("delegate", "--objective", "打开记事本")["data"]["invocation_id"], "task_1")
        self.assertIn("idempotency_key", self.calls[0][2])
        self.assertEqual(self.calls[1][2], {"objective": "打开记事本", "wait_ms": 0})
        self.assertNotIn("task_1", self.saved().get("submissions", {}))

    def keep_submission(self, sent_at):
        body = {"objective": "打开 B 站", "wait_ms": 0, "idempotency_key": "ark-cua-original"}
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1", "submissions": {
            "task_1": {"path": "/v1/tasks", "body": body, "sent_at": sent_at}}}))
        return body

    def awaiting_authorization(self):
        return task("task_1", "ctx_1", outcome="in_progress", progress={"state": "awaiting_model_authorization"},
                    next_action={"agent_hint": "Repeat the original task submission with valid model credentials."})

    def test_a_lapsed_model_authorization_is_restored_by_sending_the_request_again(self):
        body = self.keep_submission(sent_at=0)
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization(),
                        ("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress", progress={"state": "running"})}
        reply = self.run_cli("watch", "--last")
        self.assertEqual(self.calls[1], ("POST", "/v1/tasks", body))
        self.assertEqual(reply["data"]["progress"]["state"], "running")
        self.assertIn("watch --invocation-id task_1", reply["next"]["command"])

    def test_the_request_is_sent_again_at_most_every_half_minute(self):
        self.keep_submission(sent_at=time.time())
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization()}
        reply = self.run_cli("watch", "--last")
        self.assertEqual([call[1] for call in self.calls], ["/v1/invocations/task_1/watch"])
        self.assertIn("do not submit the task again", reply["next"]["agent_hint"])
        self.assertNotIn("Repeat the original", reply["next"]["agent_hint"])

    def test_a_task_that_cannot_be_sent_again_is_left_to_the_user(self):
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1"}))
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization()}
        reply = self.run_cli("watch", "--last")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("command", reply["next"])
        self.assertIn("Tell the user", reply["next"]["agent_hint"])


if __name__ == "__main__":
    unittest.main()
