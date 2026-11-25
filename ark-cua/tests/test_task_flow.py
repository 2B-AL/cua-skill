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

"""The commands an agent runs for tasks, end to end against a fake gateway.

A follow-up continues the context of the delegated task, the user's words
reach the gateway unchanged, a task id and an invocation id name the same
task, a call lost to a dropped connection is retried, and a task that lost
model authorization is resumed by sending its creation request again.
"""

import base64
import contextlib
import http.client
import io
import json
import os
import shlex
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
                patch.object(cua.cua_auth.time, "sleep") as sleep, patch.object(sys, "stdin", Stdin(stdin)), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cua.main(list(argv))
        self.slept = [call.args[0] for call in sleep.call_args_list]
        return json.loads(out.getvalue())

    def test_desktop_specs_reads_only_the_current_accounts_catalog(self):
        catalog = {
            "default_spec_code": "windows-4c8g",
            "available_specs": [
                {"spec_code": "windows-4c8g", "os": "Windows", "availability": "available"},
                {"spec_code": "windows-8c16g", "os": "Windows", "availability": "temporarily_unavailable",
                 "lease_duration_seconds": 7200, "user_stop_policy": "release"},
            ],
        }
        self.replies = {("GET", "/v1/desktop-specs"): catalog}

        result = self.run_cli("desktop", "specs")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], catalog)
        self.assertEqual(self.calls, [("GET", "/v1/desktop-specs", None)])

    def test_desktop_allocate_follows_the_same_operation_and_reads_its_lease(self):
        operation = "/v1/desktop/operations/op-example"
        sandbox = "/v1/sandboxes/sbi-example"
        expires_at = "2026-11-25T12:30:00Z"
        self.replies = {
            ("POST", "/v1/desktop-allocations"): {
                "desktop_id": "desk-example", "sandbox_id": "sbi-example",
                "operation_id": "op-example", "spec_code": "windows-8c16g",
            },
            ("GET", operation): [{"status": "running", "terminal": False},
                                 {"status": "succeeded", "terminal": True}],
            ("GET", sandbox): ({}, json.dumps({"lifecycle": {"hardExpiresAt": expires_at}}).encode()),
        }
        gateway = ["--api-base-url", "https://gateway.example.test/cua"]
        follow_up = [*gateway, "desktop", "operation", "--operation-id", "op-example", "--sandbox-id", "sbi-example"]

        with patch.object(cua.cua_auth, "_authorized_raw_call_once", side_effect=self.gateway):
            allocated = self.run_cli(*gateway, "desktop", "allocate", "--spec", "windows-8c16g",
                                     "--idempotency-key", "allocate-example")
            self.assertTrue(allocated["ok"])
            command = shlex.split(allocated["next"]["command"])[2:]
            self.assertEqual(command, follow_up)
            running = self.run_cli(*command)
            self.assertEqual(shlex.split(running["next"]["command"])[2:], follow_up)
            ready = self.run_cli(*follow_up)

        self.assertTrue(ready["ok"])
        self.assertEqual(ready["data"]["expires_at"], expires_at)
        self.assertEqual(self.calls, [
            ("POST", "/v1/desktop-allocations", {"spec_code": "windows-8c16g", "idempotency_key": "allocate-example"}),
            ("GET", operation, None),
            ("GET", operation, None),
            ("GET", sandbox, None),
        ])

    def test_desktop_allocate_reports_a_shortage_without_a_second_request(self):
        self.replies = {("POST", "/v1/desktop-allocations"): SkillError(
            "SPEC_TEMPORARILY_UNAVAILABLE", "The selected desktop spec has no capacity.",
        )}

        result = self.run_cli("desktop", "allocate", "--spec", "windows-8c16g", "--idempotency-key", "allocate-example")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SPEC_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(self.calls, [
            ("POST", "/v1/desktop-allocations", {"spec_code": "windows-8c16g", "idempotency_key": "allocate-example"}),
        ])

    def test_desktop_allocate_requires_both_ids_to_continue(self):
        self.replies = {("POST", "/v1/desktop-allocations"): {"operation_id": "op-example"}}

        result = self.run_cli("desktop", "allocate", "--spec", "windows-8c16g", "--idempotency-key", "allocate-example")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UPSTREAM_PROTOCOL_ERROR")

    def test_desktop_operation_read_skips_lease_for_legacy_or_failed_operations(self):
        operation = "/v1/desktop/operations/op-example"
        cases = [([], "succeeded"), (["--sandbox-id", "sbi-example"], "failed")]
        for options, status in cases:
            with self.subTest(status=status):
                self.replies = {("GET", operation): {"status": status, "terminal": True}}
                with patch.object(cua.cua_auth, "authorized_raw_call") as raw_call:
                    result = self.run_cli("desktop", "operation", "--operation-id", "op-example", *options)
                self.assertTrue(result["ok"])
                self.assertNotIn("expires_at", result["data"])
                raw_call.assert_not_called()

    def test_desktop_start_and_shutdown_keep_their_existing_routes(self):
        self.replies = {
            ("POST", "/v1/desktop/start"): {"operation_id": "op-start"},
            ("POST", "/v1/desktop/release"): {"operation_id": "op-shutdown"},
        }
        started = self.run_cli("desktop", "start", "--idempotency-key", "start-example")
        stopped = self.run_cli("desktop", "shutdown", "--confirm", "--idempotency-key", "shutdown-example")

        for action, result in (("start", started), ("shutdown", stopped)):
            self.assertTrue(result["ok"])
            self.assertEqual(shlex.split(result["next"]["command"])[2:],
                             ["desktop", "operation", "--operation-id", f"op-{action}"])
        self.assertEqual(self.calls, [
            ("POST", "/v1/desktop/start", {"idempotency_key": "start-example"}),
            ("POST", "/v1/desktop/release", {"idempotency_key": "shutdown-example",
                                            "confirm": True, "reason": "user_requested_shutdown"}),
        ])

    def test_a_follow_up_goes_to_the_context_of_the_delegated_task(self):
        # An earlier `task run` left its task and context as the latest ones.
        self.session_file.write_text(json.dumps(
            {"last_task_id": "task_old", "last_invocation_id": "task_old", "last_context_id": "ctx_old"}))
        self.replies = {
            ("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress"),
            ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1"),
            ("POST", "/v1/contexts/ctx_1/tasks"): task("task_2", "ctx_1"),
        }
        self.assertEqual(self.run_cli("delegate", "--objective", "在云电脑上打开 B 站")["data"]["outcome"], "completed")
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

    def test_an_unfinished_follow_up_is_watched_rather_than_polled(self):
        # task status answers at once, so following it would cost a turn every few seconds.
        self.session_file.write_text(json.dumps({"last_context_id": "ctx_1"}))
        unfinished = task("task_2", "ctx_1", outcome="in_progress")
        self.replies = {("POST", "/v1/contexts/ctx_1/tasks"): unfinished, ("POST", "/v1/invocations/task_2/watch"): unfinished}
        reply = self.run_cli("task", "continue", "--last-context", "--objective", "再告诉我播放量")
        self.assertIn("watch --invocation-id task_2", reply["next"]["command"])

    def test_last_says_when_no_task_is_recorded(self):
        for argv in (("watch", "--last"), ("task", "status", "--last")):
            reply = self.run_cli(*argv)
            self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
            self.assertIn("No task has been recorded", reply["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_an_unreachable_gateway_names_its_address_and_where_it_came_from(self):
        os.environ["AP_CUA_SKILL_API_BASE_URL"] = "http://127.0.0.1:9"
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1"}))
        refused = SkillError("NETWORK", "Cannot reach CUA gateway at http://127.0.0.1:9: Connection refused")
        self.replies = {("POST", "/v1/invocations/task_1/watch"): [refused] * 3}
        reply = self.run_cli("watch", "--last")
        self.assertEqual(reply["error"]["gateway"], {"url": "http://127.0.0.1:9", "source": "AP_CUA_SKILL_API_BASE_URL"})
        self.assertIn("Never print environment variables", reply["next"]["agent_hint"])
        self.assertNotIn("not a real failure", reply["next"]["agent_hint"])

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

    def test_artifacts_of_the_latest_task_are_listed_without_an_id(self):
        # Like artifact save, artifact list falls back to the latest task.
        self.session_file.write_text(json.dumps({"last_task_id": "task_2", "last_invocation_id": "task_2"}))
        self.replies = {("GET", "/v1/tasks/task_2/artifacts"): {"task_id": "task_2", "artifacts": []}}
        self.assertTrue(self.run_cli("artifact", "list")["ok"])
        self.assertEqual(self.calls, [("GET", "/v1/tasks/task_2/artifacts", None)])
        self.session_file.write_text("{}")
        reply = self.run_cli("artifact", "list")
        self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("No task has been recorded", reply["error"]["message"])
        self.assertEqual(len(self.calls), 1)

    def test_saving_the_last_artifact_says_when_none_is_saved(self):
        reply = self.run_cli("artifact", "save", "--last")
        self.assertEqual(reply["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("No artifact has been saved", reply["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_each_task_is_created_under_its_own_key_kept_until_the_task_ends(self):
        self.replies = {("POST", "/v1/tasks"): [task("task_1", "ctx_1", outcome="in_progress"), task("task_2", "ctx_2")],
                        ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1")}
        self.run_cli("delegate", "--wait-ms", "0", "--objective", "第一个")
        kept = self.saved()["submissions"]["task_1"]
        self.assertEqual((kept["path"], kept["body"]), ("/v1/tasks", self.calls[0][2]))
        self.run_cli("watch", "--last")
        self.run_cli("delegate", "--wait-ms", "0", "--objective", "第二个")
        first, second = self.calls[0][2]["idempotency_key"], self.calls[2][2]["idempotency_key"]
        self.assertTrue(first.startswith("ark-cua-") and second.startswith("ark-cua-") and first != second)
        self.assertEqual(self.saved()["submissions"], {})

    def test_a_lost_creation_response_is_retried_under_the_same_key(self):
        self.replies = {("POST", "/v1/tasks"): [SkillError("NETWORK", "The connection dropped."),
                                                task("task_1", "ctx_1", outcome="in_progress")]}
        self.assertEqual(self.run_cli("delegate", "--wait-ms", "0", "--objective", "打开记事本")["data"]["invocation_id"], "task_1")
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
        self.assertEqual(self.run_cli("delegate", "--wait-ms", "0", "--objective", "打开记事本")["data"]["invocation_id"], "task_1")
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
        self.assertEqual(reply["data"]["outcome"], "in_progress")
        self.assertIn("watch --invocation-id task_1", reply["next"]["command"])

    def test_a_new_task_is_sent_again_as_soon_as_authorization_lapses(self):
        # Authorization can lapse seconds after creation; the first resend does not wait.
        self.replies = {("POST", "/v1/tasks"): [task("task_1", "ctx_1", outcome="in_progress")] * 2,
                        ("GET", "/v1/tasks/task_1"): self.awaiting_authorization()}
        self.run_cli("delegate", "--wait-ms", "0", "--objective", "打开 B 站")
        self.run_cli("task", "status", "--last")
        self.assertEqual([call[:2] for call in self.calls],
                         [("POST", "/v1/tasks"), ("GET", "/v1/tasks/task_1"), ("POST", "/v1/tasks")])
        self.assertEqual(self.calls[2][2], self.calls[0][2])

    def test_the_request_is_sent_again_at_most_every_half_minute(self):
        self.keep_submission(sent_at=time.time())
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization()}
        reply = self.run_cli("watch", "--last")
        self.assertEqual([call[1] for call in self.calls], ["/v1/invocations/task_1/watch"])
        # The gateway's watch answers at once while CUA waits; the CLI still uses up the wait.
        self.assertAlmostEqual(sum(self.slept), cua.DEFAULT_WATCH_WAIT_MS / 1000, delta=1)
        self.assertIn("do not submit the task again", reply["next"]["agent_hint"])
        self.assertNotIn("Repeat the original", reply["next"]["agent_hint"])

    def test_a_watch_waits_for_a_send_that_falls_within_it(self):
        body = self.keep_submission(sent_at=time.time() - 25)
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization(),
                        ("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress", progress={"state": "running"})}
        self.run_cli("watch", "--last")
        self.assertAlmostEqual(self.slept[0], 5, delta=1)
        self.assertEqual(self.calls[1], ("POST", "/v1/tasks", body))

    def test_a_task_that_cannot_be_sent_again_is_left_to_the_user(self):
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1"}))
        self.replies = {("POST", "/v1/invocations/task_1/watch"): self.awaiting_authorization()}
        reply = self.run_cli("watch", "--last")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("command", reply["next"])
        self.assertIn("Tell the user", reply["next"]["agent_hint"])

    def test_a_result_wait_ends_when_the_task_cannot_be_sent_again(self):
        # Nothing else resumes such a task, so waiting out --timeout would only hold the agent.
        self.session_file.write_text(json.dumps({"last_invocation_id": "task_1", "last_task_id": "task_1"}))
        self.replies = {("GET", "/v1/invocations/task_1"): self.awaiting_authorization(),
                        ("GET", "/v1/tasks/task_1/result"): self.awaiting_authorization()}
        for argv in (("result", "--last"), ("task", "result", "--last")):
            self.assertIn("Tell the user", self.run_cli(*argv)["next"]["agent_hint"])
        self.assertEqual([call[1] for call in self.calls], ["/v1/invocations/task_1", "/v1/tasks/task_1/result"])

    def test_submit_commands_wait_by_default(self):
        parser = cua.build_parser()
        for argv in (["delegate", "--objective", "x"], ["task", "run", "--objective", "x"],
                     ["task", "continue", "--last-context", "--objective", "x"]):
            self.assertEqual(parser.parse_args(argv).wait_ms, cua.DEFAULT_WATCH_WAIT_MS)
        self.assertEqual(parser.parse_args(["delegate", "--objective", "x", "--wait-ms", "0"]).wait_ms, 0)

    def test_a_short_task_completes_in_the_delegate_call(self):
        self.replies = {("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress"),
                        ("POST", "/v1/invocations/task_1/watch"): task("task_1", "ctx_1", result={"text": "杭州今天多云"})}
        reply = self.run_cli("delegate", "--objective", "查一下杭州天气")
        self.assertEqual((reply["data"]["outcome"], reply["data"]["result"]), ("completed", {"text": "杭州今天多云"}))
        self.assertNotIn("command", reply["next"])

    def test_an_interrupted_wait_keeps_the_created_task(self):
        dropped = SkillError("NETWORK", "connection reset")
        self.replies = {("POST", "/v1/tasks"): task("task_1", "ctx_1", outcome="in_progress"),
                        ("POST", "/v1/invocations/task_1/watch"): [dropped] * 3}
        reply = self.run_cli("delegate", "--objective", "打开记事本")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["wait_interrupted"], {"code": "NETWORK", "message": "connection reset"})
        self.assertIn("watch --invocation-id task_1", reply["next"]["command"])
        self.assertEqual(self.saved()["last_invocation_id"], "task_1")

    def test_a_rejected_submission_is_still_an_error(self):
        self.replies = {("POST", "/v1/tasks"): SkillError("FORBIDDEN", "Gateway request failed (Forbidden).")}
        reply = self.run_cli("delegate", "--objective", "打开记事本")
        self.assertEqual((reply["ok"], reply["error"]["code"]), (False, "FORBIDDEN"))
        self.assertNotIn("last_invocation_id", self.saved())

    def test_the_output_keeps_what_the_agent_acts_on(self):
        advisory = {"url": "https://security.example/view", "expires_at": "2026-11-16T12:00:00Z"}
        full = {"invocation_id": "task_1", "outcome": "completed", "result": {"text": "完成", "artifacts": [], "error": None},
                "platform": {"desktop": "desk_1", "session_id": "sess_1", "run_id": "run_1", "context_id": "ctx_1",
                             "trace_id": None, "security_advisory": advisory},
                "progress": {"state": "done", "step_count": 4, "summary": "CUA finished."},
                "diagnostics": {"request_id": "req_1"}, "model_selection": {"model": "m"},
                "next_action": {"agent_hint": "Use the result."}, "input_request": None}
        self.replies = {("POST", "/v1/tasks"): full}
        data = self.run_cli("delegate", "--objective", "做完")["data"]
        self.assertEqual(data, {"invocation_id": "task_1", "outcome": "completed", "result": {"text": "完成"},
                                "platform": {"desktop": "desk_1", "run_id": "run_1", "context_id": "ctx_1",
                                             "security_advisory": advisory},
                                "request_id": "req_1"})

    def test_a_screenshot_is_saved_to_a_new_file_without_a_task(self):
        image = b"\x89PNG\r\n\x1a\n fake image bytes"
        shot = {"environment": {"id": "desk_1"}, "screenshot": {
            "mime_type": "image/png", "width": 1920, "height": 1080, "base64": base64.b64encode(image).decode()}}
        self.replies = {("GET", "/v1/desktop/screenshot?desktop=desk_1"): shot}
        target = self.session_file.parent / "screen.png"
        reply = self.run_cli("desktop", "screenshot", "--desktop", "desk_1", "--output", str(target))
        self.assertEqual(target.read_bytes(), image)
        self.assertEqual(reply["data"], {"file": str(target), "mime_type": "image/png", "bytes": len(image),
                                         "width": 1920, "height": 1080, "desktop_id": "desk_1"})
        again = self.run_cli("desktop", "screenshot", "--desktop", "desk_1", "--output", str(target))
        self.assertEqual(again["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(target.read_bytes(), image)

    def test_a_screenshot_without_an_image_is_an_error(self):
        self.replies = {("GET", "/v1/desktop/screenshot"): {"screenshot": {"mime_type": "image/png", "base64": ""}}}
        reply = self.run_cli("desktop", "screenshot")
        self.assertEqual(reply["error"]["code"], "UPSTREAM_PROTOCOL_ERROR")


if __name__ == "__main__":
    unittest.main()
