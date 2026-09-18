"""记忆更新输出的测试：围栏容错、维度前缀清洗、缺失字段报错、自身排除与快照导出。

运行：python -m unittest discover -s tests -v
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.engine.growth import snapshot
from bot.engine.growth.update import _involved_users, clean_basic, parse_update_output
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore


class ParseUpdateTests(unittest.TestCase):
    def test_parse_with_fence_and_profiles(self):
        raw = (
            '```json\n{"log": "一段记录", "profiles": ['
            '{"user_id": "u1", "basic": {"personality": "性格：爱折腾", "other": "其他"},'
            ' "aliases": ["小纳"], "entries": ["9月16日上线"]}]}\n```'
        )
        parsed = parse_update_output(raw)
        self.assertEqual(parsed["log"], "一段记录")
        profile = parsed["profiles"][0]
        self.assertEqual(profile["user_id"], "u1")
        self.assertEqual(profile["basic"]["personality"], "爱折腾")
        self.assertEqual(profile["basic"]["other"], "其他")
        self.assertEqual(profile["aliases"], ["小纳"])
        self.assertEqual(profile["entries"], ["9月16日上线"])

    def test_missing_log_raises(self):
        with self.assertRaises(ValueError):
            parse_update_output('{"profiles": []}')

    def test_not_json_raises(self):
        with self.assertRaises(ValueError):
            parse_update_output("不是 json")

    def test_profile_without_user_id_skipped(self):
        parsed = parse_update_output('{"log": "x", "profiles": [{"basic": {}}]}')
        self.assertEqual(parsed["profiles"], [])


class InvolvedUsersTests(unittest.TestCase):
    def test_excludes_self_as_sender_and_mentioned(self):
        batch = [
            {"user_id": "self", "display_name": "Ohana", "mentions": ["u2"]},
            {"user_id": "u1", "display_name": "甲", "mentions": ["self", "u3"]},
        ]
        users = _involved_users(batch, ("self",))
        self.assertEqual(set(users), {"u1", "u2", "u3"})

    def test_without_exclusion_keeps_all(self):
        batch = [{"user_id": "self", "display_name": "Ohana", "mentions": []}]
        self.assertEqual(set(_involved_users(batch)), {"self"})


class CleanBasicTests(unittest.TestCase):
    def test_strips_label_prefix(self):
        cleaned = clean_basic(
            {"personality": "性格：爱折腾", "speech": "说话风格: 简短", "other": "其他"}
        )
        self.assertEqual(cleaned["personality"], "爱折腾")
        self.assertEqual(cleaned["speech"], "简短")
        self.assertEqual(cleaned["other"], "其他")

    def test_keeps_plain_value(self):
        self.assertEqual(clean_basic({"interests": "AI、游戏"})["interests"], "AI、游戏")

    def test_non_dict_returns_empty(self):
        self.assertEqual(clean_basic(None), {})
        self.assertEqual(clean_basic("x"), {})


class SnapshotExportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.store = MemoryStore(self.conn)
        self.store.ensure_conversation("qq", "group:1", "测试群")
        self.store.add_message(
            "qq", "group:1", "m1", "self", "Ohana", "大家好", "2026-09-17T10:00:00Z"
        )
        self.store.add_message(
            "qq", "group:1", "m2", "u1", "甲", "你好", "2026-09-17T10:01:00Z"
        )
        self.store.refresh_nicknames("qq", "group:1", up_to_id=999)

    def test_export_excludes_self(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        updated = snapshot.export_all(self.store, Path(tmp.name), exclude_user_ids=("self",))
        names = {path.name for path in updated}
        self.assertIn("u1.md", names)
        self.assertNotIn("self.md", names)

    def test_export_without_exclusion_includes_self(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        updated = snapshot.export_all(self.store, Path(tmp.name))
        names = {path.name for path in updated}
        self.assertIn("self.md", names)
        self.assertIn("u1.md", names)


if __name__ == "__main__":
    unittest.main()
