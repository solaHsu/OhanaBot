import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.apps._http import ServiceError
from bot.apps.desktop import dispatch


class DesktopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "workspace.sqlite3"
        base = patch.dict(os.environ, {"OHANA_MODEL_URL": "", "OHANA_MODEL": ""})
        base.start()
        self.addCleanup(base.stop)

    def action(self, action, **payload):
        return dispatch(self.database, {"action": action, "payload": payload})

    def test_chat_settings_and_activity_are_saved_in_the_workspace(self):
        initial = self.action("load")["data"]
        self.assertEqual(initial["activities"], [])
        self.assertIsNone(initial["conversation"])

        saved = self.action("save_app", app="qq", groups=[" 123 ", "123", "456"], accept_private=True)["data"]
        self.assertEqual(saved["apps"]["qq"], {"groups": ["123", "456"], "accept_private": True})
        self.assertEqual(saved["apps"]["feishu"], {"groups": [], "accept_private": False})

        sent = self.action("send", conversation=None, text="海棠 🌸")["data"]
        old_id = sent["conversation"]
        self.assertEqual(sent["messages"][-1]["text"], "回显：海棠 🌸")
        self.assertEqual([item["category"] for item in sent["activities"]], ["chat", "settings"])
        self.assertEqual(self.action("load")["data"], sent)

        new = self.action("open_chat")["data"]
        self.assertEqual(new["messages"], [])
        self.assertNotEqual(new["conversation"], old_id)
        self.assertIn("error", self.action("send", conversation=old_id, text="过时的会话"))
        self.assertEqual(self.action("restore_chat", conversation=old_id)["data"]["messages"], sent["messages"])

    @patch.dict(os.environ, {"OHANA_MODEL_URL": "http://localhost/model", "OHANA_MODEL": "test"})
    @patch("bot.apps.chat.request_json", side_effect=ServiceError("HTTP 503"))
    def test_chat_failure_is_visible_and_does_not_add_a_fake_reply(self, _):
        data = self.action("send", text="你好", conversation=None)["data"]
        self.assertEqual([message["role"] for message in data["messages"]], ["input"])
        self.assertIn("HTTP 503", data["notice"])
        self.assertTrue(data["reply_failed"])
        self.assertEqual(data["activities"][0]["status"], "error")

    @patch("bot.apps.desktop.Workspace.record", side_effect=sqlite3.OperationalError("busy"))
    def test_activity_failure_does_not_report_a_saved_reply_as_failed(self, _):
        data = self.action("send", text="你好", conversation=None)["data"]
        self.assertEqual(data["messages"][-1]["text"], "回显：你好")
        self.assertFalse(data["reply_failed"])
        self.assertIn("回复已保存", data["notice"])
