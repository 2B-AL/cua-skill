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

"""The state directory is ~/.ark-cua; state left in ~/.openclaw/ark-cua moves there once."""

import errno
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cua_state  # noqa: E402
import cua_util  # noqa: E402
from cua_util import SkillError  # noqa: E402

KEY = secrets.token_urlsafe(24)
OTHER_KEY = secrets.token_urlsafe(24)
LOGIN = {"api_base_url": "https://gateway.example", "api_key": KEY, "credential_source": "manual",
         "user": {"account_id": 1}}
SESSION = {"last_invocation_id": "inv-1"}
OVERRIDES = ("AP_CUA_SKILL_AUTH_FILE", "CUA_SKILL_AUTH_FILE", "AP_CUA_SKILL_SESSION_FILE", "CUA_SKILL_SESSION_FILE")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def put(directory, name, data, file_mode=0o600):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, file_mode)
    return path


class StateDirectoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.new, self.old = root / ".ark-cua", root / ".openclaw" / "ark-cua"
        self.notices = []
        environment = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
        for patcher in (patch.object(cua_state, "DEFAULT_DIR", self.new),
                        patch.object(cua_state, "LEGACY_DIR", self.old),
                        patch.object(cua_util, "_NOTICES", self.notices),
                        patch.dict(os.environ, environment, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def load(self):
        return cua_state.AuthState.load(), cua_state.SessionState.load()

    def test_state_only_in_the_old_directory_moves_to_the_new_one(self):
        put(self.old, "auth.json", LOGIN, file_mode=0o644)
        put(self.old, "session.json", SESSION)
        other_login = put(self.old / "agentplan", "auth.json", {"api_key": OTHER_KEY})
        sibling = put(self.old.parent, "openclaw.json", {"unrelated": True})
        auth, session = self.load()
        self.assertEqual((auth.access_token, session.last_invocation_id), (KEY, "inv-1"))
        for name in ("auth.json", "session.json"):
            self.assertEqual(mode(self.new / name), 0o600)
            self.assertFalse((self.old / name).exists())
        self.assertEqual(mode(self.new), 0o700)
        self.assertTrue(other_login.exists() and sibling.exists() and self.old.is_dir())
        self.assertEqual(self.notices, [])

    def test_across_filesystems_the_copy_is_verified_before_the_original_goes(self):
        original = put(self.old, "auth.json", LOGIN).read_bytes()
        put(self.old, "session.json", SESSION)
        with patch.object(cua_state.os, "rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            auth, session = self.load()
        self.assertEqual((auth.access_token, session.last_invocation_id), (KEY, "inv-1"))
        self.assertEqual((self.new / "auth.json").read_bytes(), original)
        self.assertEqual((mode(self.new / "auth.json"), mode(self.new / "session.json")), (0o600, 0o600))
        self.assertFalse((self.old / "auth.json").exists() or (self.old / "session.json").exists())
        self.assertEqual([p.name for p in self.new.iterdir() if p.name.startswith(".tmp-")], [])

    def test_state_only_in_the_new_directory_is_used_as_is(self):
        put(self.new, "auth.json", LOGIN)
        os.chmod(self.new, 0o755)
        auth, _ = self.load()
        self.assertEqual(auth.access_token, KEY)
        self.assertEqual(mode(self.new), 0o700)
        self.assertFalse(self.old.exists())
        self.assertEqual(self.notices, [])

    def test_with_state_in_both_the_new_directory_wins_and_the_old_is_reported_once(self):
        put(self.new, "auth.json", LOGIN)
        old_login = put(self.old, "auth.json", {**LOGIN, "api_key": OTHER_KEY})
        before = (old_login.read_bytes(), old_login.stat().st_mtime_ns)
        auth, _ = self.load()
        self.assertEqual(auth.access_token, KEY)
        self.assertEqual((old_login.read_bytes(), old_login.stat().st_mtime_ns), before)
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn(KEY, self.notices[0])
        self.assertNotIn(OTHER_KEY, self.notices[0])
        self.notices.clear()
        self.load()
        self.assertEqual(self.notices, [])

    def test_an_override_variable_keeps_its_meaning_and_turns_the_move_off(self):
        put(self.old, "auth.json", LOGIN)
        for name in OVERRIDES:
            with self.subTest(name), tempfile.TemporaryDirectory() as elsewhere:
                target = Path(elsewhere) / "state.json"
                with patch.dict(os.environ, {name: str(target)}):
                    auth, _ = self.load()
                    session_file = target.parent / "session.json" if "AUTH" in name else target
                    self.assertEqual(cua_state.session_file_path(), session_file)
                self.assertIsNone(auth.access_token)
                self.assertTrue((self.old / "auth.json").exists())
                self.assertFalse(self.new.exists())
        with tempfile.TemporaryDirectory() as elsewhere:
            given = put(Path(elsewhere), "auth.json", LOGIN)
            with patch.dict(os.environ, {"AP_CUA_SKILL_AUTH_FILE": str(given)}):
                auth, _ = self.load()
            self.assertEqual(auth.access_token, KEY)
        self.assertTrue((self.old / "auth.json").exists())

    def test_a_failed_move_stops_with_an_error_and_keeps_the_login(self):
        original = put(self.old, "auth.json", LOGIN).read_bytes()
        with patch.object(cua_state.os, "rename", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(SkillError) as raised:
                cua_state.AuthState.load()
        self.assertEqual(raised.exception.code, "STATE_MIGRATION_FAILED")
        self.assertNotIn(KEY, raised.exception.message + json.dumps(raised.exception.extra))
        self.assertEqual((self.old / "auth.json").read_bytes(), original)
        self.assertFalse((self.new / "auth.json").exists())

    def test_a_copy_that_does_not_match_is_discarded(self):
        put(self.old, "auth.json", LOGIN)
        real_read_bytes = Path.read_bytes

        def read_bytes(path):
            return b"{}" if path.name.startswith(".tmp-") else real_read_bytes(path)

        with patch.object(cua_state.os, "rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")), \
                patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes):
            with self.assertRaises(SkillError) as raised:
                cua_state.AuthState.load()
        self.assertEqual(raised.exception.code, "STATE_MIGRATION_FAILED")
        self.assertTrue((self.old / "auth.json").exists())
        self.assertFalse((self.new / "auth.json").exists())
        self.assertEqual([p.name for p in self.new.iterdir() if p.name.startswith(".tmp-")], [])

    def test_an_original_that_cannot_be_removed_is_reported_and_the_copy_is_used_next(self):
        original = put(self.old, "auth.json", LOGIN).read_bytes()
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if Path(path) == self.old / "auth.json":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        with patch.object(cua_state.os, "rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")), \
                patch.object(cua_state.os, "unlink", side_effect=unlink):
            with self.assertRaises(SkillError) as raised:
                cua_state.AuthState.load()
        self.assertEqual(raised.exception.code, "STATE_MIGRATION_FAILED")
        self.assertIn("could not be removed", raised.exception.message)
        self.assertEqual((self.new / "auth.json").read_bytes(), original)
        self.assertEqual(mode(self.new / "auth.json"), 0o600)
        auth, _ = self.load()
        self.assertEqual(auth.access_token, KEY)
        self.assertEqual(len(self.notices), 1)


class CommandLineTests(unittest.TestCase):
    """The whole CLI, with HOME pointing at a scratch directory; `watch --last` stops before any request."""

    def run_cli(self, home):
        environment = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
        environment["HOME"] = home
        done = subprocess.run([sys.executable, str(SCRIPTS / "cua.py"), "watch", "--last"], env=environment,
                              capture_output=True, text=True, timeout=120)
        self.assertNotIn(KEY, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def test_the_login_moves_once_and_the_key_never_reaches_the_output(self):
        with tempfile.TemporaryDirectory() as home:
            old, new = Path(home) / ".openclaw" / "ark-cua", Path(home) / ".ark-cua"
            put(old, "auth.json", LOGIN, file_mode=0o644)
            first = self.run_cli(home)
            self.assertEqual(first["error"]["code"], "VALIDATION_ERROR")
            self.assertNotIn("notices", first)
            self.assertEqual(json.loads((new / "auth.json").read_text())["api_key"], KEY)
            self.assertEqual((mode(new / "auth.json"), mode(new)), (0o600, 0o700))
            self.assertFalse((old / "auth.json").exists())
            put(old, "auth.json", {**LOGIN, "api_key": OTHER_KEY})
            reported, quiet = self.run_cli(home), self.run_cli(home)
            self.assertEqual(len(reported["notices"]), 1)
            self.assertNotIn("notices", quiet)
            self.assertEqual(json.loads((new / "auth.json").read_text())["api_key"], KEY)


if __name__ == "__main__":
    unittest.main()
