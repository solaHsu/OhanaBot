"""消息入库管道：OneBot 事件 → 解析 → 白名单 → 去重 → 落库（判定运行时接入的挂点）。

设计依据 docs/engine-spec.md §1/§2：
- 非白名单群在存储正文前拒绝；
- 重复回调（同会话同消息 ID）不重复入库；
- 群昵称由程序从消息自动记录（记忆标准 §3）。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from bot.engine.config import EngineConfig
from bot.engine.onebot.parser import IncomingMessage, parse_message_event
from bot.engine.storage.memory_store import MemoryStore

logger = logging.getLogger(__name__)

PLATFORM = "qq"


class MessageIngestor:
    """把一个会话的事件流安全地写进权威库；串行加锁避免并发写冲突。"""

    def __init__(
        self,
        config: EngineConfig,
        store: MemoryStore,
        on_message: Callable[[IncomingMessage], None] | None = None,
    ):
        self.config = config
        self.store = store
        self.on_message = on_message
        self._lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        self.duplicated = 0

    def handle(self, payload: dict) -> None:
        """事件服务器回调入口；处理失败抛异常（由服务器返回 503）。"""
        message = parse_message_event(payload, self.config.onebot.expected_self_id)
        if message is None:
            return
        if message.conversation_type != "group":
            return
        group_id = message.conversation_id.split(":", 1)[1]
        if group_id not in self.config.onebot.allowed_groups:
            self.rejected += 1
            logger.info("忽略非白名单群：%s", message.conversation_id)
            return

        with self._lock:
            if self.store.message_exists(PLATFORM, message.conversation_id, message.message_id):
                self.duplicated += 1
                return
            self.store.ensure_conversation(PLATFORM, message.conversation_id)
            self.store.add_message(
                PLATFORM,
                message.conversation_id,
                message.message_id,
                message.user_id,
                message.display_name,
                message.text,
                message.sent_at,
                mentions=list(message.mentions),
            )
            if message.display_name:
                self.store.record_group_nickname(
                    PLATFORM,
                    message.conversation_id,
                    message.user_id,
                    message.display_name,
                    message.sent_at,
                )
            self.accepted += 1
        logger.info(
            "入库 %s %s %s：%s",
            message.conversation_id,
            message.message_id,
            message.display_name,
            message.text[:40],
        )
        if self.on_message is not None:
            self.on_message(message)
