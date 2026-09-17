"""Harness 桥接的测试：启用条件、结果清洗、失败与超时（含进程树终止）。

运行：python -m unittest discover -s tests -v
"""

import subprocess
import tempfile
import unittest

from bot.tools import harness


class _FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, timeout_first=False):
        self.pid = 4242
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_first = timeout_first

    def communicate(self, timeout=None):
        if self._timeout_first:
            self._timeout_first = False
            raise subprocess.TimeoutExpired(cmd="dsh", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9


class HarnessBridgeTests(unittest.TestCase):
    def _bridge(self, **overrides):
        bridge = harness.HarnessBridge(enabled=True, command="dsh", api_key="k", **overrides)
        bridge._resolve_command = lambda: "dsh-fake"
        return bridge

    def _patch_popen(self, factory):
        original = harness.subprocess.Popen
        harness.subprocess.Popen = factory
        self.addCleanup(setattr, harness.subprocess, "Popen", original)

    def test_disabled_returns_none(self):
        bridge = harness.HarnessBridge(enabled=False)
        self.assertIsNone(bridge.run_task("任务"))

    def test_unavailable_returns_none(self):
        bridge = harness.HarnessBridge(enabled=True, command="no-such-cmd-xyz-000")
        self.assertFalse(bridge.available)
        self.assertIsNone(bridge.run_task("任务"))

    def test_success_strips_ansi(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bridge = self._bridge(workspace=tmp.name)
        captured = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            return _FakeProcess(stdout="\x1b[1m结果文本\x1b[0m\n", stderr="思考流", returncode=0)

        self._patch_popen(fake_popen)
        self.assertEqual(bridge.run_task("任务"), "结果文本")
        self.assertEqual(captured.get("cwd"), tmp.name)

    def test_nonzero_exit_returns_none(self):
        bridge = self._bridge()
        self._patch_popen(lambda argv, **kwargs: _FakeProcess(stderr="boom", returncode=1))
        self.assertIsNone(bridge.run_task("任务"))

    def test_timeout_kills_tree_and_returns_none(self):
        bridge = self._bridge()
        self._patch_popen(lambda argv, **kwargs: _FakeProcess(timeout_first=True))
        killed = []
        original_kill = harness._kill_tree
        harness._kill_tree = lambda proc: killed.append(proc.pid)
        self.addCleanup(setattr, harness, "_kill_tree", original_kill)
        self.assertIsNone(bridge.run_task("任务"))
        self.assertEqual(killed, [4242])

    def test_empty_task_returns_none(self):
        bridge = self._bridge()
        self.assertIsNone(bridge.run_task(""))


if __name__ == "__main__":
    unittest.main()
