"""发言引擎判定核心的回放测试：虚拟时钟 + 纯函数，零 token。

运行：python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bot.engine.config import load_config
from bot.engine.policy.interests import (
    InterestCategory,
    InterestSet,
    extract_keywords,
    load_interests,
)
from bot.engine.policy.machine import (
    ATTENTIVE,
    ENGAGED,
    OBSERVING,
    ConversationPolicy,
    MessageEvent,
    PolicyConfig,
)

CATEGORIES = [
    InterestCategory("AI", ("ai", "agent"), 0),
    InterestCategory("游戏", ("游戏",), 1),
]
NOW = datetime(2026, 9, 16, 12, 0, 0)


def msg(text: str, **kwargs) -> MessageEvent:
    return MessageEvent(sender_id=kwargs.pop("sender", "p001"), text=text, **kwargs)


class PolicyTests(unittest.TestCase):
    def make(self, rng=None, **config) -> ConversationPolicy:
        return ConversationPolicy(
            CATEGORIES,
            self_names=["ohana"],
            config=PolicyConfig(**config),
            rng=rng or (lambda: 0.99),  # 默认关闭幸运升级，保证回放确定性
        )

    def test_hard_trigger_mention_enters_engaged(self):
        machine = self.make()
        decision = machine.on_message(msg("在吗", mentions_ohana=True), NOW)
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.action, "generate")
        self.assertTrue(decision.must_reply)
        self.assertEqual(decision.reason, "reply:mention")

    def test_name_and_reply_triggers(self):
        machine = self.make()
        self.assertTrue(machine.on_message(msg("ohana 帮个忙"), NOW).must_reply)
        other = self.make()
        decision = other.on_message(msg("这个怎么搞", replies_to_ohana=True), NOW)
        self.assertTrue(decision.must_reply)
        self.assertEqual(decision.reason, "reply:reply")

    def test_self_message_never_triggers(self):
        machine = self.make()
        decision = machine.on_message(msg("大家好", from_ohana=True), NOW)
        self.assertIsNone(decision.action)
        self.assertEqual(decision.phase, OBSERVING)

    def test_interest_ladder_two_messages(self):
        machine = self.make()
        first = machine.on_message(msg("有人在聊 ai 吗"), NOW)
        self.assertEqual(first.phase, OBSERVING)
        self.assertIn("AI", machine.tracking)
        second = machine.on_message(msg("agent 怎么做", sender="p002"), NOW + timedelta(seconds=10))
        self.assertEqual(second.phase, ATTENTIVE)
        self.assertIsNone(second.action)
        self.assertEqual(second.reason, "attend:interest")

    def test_same_message_not_re_hit(self):
        # 一条消息里同时出现两个成员词：只算一次命中，不构成"再次命中"
        machine = self.make()
        machine.on_message(msg("ai 和 agent"), NOW)
        self.assertEqual(machine.phase, OBSERVING)

    def test_ladder_window_expiry(self):
        machine = self.make()
        machine.on_message(msg("聊聊 ai"), NOW)
        for i in range(10):
            machine.on_message(msg(f"闲聊 {i}", sender="p003"), NOW + timedelta(seconds=i + 1))
        decision = machine.on_message(msg("agent 部署", sender="p004"), NOW + timedelta(seconds=20))
        self.assertEqual(decision.phase, OBSERVING)
        self.assertIn("AI", machine.tracking)

    def test_different_category_does_not_upgrade(self):
        machine = self.make()
        machine.on_message(msg("聊聊 ai"), NOW)
        decision = machine.on_message(msg("开黑打游戏", sender="p002"), NOW + timedelta(seconds=5))
        self.assertEqual(decision.phase, OBSERVING)

    def test_attend_timeout_returns_observing(self):
        machine = self.make(attend_window_minutes=2)
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        self.assertEqual(machine.phase, ATTENTIVE)
        decision = machine.tick(NOW + timedelta(seconds=1, minutes=3))
        self.assertEqual(decision.phase, OBSERVING)
        self.assertEqual(decision.reason, "attend:timeout")

    def test_attend_window_relaxed_to_15_minutes(self):
        machine = self.make()
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        decision = machine.on_message(msg("ai 又来了", sender="p003"), NOW + timedelta(minutes=10))
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.reason, "engage:interest")

    def test_attend_default_window_timeout(self):
        machine = self.make()
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        decision = machine.tick(NOW + timedelta(minutes=16))
        self.assertEqual(decision.phase, OBSERVING)
        self.assertEqual(decision.reason, "attend:timeout")

    def test_lucky_engage_promotes_directly(self):
        machine = self.make(rng=lambda: 0.0)
        decision = machine.on_message(msg("今天天气不错"), NOW)
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.action, "generate")
        self.assertFalse(decision.must_reply)
        self.assertEqual(decision.reason, "engage:lucky")

    def test_lucky_engage_from_attentive(self):
        rolls = iter([0.99, 0.99, 0.0])
        machine = self.make(rng=lambda: next(rolls))
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        self.assertEqual(machine.phase, ATTENTIVE)
        decision = machine.on_message(msg("随便说点什么", sender="p003"), NOW + timedelta(seconds=2))
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.reason, "engage:lucky")

    def test_lucky_engage_disabled_by_probability(self):
        machine = self.make(lucky_engage_probability=0, rng=lambda: 0.0)
        decision = machine.on_message(msg("今天天气不错"), NOW)
        self.assertEqual(decision.phase, OBSERVING)

    def test_lucky_roll_miss_keeps_normal_flow(self):
        machine = self.make(rng=lambda: 0.5)
        decision = machine.on_message(msg("聊聊 ai"), NOW)
        self.assertEqual(decision.phase, OBSERVING)
        self.assertIn("AI", machine.tracking)

    def test_self_message_not_lucky(self):
        machine = self.make(rng=lambda: 0.0)
        decision = machine.on_message(msg("大家好", from_ohana=True), NOW)
        self.assertEqual(decision.phase, OBSERVING)
        self.assertIsNone(decision.action)

    def test_attend_hit_engages(self):
        machine = self.make()
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        decision = machine.on_message(msg("ai 真强", sender="p003"), NOW + timedelta(seconds=30))
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.action, "generate")
        self.assertFalse(decision.must_reply)
        self.assertEqual(decision.reason, "engage:interest")

    def test_attentive_hard_trigger(self):
        machine = self.make()
        machine.on_message(msg("聊 ai"), NOW)
        machine.on_message(msg("agent"), NOW + timedelta(seconds=1))
        decision = machine.on_message(msg("ohana 看看", mentions_ohana=True), NOW + timedelta(seconds=20))
        self.assertEqual(decision.phase, ENGAGED)
        self.assertTrue(decision.must_reply)

    def test_engaged_decay_by_messages_and_keyword_rebuild(self):
        machine = self.make()
        machine.on_message(msg("ohana 在吗"), NOW)  # 硬触发 → 参与
        for i in range(11):
            decision = machine.on_message(
                msg(f"普通消息 {i} 服务器 讨论", sender="p00x"), NOW + timedelta(seconds=i + 1)
            )
            self.assertEqual(decision.phase, ENGAGED)
            self.assertEqual(decision.action, "generate")
        decision = machine.on_message(msg("第十二条 内容", sender="p00x"), NOW + timedelta(seconds=20))
        self.assertEqual(decision.phase, ATTENTIVE)
        self.assertIsNone(decision.action)
        self.assertEqual(decision.reason, "decay:engaged")
        self.assertTrue(machine.interest_set.items)  # 退出时已重建（含动态关键词）

    def test_engaged_dynamic_keyword_reattracts(self):
        machine = self.make()
        machine.on_message(msg("ohana 在吗"), NOW)
        for i in range(12):
            machine.on_message(msg(f"服务器 迁移 讨论 {i}", sender="p00x"), NOW + timedelta(seconds=i + 1))
        self.assertEqual(machine.phase, ATTENTIVE)
        probe = machine.interest_set.items[0].words[0]  # 用集内实际的词探测
        decision = machine.on_message(msg(f"{probe} 怎么样了", sender="p00y"), NOW + timedelta(seconds=60))
        self.assertEqual(decision.phase, ENGAGED)
        self.assertEqual(decision.reason, "engage:interest")

    def test_engaged_touch_by_reply_and_nearby(self):
        machine = self.make()
        machine.on_message(msg("ohana"), NOW)
        machine.on_message(msg("甲", sender="a"), NOW + timedelta(seconds=1))
        machine.on_message(msg("我说了一句", from_ohana=True), NOW + timedelta(seconds=2))  # 自己发言 → 刷新
        self.assertEqual(machine.messages_since_touch, 0)
        machine.on_message(msg("哦哦", sender="b"), NOW + timedelta(seconds=30))  # 紧邻接话 → 刷新
        self.assertEqual(machine.messages_since_touch, 0)
        machine.on_message(msg("继续说", sender="c"), NOW + timedelta(seconds=40))  # 不再算紧邻
        self.assertEqual(machine.messages_since_touch, 1)

    def test_engaged_time_decay(self):
        machine = self.make()
        machine.on_message(msg("ohana"), NOW)
        decision = machine.tick(NOW + timedelta(minutes=16))
        self.assertEqual(decision.phase, ATTENTIVE)
        self.assertEqual(decision.reason, "decay:engaged")

    def test_exit_command(self):
        machine = self.make()
        machine.on_message(msg("聊 ai"), NOW)  # 兴趣入集
        machine.on_message(msg("ohana 在吗"), NOW + timedelta(seconds=1))  # 进入参与
        self.assertTrue(machine.interest_set.items)
        decision = machine.exit_command()
        self.assertEqual(decision.phase, OBSERVING)
        self.assertEqual(machine.interest_set.items, [])
        self.assertEqual(machine.tracking, {})

    def test_after_exit_hard_trigger_still_works(self):
        machine = self.make()
        machine.exit_command()
        decision = machine.on_message(msg("ohana", mentions_ohana=True), NOW)
        self.assertEqual(decision.phase, ENGAGED)
        self.assertTrue(decision.must_reply)


class InterestSetTests(unittest.TestCase):
    def test_capacity_eviction_prefers_keywords_first(self):
        interest_set = InterestSet(capacity=2)
        interest_set.rebuild([InterestCategory("AI", ("ai",), 0)], [("服务器", 3)])
        self.assertEqual(len(interest_set.items), 2)
        interest_set.ensure_category(InterestCategory("游戏", ("游戏",), 1))  # 挤掉动态词
        names = [item.name for item in interest_set.items]
        self.assertIn("游戏", names)
        self.assertNotIn("服务器", names)

    def test_lower_priority_category_not_added_when_full(self):
        interest_set = InterestSet(capacity=2)
        interest_set.ensure_category(InterestCategory("A", ("a",), 0))
        interest_set.ensure_category(InterestCategory("B", ("b",), 1))
        interest_set.ensure_category(InterestCategory("C", ("c",), 2))  # 优先级最低 → 不加入
        self.assertEqual([item.name for item in interest_set.items], ["A", "B"])

    def test_hit_matches_member_words(self):
        interest_set = InterestSet(capacity=5)
        interest_set.ensure_category(InterestCategory("AI", ("ai", "agent"), 0))
        self.assertEqual(interest_set.hit({"agent"}), ["AI"])
        self.assertEqual(interest_set.hit({"hello"}), [])

    def test_extract_keywords_single_count_per_message(self):
        keywords = dict(extract_keywords(["服务器 服务器 服务器", "服务器 讨论", "迁移 方案"], top=5))
        self.assertEqual(keywords.get("服务器"), 2)  # 一条消息内重复只计一次
        self.assertEqual(keywords.get("迁移"), 1)


class InterestsFileTests(unittest.TestCase):
    def test_load_default_file_skips_header(self):
        categories = load_interests()
        names = [category.name for category in categories]
        self.assertIn("AI", names)
        self.assertNotIn("命中判定", names)
        ai = next(category for category in categories if category.name == "AI")
        self.assertIn("agent", ai.words)
        self.assertEqual(ai.priority, 0)  # md 顺序 = 优先级


class PolicyConfigYamlTests(unittest.TestCase):
    def test_policy_section_overrides(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text(
            "policy:\n  attend_window_minutes: 20\n  lucky_engage_probability: 0\n  interest_window_messages: 6\n",
            encoding="utf-8",
        )
        config = load_config(path).policy
        self.assertEqual(config.attend_window_minutes, 20.0)
        self.assertEqual(config.lucky_engage_probability, 0.0)
        self.assertEqual(config.interest_window_messages, 6)
        self.assertEqual(config.engaged_downshift_messages, 12)  # 其余保持默认

    def test_policy_defaults(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text("policy: {}\n", encoding="utf-8")
        config = load_config(path).policy
        self.assertEqual(config.attend_window_minutes, 15.0)
        self.assertAlmostEqual(config.lucky_engage_probability, 0.003)


if __name__ == "__main__":
    unittest.main()
