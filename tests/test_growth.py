"""记忆更新输出的测试：围栏容错、维度前缀清洗、缺失字段报错、自身排除与快照导出。

运行：python -m unittest discover -s tests -v
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.engine.growth import refine, snapshot
from bot.engine.growth.update import _involved_users, clean_basic, parse_update_output
from bot.engine.storage import db
from bot.engine.storage.memory_store import NAME_ALIAS, MemoryStore


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

    def test_parse_remove_aliases(self):
        raw = (
            '{"log": "记录", "profiles": ['
            '{"user_id": "u1", "basic": {"interests": "关注游戏建模"},'
            ' "aliases": ["羊羊"], "remove_aliases": ["威魔"], "entries": ["9月中：上线"]}]}'
        )
        profile = parse_update_output(raw)["profiles"][0]
        self.assertEqual(profile["aliases"], ["羊羊"])
        self.assertEqual(profile["remove_aliases"], ["威魔"])
        self.assertEqual(profile["entries"], ["9月中：上线"])


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


class ApplyUpdateAliasTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.store = MemoryStore(self.conn)
        self.store.ensure_conversation("qq", "group:1", "测试群")

    def test_remove_aliases_retracts_wrong_alias_only(self):
        self.store.add_alias("qq", "group:1", "u1", "威魔", "2026-09-18T00:00:00Z")
        self.store.add_alias("qq", "group:1", "u2", "威魔", "2026-09-18T00:00:00Z")
        counters = self.store.apply_update(
            "qq",
            "group:1",
            [{"user_id": "u1", "basic": {}, "aliases": [], "remove_aliases": ["威魔"], "entries": []}],
            "一段记录",
            msg_from=1,
            msg_to=2,
            seen_at="2026-09-18T00:00:00Z",
        )
        self.assertEqual(counters["aliases_removed"], 1)
        self.assertEqual(self.store.list_names("qq", "group:1", "u1", NAME_ALIAS), [])
        self.assertEqual(
            [n["text"] for n in self.store.list_names("qq", "group:1", "u2", NAME_ALIAS)], ["威魔"]
        )


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.store = MemoryStore(self.conn)
        self.store.ensure_conversation("qq", "group:1", "测试群")

    def test_parse_refine_output_with_fence(self):
        raw = '```json\n{"basic": {"personality": "性格：爱折腾"}, "entries": ["9月中：上线"]}\n```'
        parsed = refine.parse_refine_output(raw)
        self.assertEqual(parsed["basic"]["personality"], "爱折腾")
        self.assertEqual(parsed["entries"], ["9月中：上线"])

    def test_parse_refine_missing_basic_raises(self):
        with self.assertRaises(ValueError):
            refine.parse_refine_output('{"entries": []}')

    def test_refine_rewrites_basic_and_moves_events(self):
        self.store.upsert_person_basic("qq", "group:1", "u1", {"interests": "本批关注游戏建模"})
        self.store.add_entry("qq", "group:1", "u1", "9月中：上线")
        # 只有昵称、无资料的人应被跳过
        self.store.record_group_nickname("qq", "group:1", "u2", "路人", "2026-09-17T10:00:00Z")

        def generate(prompt):
            self.assertIn("现有资料", prompt)
            return '{"basic": {"interests": "关注游戏建模"}, "entries": ["9月中：改昵称"]}'

        report = refine.refine_profiles(self.store, generate, "qq", "group:1")
        self.assertEqual(report["refined"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(self.store.get_person("qq", "group:1", "u1")["basic"]["interests"], "关注游戏建模")
        texts = [e["text"] for e in self.store.list_entries("qq", "group:1", "u1")]
        self.assertIn("9月中：上线", texts)
        self.assertIn("9月中：改昵称", texts)

    def test_refine_skips_near_duplicate_entries(self):
        self.store.upsert_person_basic("qq", "group:1", "u1", {"relations": "常和老王互动"})
        self.store.add_entry("qq", "group:1", "u1", "9月18日：发PV卡片和沃图。")

        def generate(prompt):
            return (
                '{"basic": {"relations": "和老王常互动"},'
                ' "entries": ["9月18日：发PV卡片和沃图，随后感叹龙王没了。"]}'
            )

        report = refine.refine_profiles(self.store, generate, "qq", "group:1")
        self.assertEqual(report["entries_added"], 0)
        self.assertEqual(len(self.store.list_entries("qq", "group:1", "u1")), 1)

    def test_refine_dry_run_does_not_write(self):
        self.store.upsert_person_basic("qq", "group:1", "u1", {"interests": "旧"})
        report = refine.refine_profiles(
            self.store,
            lambda prompt: '{"basic": {"interests": "新"}, "entries": []}',
            "qq",
            "group:1",
            apply=False,
        )
        self.assertEqual(report["refined"], 1)
        self.assertEqual(self.store.get_person("qq", "group:1", "u1")["basic"]["interests"], "旧")


if __name__ == "__main__":
    unittest.main()
