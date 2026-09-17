"""发言引擎判定核心：旁观 / 关注 / 参与三态状态机（纯函数、零网络）。

设计依据 analysis/proposals/speaking_engine_v0.1.md（标准）§1–§4：
- 硬触发 → 参与（必答）；兴趣类目 10 条内再命中 → 关注；关注期内（默认 15 分钟，可配）命中 → 参与，否则回旁观；
- 幸运升级：每条他人消息按 `lucky_engage_probability`（默认 0.003）概率直接升级参与；
- 参与衰减（12 条消息或 15 分钟无"指向"互动）→ 关注（退出时重建兴趣集）→ 再走关注 → 旁观；
- /exit 指令直接切旁观。

调用方（引擎）负责：消息合并窗口、LLM 调用、限额与发送。本模块只做判定；时间一律由参数传入。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bot.engine.policy.interests import (
    InterestCategory,
    InterestSet,
    extract_keywords,
    tokenize,
)

OBSERVING = "observing"
ATTENTIVE = "attentive"
ENGAGED = "engaged"

_MAX_REMEMBERED_TEXTS = 300


@dataclass(frozen=True)
class PolicyConfig:
    """默认值与标准 §7 基本一致；关注期按实测放宽（15 分钟），全部可通过 yaml `policy:` 段覆盖。"""

    interest_window_messages: int = 10  # 旁观追踪窗口（条）
    attend_window_minutes: float = 15  # 关注期时长（第三次触发的时限）
    engaged_downshift_messages: int = 12  # 参与衰减：消息数
    engaged_downshift_minutes: float = 15  # 参与衰减：时长
    nearby_touch_seconds: float = 90  # 紧邻接话弱信号窗口
    interest_set_capacity: int = 5  # 会话兴趣集容量
    lucky_engage_probability: float = 0.003  # 每条他人消息直接升级参与的概率（0 = 关闭）


@dataclass(frozen=True)
class MessageEvent:
    """一条已解析的消息（engine 侧组装；text 不含引用块内容）。"""

    sender_id: str
    text: str
    from_ohana: bool = False
    mentions_ohana: bool = False
    replies_to_ohana: bool = False


@dataclass(frozen=True)
class Decision:
    """一次判定的结果；action="generate" 表示应进入 LLM 评估（由调用方按 3 秒批处理）。"""

    phase: str
    action: str | None = None
    must_reply: bool = False
    reason: str = ""


class ConversationPolicy:
    """单个会话的状态机；engine 为每个会话维护一个实例。"""

    def __init__(
        self,
        categories: list[InterestCategory],
        self_names: list[str] | None = None,
        config: PolicyConfig | None = None,
        rng: Callable[[], float] | None = None,
    ):
        self.config = config or PolicyConfig()
        self.categories = list(categories)
        self.self_names = [name.strip().lower() for name in (self_names or []) if name.strip()]
        self._category_by_name = {category.name: category for category in self.categories}
        self._rng = rng or random.random  # 回放测试可注入确定性随机数

        self.phase = OBSERVING
        self.interest_set = InterestSet(self.config.interest_set_capacity)
        self.tracking: dict[str, int] = {}  # 旁观追踪：类目名 → 剩余窗口消息数
        self.last_touch: datetime | None = None
        self.messages_since_touch = 0
        self.attend_until: datetime | None = None
        self.last_ohana_speak: datetime | None = None
        self.ohana_followed = False
        self._participated_texts: list[str] = []

    # ---------- 对外入口 ----------

    def on_message(self, event: MessageEvent, now: datetime) -> Decision:
        """处理一条消息，返回判定结果。"""
        self._sync_time(now)  # 时间型降级先落定，再按新状态处理本条消息

        if event.from_ohana:
            self.last_ohana_speak = now
            self.ohana_followed = False
            if self.phase == ENGAGED:
                self._touch(now)
            return Decision(self.phase, None, False, "self")

        nearby = self._consume_nearby(now)
        hard = self._hard_trigger(event)
        if hard:
            self._enter_engaged(now)
            self._remember(event.text)
            return Decision(ENGAGED, "generate", True, f"reply:{hard}")

        if self.phase != ENGAGED and self._lucky_engage():
            self._enter_engaged(now)
            self._remember(event.text)
            return Decision(ENGAGED, "generate", False, "engage:lucky")

        if self.phase == ENGAGED:
            self._remember(event.text)
            if nearby:
                self._touch(now)
                return Decision(ENGAGED, "generate", False, "reply:engaged")
            self.messages_since_touch += 1
            if self.messages_since_touch >= self.config.engaged_downshift_messages:
                self._enter_attentive(now)
                return Decision(ATTENTIVE, None, False, "decay:engaged")
            return Decision(ENGAGED, "generate", False, "reply:engaged")

        tokens = set(tokenize(event.text))
        if self.phase == ATTENTIVE:
            if self.interest_set.hit(tokens):
                self._enter_engaged(now)
                self._remember(event.text)
                return Decision(ENGAGED, "generate", False, "engage:interest")
            return Decision(ATTENTIVE, None, False, "")

        return self._observing_message(tokens, now)

    def tick(self, now: datetime) -> Decision | None:
        """时间推进检查（关注超时 / 参与超时）；无变化返回 None。"""
        return self._sync_time(now)

    def exit_command(self) -> Decision:
        """`/exit`：直接切回旁观，清空兴趣集与追踪。"""
        self.phase = OBSERVING
        self.tracking.clear()
        self.interest_set.clear()
        self.last_touch = None
        self.messages_since_touch = 0
        self.attend_until = None
        self._participated_texts.clear()
        return Decision(OBSERVING, None, False, "exit:command")

    # ---------- 状态迁移 ----------

    def _sync_time(self, now: datetime) -> Decision | None:
        if self.phase == ENGAGED and self.last_touch is not None:
            if now - self.last_touch >= timedelta(minutes=self.config.engaged_downshift_minutes):
                self._enter_attentive(now)
                return Decision(ATTENTIVE, None, False, "decay:engaged")
        if self.phase == ATTENTIVE and self.attend_until is not None and now >= self.attend_until:
            self.phase = OBSERVING
            self.attend_until = None
            return Decision(OBSERVING, None, False, "attend:timeout")
        return None

    def _enter_engaged(self, now: datetime) -> None:
        if self.phase != ENGAGED:
            self._participated_texts.clear()
        self.phase = ENGAGED
        self.tracking.clear()
        self.attend_until = None
        self._touch(now)

    def _enter_attentive(self, now: datetime) -> None:
        if self.phase == ENGAGED:
            self._rebuild_interest_set()
        self.phase = ATTENTIVE
        self.attend_until = now + timedelta(minutes=self.config.attend_window_minutes)
        self.tracking.clear()

    def _touch(self, now: datetime) -> None:
        self.last_touch = now
        self.messages_since_touch = 0

    # ---------- 判定细节 ----------

    def _hard_trigger(self, event: MessageEvent) -> str | None:
        if event.mentions_ohana:
            return "mention"
        if event.replies_to_ohana:
            return "reply"
        lowered = event.text.lower()
        if any(name in lowered for name in self.self_names):
            return "name"
        return None

    def _lucky_engage(self) -> bool:
        """幸运升级：按概率直接进入参与（硬触发之外的一条保底通道）。"""
        probability = self.config.lucky_engage_probability
        return probability > 0 and self._rng() < probability

    def _consume_nearby(self, now: datetime) -> bool:
        """紧邻接话：Ohana 发言后的第一条他人消息、间隔 ≤ 窗口。每条他人消息只判定一次。"""
        if self.last_ohana_speak is None or self.ohana_followed:
            return False
        self.ohana_followed = True
        return now - self.last_ohana_speak <= timedelta(seconds=self.config.nearby_touch_seconds)

    def _observing_message(self, tokens: set[str], now: datetime) -> Decision:
        hits = [c for c in self.categories if any(word in tokens for word in c.words)]
        if any(self.tracking.get(category.name, 0) > 0 for category in hits):
            self._enter_attentive(now)
            return Decision(ATTENTIVE, None, False, "attend:interest")
        self.tracking = {name: left - 1 for name, left in self.tracking.items() if left - 1 > 0}
        for category in hits:
            self.tracking[category.name] = self.config.interest_window_messages
            self.interest_set.ensure_category(category)
        return Decision(OBSERVING, None, False, "")

    def _remember(self, text: str) -> None:
        """参与期内的他人消息（供退出时提取动态关键词）。"""
        self._participated_texts.append(text)
        if len(self._participated_texts) > _MAX_REMEMBERED_TEXTS:
            del self._participated_texts[: len(self._participated_texts) - _MAX_REMEMBERED_TEXTS]

    def _rebuild_interest_set(self) -> None:
        kept = [
            self._category_by_name[item.name]
            for item in self.interest_set.items
            if item.kind == "category" and item.name in self._category_by_name
        ]
        keywords = extract_keywords(self._participated_texts, top=self.config.interest_set_capacity)
        self.interest_set.rebuild(kept, keywords)
