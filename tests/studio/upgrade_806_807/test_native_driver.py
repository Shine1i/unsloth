# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0
"""Process-selection regression only; never launches a backend or release binary."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("upgrade_native_driver", Path(__file__).with_name("native_driver.py"))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class RelaunchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.driver = object.__new__(native.Driver)
        self.driver.args = SimpleNamespace(app=Path("/release/Unsloth.AppImage"))
        self.driver.relaunch_at = 100.9  # Old predicate: incomparable wall-clock ordering.
        self.driver.pre_restart_pids = {100, 5298}
        self.driver.wait = lambda probe, description: probe()
        self.driver.health = Mock(return_value=True)
        self.driver.backend_version = Mock(return_value=native.NEW_BACKEND)
        self.driver.record = Mock()
        self.driver.wd = Mock()
        self.driver.session = "old"
        self.driver.new_linux_session = Mock()

    def process(self, pid, created=100.0, name="unsloth-studio", appimage=None):
        proc = Mock()
        proc.pid = pid
        proc.info = {"pid": pid, "name": name, "create_time": created}
        proc.environ.return_value = {"APPIMAGE": appimage or str(self.driver.args.app)}
        return proc

    def reconnect(self, processes):
        psutil = SimpleNamespace(process_iter=lambda attrs: processes,
                                 wait_procs=lambda procs, timeout: (procs, []),
                                 Error=RuntimeError)
        with patch.dict(sys.modules, psutil=psutil):
            self.driver.linux_reconnect()

    def test_new_pid_with_timestamp_before_request_is_accepted(self):
        # Run2: matching PIDs6100/6104 were rejected solely because their reported
        # create times preceded time.time() at the restart request.
        replacement = self.process(6104)
        self.reconnect([replacement])
        replacement.terminate.assert_called_once()
        self.driver.new_linux_session.assert_called_once()

    def test_old_pid_cannot_be_accepted_even_with_later_timestamp(self):
        old = self.process(100, created=101.0)
        with self.assertRaisesRegex(RuntimeError, "Cannot prove/identify"):
            self.reconnect([old])
        old.terminate.assert_not_called()
        self.driver.new_linux_session.assert_not_called()

    def test_other_app_and_non_shell_child_are_never_terminated(self):
        replacement = self.process(6104, created=101.0)
        other_app = self.process(6105, created=101.0, appimage="/other/Unsloth.AppImage")
        child = self.process(6310, created=101.0, name="python")
        self.reconnect([replacement, other_app, child])
        replacement.terminate.assert_called_once()
        other_app.terminate.assert_not_called()
        child.terminate.assert_not_called()

    def test_wrong_activated_backend_prevents_any_termination(self):
        replacement = self.process(6104, created=101.0)
        self.driver.backend_version.return_value = native.OLD_BACKEND
        with self.assertRaisesRegex(RuntimeError, "metadata mismatch"):
            self.reconnect([replacement])
        replacement.terminate.assert_not_called()
        self.driver.new_linux_session.assert_not_called()


class AttachmentDiagnosticsTests(unittest.TestCase):
    def test_wait_retains_redacted_connection_reason(self):
        driver = object.__new__(native.Driver)
        driver.deadline = 10
        driver.args = SimpleNamespace(timeout=1)
        probe = Mock(side_effect=RuntimeError("CDP ECONNREFUSED token=private123"))
        with patch.object(native.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(native.time, "sleep"):
            with self.assertRaises(TimeoutError) as caught:
                driver.wait(probe, "release CDP", budget=1)
        self.assertIn("ECONNREFUSED", str(caught.exception))
        self.assertNotIn("private123", str(caught.exception))



if __name__ == "__main__":
    unittest.main()
