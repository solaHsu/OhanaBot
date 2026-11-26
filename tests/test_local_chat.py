import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPRedirectHandler

from bot.apps._http import ServiceError, request_json as send_json
from bot.apps.chat import main


class LocalChatTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    @patch("bot.apps.chat.request_json")
    def test_echo_is_offline_and_history_can_be_opened_after_restart(self, request_json):
        with tempfile.TemporaryDirectory() as root:
            db = str(Path(root) / "chat.sqlite3")
            args = ["--echo", "--debug", "--db", db]
            status, stdout, stderr = self.run_cli([*args, "--once", "项目叫海棠"])
            self.assertEqual((status, stdout), (0, "回显：项目叫海棠\n"))
            conversation = stderr.splitlines()[0].removeprefix("会话 ID：")
            self.assertIn("上下文 1/1 条", stderr)

            with patch("builtins.input", side_effect=["/history", "它叫什么？", "/exit"]):
                status, stdout, stderr = self.run_cli([*args, "--conversation", conversation])
            self.assertEqual(status, 0)
            self.assertIn("你> 项目叫海棠", stdout)
            self.assertIn("Ohana> 回显：项目叫海棠", stdout)
            self.assertIn("Ohana> 回显：它叫什么？", stdout)
            self.assertIn("上下文 3/3 条", stderr)
            request_json.assert_not_called()

    @patch.dict("os.environ", {
        "OHANA_MODEL_URL": "http://localhost/chat/completions",
        "OHANA_MODEL": "test-model",
        "OHANA_MODEL_API_KEY": "test-key",
    })
    @patch("bot.apps.chat.request_json")
    def test_model_receives_user_and_assistant_history_with_context_limit(self, request_json):
        request_json.side_effect = [
            {"choices": [{"message": {"content": "我记住了海棠。"}}]},
            {"choices": [{"message": {"content": "代号是海棠。"}}]},
            {"choices": [{"message": {"content": "已经确认。"}}]},
        ]
        with tempfile.TemporaryDirectory() as root:
            args = ["--db", str(Path(root) / "chat.sqlite3"), "--debug"]
            status, _, stderr = self.run_cli([*args, "--once", "项目叫海棠"])
            self.assertEqual(status, 0)
            conversation = stderr.splitlines()[0].removeprefix("会话 ID：")
            resume = [*args, "--conversation", conversation]
            status, stdout, _ = self.run_cli([*resume, "--once", "它叫什么？"])
            self.assertEqual((status, stdout), (0, "代号是海棠。\n"))
            continued = json.loads(request_json.call_args.kwargs["body"])["messages"]
            self.assertEqual(continued[0], {"role": "user", "content": "项目叫海棠"})
            status, stdout, stderr = self.run_cli([
                *resume, "--context-messages", "4", "--once", "已经确认了吗？",
            ])
        self.assertEqual((status, stdout), (0, "已经确认。\n"))
        request = request_json.call_args.kwargs
        self.assertEqual(json.loads(request["body"]), {"model": "test-model", "messages": [
            {"role": "user", "content": "它叫什么？"},
            {"role": "assistant", "content": "代号是海棠。"},
            {"role": "user", "content": "已经确认了吗？"},
        ]})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        self.assertIn("上下文 3/5 条", stderr)
        self.assertNotIn("test-key", stderr)

    @patch.dict("os.environ", {"OHANA_MODEL_URL": "http://localhost/chat/completions", "OHANA_MODEL": "test-model"})
    @patch("bot.apps.chat.request_json")
    def test_interactive_model_error_keeps_session_open(self, request_json):
        request_json.side_effect = [
            ServiceError("HTTP 503"),
            {"choices": [{"finish_reason": "length", "message": {"content": "部分文本"}}]},
            {"choices": [{"message": {"content": "已恢复"}}]},
        ]
        with patch("builtins.input", side_effect=["第一次", "第二次", "第三次", "/history", "/exit"]):
            status, stdout, stderr = self.run_cli(["--db", ":memory:"])
        self.assertEqual(status, 0)
        self.assertIn("错误：模型调用失败：HTTP 503", stderr)
        self.assertIn("错误：模型调用失败：模型回复被截断", stderr)
        self.assertIn("你> 第一次", stdout)
        self.assertIn("你> 第二次", stdout)
        self.assertIn("Ohana> 已恢复", stdout)
        self.assertNotIn("部分文本", stdout)
        self.assertEqual(json.loads(request_json.call_args.kwargs["body"])["messages"], [
            {"role": "user", "content": "第三次"},
        ])

    @patch("bot.apps._http.urlopen")
    def test_model_authorization_is_sent_only_to_initial_address(self, urlopen):
        urlopen.return_value = io.BytesIO(b"{}")
        send_json("https://first.invalid/chat", headers={"Authorization": "Bearer test"}, body=b"{}")
        original = urlopen.call_args.args[0]
        redirected = HTTPRedirectHandler().redirect_request(
            original, None, 302, "Found", {}, "https://second.invalid/chat",
        )
        self.assertEqual(original.get_header("Authorization"), "Bearer test")
        self.assertIsNone(redirected.get_header("Authorization"))

    @unittest.skipUnless(os.name == "nt", "Windows pipe encoding")
    def test_module_entry_accepts_and_prints_utf8_through_windows_pipes(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "bot.apps.chat", "--echo", "--db", ":memory:"],
            input="海棠 🌸\n/exit\n".encode(), capture_output=True, timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "gbk"},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("Ohana> 回显：海棠 🌸", result.stdout.decode())
