"""判定运行时的测试：批窗口、生成、发送、dry-run、指令。

运行：python -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.engine.config import EngineConfig, OneBotConfig
from bot.engine.context import parse_reply
from bot.engine.onebot.parser import ImageRef, IncomingMessage
from bot.engine.runtime import EngineRuntime
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore

COUNTER = itertools.count(1)


def make_msg(
    text: str, *, user_id: str = "10001", mentions_self: bool = False, images: tuple = ()
) -> IncomingMessage:
    return IncomingMessage(
        conversation_id="group:888888",
        conversation_type="group",
        message_id=f"m-{next(COUNTER)}",
        user_id=user_id,
        display_name=f"用户{user_id}",
        text=text,
        mentions=(),
        mentions_self=mentions_self,
        replies_to=None,
        sent_at="2026-09-16T12:00:00Z",
        images=tuple(images),
    )


class ParseReplyTests(unittest.TestCase):
    def test_valid_reply(self):
        parsed = parse_reply('{"action": "reply", "text": "好呀"}')
        self.assertEqual(parsed, {"action": "reply", "text": "好呀"})

    def test_silence(self):
        parsed = parse_reply('{"action": "silence", "text": ""}')
        self.assertEqual(parsed["action"], "silence")

    def test_fence_tolerated(self):
        parsed = parse_reply('```json\n{"action": "reply", "text": "嗯"}\n```')
        self.assertEqual(parsed["text"], "嗯")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_reply("随便说点什么")
        with self.assertRaises(ValueError):
            parse_reply('{"action": "reply", "text": ""}')
        with self.assertRaises(ValueError):
            parse_reply('{"action": "sing", "text": "啊"}')


class RuntimeTests(unittest.TestCase):
    def _make(self, *, mode="dry_run", send_enabled=False, replies_per_hour=20, admins=("1",), reply_json=None):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "rt.sqlite3"
        conn = db.connect(db_path)
        db.migrate(conn)
        self.addCleanup(conn.close)
        store = MemoryStore(conn)
        store.ensure_conversation("qq", "group:888888")
        config = EngineConfig(
            mode=mode,
            database=db_path,
            send_enabled=send_enabled,
            replies_per_hour=replies_per_hour,
            admins=list(admins),
            onebot=OneBotConfig(expected_self_id="424242", allowed_groups=["888888"]),
        )
        calls = {"prompts": [], "sends": [], "images": []}
        payload = reply_json or '{"action": "reply", "text": "收到捏"}'

        def generate(prompt: str, images=None) -> str:
            calls["prompts"].append(prompt)
            calls["images"].append(list(images or []))
            return payload

        def send_group(conversation_id: str, text: str) -> dict:
            calls["sends"].append((conversation_id, text))
            return {"status": "ok", "retcode": 0, "data": {"message_id": "s-1"}}

        now = [datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)]
        runtime = EngineRuntime(config, store, generate, send_group, clock=lambda: now[0])
        return runtime, store, calls, now

    def test_hard_trigger_batches_then_drafts_in_dry_run(self):
        runtime, store, calls, now = self._make()
        runtime.handle(make_msg("在吗", mentions_self=True))
        self.assertEqual(calls["prompts"], [])  # 未到 3 秒批窗口
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(len(calls["prompts"]), 1)
        self.assertIn("在吗", calls["prompts"][0])
        self.assertEqual(calls["sends"], [])  # dry-run 不发送
        self.assertEqual(store.list_recent_messages("qq", "group:888888"), [])

    def test_live_mode_sends_and_records_own_message(self):
        runtime, store, calls, now = self._make(mode="live", send_enabled=True)
        runtime.handle(make_msg("帮我看看", mentions_self=True))
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(calls["sends"], [("group:888888", "收到捏")])
        rows = store.list_recent_messages("qq", "group:888888")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "424242")
        self.assertEqual(rows[0]["text"], "收到捏")

    def test_batch_images_attached(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pic.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        runtime, store, calls, now = self._make()
        runtime.handle(
            make_msg("看看这张图", mentions_self=True, images=(ImageRef(url="", file=str(path)),))
        )
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(len(calls["images"][0]), 1)
        self.assertTrue(calls["images"][0][0].startswith("data:image/png;base64,"))
        self.assertIn("已随提示一并提供", calls["prompts"][0])

    def test_must_reply_silence_is_skipped(self):
        runtime, store, calls, now = self._make(
            mode="live", send_enabled=True, reply_json='{"action": "silence", "text": ""}'
        )
        runtime.handle(make_msg("在吗", mentions_self=True))
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(calls["sends"], [])  # 硬触发不允许沉默 → 跳过
        self.assertEqual(store.list_recent_messages("qq", "group:888888"), [])

    def test_hourly_call_limit(self):
        runtime, store, calls, now = self._make(replies_per_hour=1)
        runtime.handle(make_msg("第一条", mentions_self=True))
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(len(calls["prompts"]), 1)
        runtime.handle(make_msg("第二条", mentions_self=True))
        now[0] += timedelta(seconds=4)
        runtime.tick()
        self.assertEqual(len(calls["prompts"]), 1)  # 超限被跳过

    def test_exit_command_switches_to_observing(self):
        runtime, store, calls, now = self._make()
        runtime.handle(make_msg("在吗", mentions_self=True))
        conv = runtime.state("group:888888")
        self.assertEqual(conv.policy.phase, "engaged")
        runtime.handle(make_msg("/exit", user_id="1"))
        self.assertEqual(conv.policy.phase, "observing")
        self.assertEqual(conv.pending, [])

    def test_non_admin_exit_ignored(self):
        runtime, store, calls, now = self._make()
        runtime.handle(make_msg("在吗", mentions_self=True))
        runtime.handle(make_msg("/exit", user_id="999"))
        conv = runtime.state("group:888888")
        self.assertEqual(conv.policy.phase, "engaged")


class ConfigDefaultsTests(unittest.TestCase):
    def test_engine_defaults(self):
        config = EngineConfig()
        self.assertEqual(config.merge_window_seconds, 3.0)
        self.assertEqual(config.replies_per_hour, 20)
        self.assertEqual(config.max_reply_characters, 300)
        self.assertEqual(config.max_images_per_call, 3)
        self.assertEqual(config.image_max_dimension, 768)
        self.assertEqual(config.onebot.self_aliases, ["ohana", "欧哈娜"])


if __name__ == "__main__":
    unittest.main()
