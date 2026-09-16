"""判定运行时：消息 → policy → 3 秒批窗口 → LLM 生成 → 发送。

设计依据 speaking_engine_v0.1.md（标准）与 engine-spec §6/§7：
- 旁观 / 关注零调用；参与态每 3 秒汇总一批，批内有新消息且过闸门 → 调用一次 LLM；
- 批中含硬触发 → 必须回复（模型沉默时跳过并记录，不硬发）；
- dry_run 只生成草稿不发送；发送结果未知 → 冻结该会话等待人工核对；
- /exit 管理指令直接切旁观。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from bot.engine.config import EngineConfig
from bot.engine.context import build_reply_prompt, load_persona, parse_reply
from bot.engine.media import collect_image_data_urls
from bot.engine.onebot.parser import IncomingMessage
from bot.engine.policy import ConversationPolicy, MessageEvent, PolicyConfig, load_interests
from bot.engine.storage.db import now_iso
from bot.engine.storage.memory_store import MemoryStore
from bot.tools import web as web_tools

logger = logging.getLogger(__name__)

PLATFORM = "qq"
MAX_BATCH_MESSAGES = 50
RECENT_LINK_WINDOW_MINUTES = 10  # 近期卡片链接的记忆窗口
MAX_RECENT_LINKS = 5  # 每会话保留的近期链接数
FROZEN_RESET_HINT = "发送结果未知，已冻结该会话；请人工核对平台侧记录后重启引擎"


@dataclass
class ConversationState:
    conversation_id: str
    policy: ConversationPolicy
    pending: list = field(default_factory=list)
    must_reply: bool = False
    batch_started: datetime | None = None
    call_times: list = field(default_factory=list)  # 滚动一小时的调用时间
    frozen: bool = False
    recent_links: list = field(default_factory=list)  # [(url, 出现时间)] 近期卡片链接

    def clear_pending(self) -> None:
        self.pending = []
        self.must_reply = False
        self.batch_started = None


class EngineRuntime:
    """把入库后的消息接到判定与生成上；线程安全（共享锁与 ingest 串行化）。"""

    def __init__(
        self,
        config: EngineConfig,
        store: MemoryStore,
        generate: Callable[[str], str],
        send_group: Callable[[str, str], dict],
        lock: threading.RLock | None = None,
        clock: Callable[[], datetime] | None = None,
        self_name: str = "Ohana",
    ):
        self.config = config
        self.store = store
        self.generate = generate
        self.send_group = send_group
        self.lock = lock or threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.self_name = self_name
        self.self_id = str(config.onebot.expected_self_id or "")
        self.admins = set(config.admins) or set(config.onebot.allowed_private_users)
        self._convs: dict[str, ConversationState] = {}
        self._categories = load_interests()
        self._soul, self._style = load_persona()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="engine-tick", daemon=True)
        self._thread.start()
        logger.info("判定运行时已启动（批窗口 %.1f 秒，mode=%s）", self.config.merge_window_seconds, self.config.mode)

    def _loop(self) -> None:
        while True:
            time.sleep(0.5)
            try:
                self.tick()
            except Exception:
                logger.exception("tick 失败")

    # ---------- 入口 ----------

    def handle(self, message: IncomingMessage) -> None:
        """入库之后被调用（ingest 的 on_message 挂点）。"""
        now = self._clock()
        with self.lock:
            conv = self._ensure_conv(message.conversation_id)
            if message.text.strip() == "/exit" and message.user_id in self.admins:
                conv.policy.exit_command()
                conv.clear_pending()
                logger.info("%s：管理员 %s 执行 /exit → 旁观", message.conversation_id, message.user_id)
                return
            self._remember_links(conv, message, now)
            decision = conv.policy.on_message(
                MessageEvent(
                    sender_id=message.user_id,
                    text=message.text,
                    from_ohana=bool(self.self_id) and message.user_id == self.self_id,
                    mentions_ohana=message.mentions_self,
                    replies_to_ohana=self._replies_to_ohana(message),
                ),
                now,
            )
            if decision.reason:
                logger.info(
                    "%s 判定：%s（状态=%s，must_reply=%s）",
                    message.conversation_id,
                    decision.reason,
                    decision.phase,
                    decision.must_reply,
                )
            if decision.action == "generate":
                if len(conv.pending) < MAX_BATCH_MESSAGES:
                    conv.pending.append(message)
                conv.must_reply = conv.must_reply or decision.must_reply
                if conv.batch_started is None:
                    conv.batch_started = now

    def tick(self, now: datetime | None = None) -> None:
        """周期检查：状态超时 + 到期的批窗口。"""
        now = now or self._clock()
        due: list[tuple[ConversationState, list, bool]] = []
        with self.lock:
            for conv in list(self._convs.values()):
                transition = conv.policy.tick(now)
                if transition is not None and transition.reason:
                    logger.info("%s 状态更新：%s", conv.conversation_id, transition.reason)
                if conv.pending and conv.batch_started is not None:
                    age = (now - conv.batch_started).total_seconds()
                    if age >= self.config.merge_window_seconds:
                        due.append((conv, conv.pending, conv.must_reply))
                        conv.clear_pending()
        for conv, batch, must_reply in due:
            try:
                self._flush(conv, batch, must_reply, now)
            except Exception:
                logger.exception("%s：批处理失败", conv.conversation_id)

    # ---------- 内部 ----------

    def _ensure_conv(self, conversation_id: str) -> ConversationState:
        conv = self._convs.get(conversation_id)
        if conv is None:
            names = list(self.config.onebot.self_aliases)
            if self.self_id:
                names += [
                    row["text"]
                    for row in self.store.list_names(PLATFORM, conversation_id, self.self_id, "group_nickname")
                ]
            policy = ConversationPolicy(self._categories, self_names=names, config=PolicyConfig())
            conv = ConversationState(conversation_id=conversation_id, policy=policy)
            self._convs[conversation_id] = conv
            logger.info("%s：新建会话判定（称呼候选：%s）", conversation_id, "、".join(names) or "无")
        return conv

    def state(self, conversation_id: str) -> ConversationState | None:
        return self._convs.get(conversation_id)

    def _remember_links(self, conv: ConversationState, message: IncomingMessage, now: datetime) -> None:
        """记住消息里的卡片链接（旁观态也算），供随后被呼唤时抓取。"""
        if not message.links:
            return
        window = RECENT_LINK_WINDOW_MINUTES * 60
        conv.recent_links = [
            (url, seen) for url, seen in conv.recent_links if (now - seen).total_seconds() <= window
        ]
        for url in message.links:
            conv.recent_links = [(u, t) for u, t in conv.recent_links if u != url]
            conv.recent_links.append((url, now))
        conv.recent_links = conv.recent_links[-MAX_RECENT_LINKS:]

    def _replies_to_ohana(self, message: IncomingMessage) -> bool:
        if not message.replies_to or not self.self_id:
            return False
        row = self.store.conn.execute(
            "SELECT user_id FROM messages WHERE platform = ? AND conversation_id = ? AND message_id = ?",
            (PLATFORM, message.conversation_id, message.replies_to),
        ).fetchone()
        return bool(row and row["user_id"] == self.self_id)

    def _flush(self, conv: ConversationState, batch: list, must_reply: bool, now: datetime) -> None:
        conversation_id = conv.conversation_id
        with self.lock:
            if conv.frozen:
                logger.warning("%s：发送冻结中，丢弃本批（%d 条）", conversation_id, len(batch))
                return
            conv.call_times = [t for t in conv.call_times if (now - t).total_seconds() < 3600]
            if len(conv.call_times) >= self.config.replies_per_hour:
                logger.info("%s：本小时调用已达上限（%d），跳过本批", conversation_id, len(conv.call_times))
                return
            conv.call_times.append(now)
            image_refs = [ref for message in batch for ref in message.images]
            link_refs = [link for message in batch for link in message.links]
            window = RECENT_LINK_WINDOW_MINUTES * 60
            conv.recent_links = [
                (url, seen) for url, seen in conv.recent_links if (now - seen).total_seconds() <= window
            ]
            for url, _seen in conv.recent_links:
                if url not in link_refs:
                    link_refs.append(url)
        images = (
            collect_image_data_urls(
                image_refs,
                limit=self.config.max_images_per_call,
                max_dimension=self.config.image_max_dimension,
            )
            if image_refs
            else []
        )
        if images:
            logger.info("%s：附带 %d 张图片参与生成", conversation_id, len(images))
        pages = (
            web_tools.collect_page_texts(link_refs, limit=self.config.max_links_per_call)
            if link_refs
            else []
        )
        with self.lock:
            prompt = build_reply_prompt(
                self._soul,
                self._style,
                self.store,
                PLATFORM,
                conversation_id,
                batch,
                must_reply,
                has_images=bool(images),
                pages=pages,
            )
        try:
            content = self.generate(prompt, images=images or None)
        except Exception as exc:
            logger.warning("%s：生成失败：%s", conversation_id, exc)
            return
        try:
            parsed = parse_reply(content)
        except ValueError as exc:
            logger.warning("%s：输出解析失败：%s", conversation_id, exc)
            return
        with self.lock:
            self._deliver(conv, parsed, must_reply, now)

    def _deliver(self, conv: ConversationState, parsed: dict, must_reply: bool, now: datetime) -> None:
        conversation_id = conv.conversation_id
        if parsed["action"] == "silence":
            if must_reply:
                logger.warning("%s：硬触发要求回复但模型选择沉默，跳过", conversation_id)
            else:
                logger.info("%s：模型选择沉默", conversation_id)
            return
        text = parsed["text"].strip()
        if not text:
            return
        if len(text) > self.config.max_reply_characters:
            logger.warning("%s：回复超长（%d → %d）已截断", conversation_id, len(text), self.config.max_reply_characters)
            text = text[: self.config.max_reply_characters]

        if not (self.config.mode == "live" and self.config.send_enabled):
            logger.info("%s：[dry-run] 拟回复：%s", conversation_id, text)
            return

        try:
            result = self.send_group(conversation_id, text)
        except Exception as exc:
            conv.frozen = True
            logger.error("%s：%s（%s）", conversation_id, FROZEN_RESET_HINT, exc)
            return
        if not (result.get("status") == "ok" and result.get("retcode") == 0):
            logger.error("%s：发送被平台拒绝：%s", conversation_id, result)
            return

        message_id = str((result.get("data") or {}).get("message_id") or "")
        if not message_id:
            message_id = f"self-{int(now.timestamp() * 1000)}"
        self.store.add_message(
            PLATFORM, conversation_id, message_id, self.self_id or "self", self.self_name, text, now_iso()
        )
        conv.policy.on_message(
            MessageEvent(sender_id=self.self_id or "self", text=text, from_ohana=True), now
        )
        logger.info("%s：已发送：%s", conversation_id, text)
