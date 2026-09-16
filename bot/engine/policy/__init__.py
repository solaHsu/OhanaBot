"""发言引擎判定核心：三态状态机 + 兴趣集（纯函数、零网络）。

设计依据 analysis/proposals/speaking_engine_v0.1.md（标准）§1–§4。
调用方（引擎）负责消息合并窗口、LLM 调用、限额与发送；本模块只做判定，时间一律由参数传入。
"""

from bot.engine.policy.interests import (
    InterestCategory,
    InterestSet,
    extract_keywords,
    load_interests,
    tokenize,
)
from bot.engine.policy.machine import (
    ATTENTIVE,
    ENGAGED,
    OBSERVING,
    ConversationPolicy,
    Decision,
    MessageEvent,
    PolicyConfig,
)

__all__ = [
    "ATTENTIVE",
    "ENGAGED",
    "OBSERVING",
    "ConversationPolicy",
    "Decision",
    "InterestCategory",
    "InterestSet",
    "MessageEvent",
    "PolicyConfig",
    "extract_keywords",
    "load_interests",
    "tokenize",
]
