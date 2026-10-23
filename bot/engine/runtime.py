"""判定运行时：消息 → policy → 3 秒批窗口 → LLM 生成 → 发送。

设计依据 speaking_engine_v0.1.md（标准）与 engine-spec §6/§7：
- 旁观 / 关注零调用；参与态每 3 秒汇总一批，批内有新消息且过闸门 → 调用一次 LLM；
- 批中含硬触发 → 必须回复（模型沉默时跳过并记录，不硬发）；
- dry_run 只生成草稿不发送；发送结果未知 → 冻结该会话等待人工核对；
- /exit 管理指令直接切旁观。
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bot.engine.config import REPO_ROOT, EngineConfig
from bot.engine.context import build_reply_prompt, load_persona, parse_reply
from bot.engine.growth import snapshot as growth_snapshot
from bot.engine.growth.update import UpdateConfig, run_update
from bot.engine.knowledge import KnowledgeBase
from bot.engine.media import collect_image_data_urls
from bot.engine.onebot.parser import IncomingMessage
from bot.engine.policy import ConversationPolicy, MessageEvent, load_interests
from bot.engine.storage import db
from bot.engine.storage.db import now_iso
from bot.engine.storage.memory_store import MemoryStore
from bot.tools import harness as harness_tool
from bot.tools import web as web_tools

logger = logging.getLogger(__name__)

PLATFORM = "qq"
MAX_BATCH_MESSAGES = 50
RECENT_LINK_WINDOW_MINUTES = 10  # 近期卡片链接的记忆窗口
MAX_RECENT_LINKS = 5  # 每会话保留的近期链接数
PROMPT_KEEP_LATEST = 10  # 本地保留的提示词轮数（debug 用，超出自动清理最旧的）
RECENT_IMAGE_WINDOW_MINUTES = 10  # 近期图片的记忆窗口（旁观态也算，供随后被呼唤/引用时附带）
MAX_RECENT_IMAGES = 8  # 每会话保留的近期图片数
FROZEN_RESET_HINT = "发送结果未知，已冻结该会话；请人工核对平台侧记录后重启引擎"


def _image_key(ref) -> str:
    """图片去重键：优先 URL，其次文件名。"""
    return getattr(ref, "url", "") or getattr(ref, "file", "")


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
    recent_images: list = field(default_factory=list)  # [(ImageRef, 来源消息ID, 出现时间)] 近期图片（含旁观态）
    task_running: bool = False  # 是否有 Harness 任务在执行

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
        policy_rng: Callable[[], float] | None = None,
        self_name: str = "Ohana",
    ):
        self.config = config
        self.store = store
        self.generate = generate
        self.send_group = send_group
        self.lock = lock or threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._policy_rng = policy_rng or random.random
        self.self_name = self_name
        self.self_id = str(config.onebot.expected_self_id or "")
        self.admins = set(config.admins) or set(config.onebot.allowed_private_users)
        self._convs: dict[str, ConversationState] = {}
        self._categories = load_interests()
        self._soul, self._style = load_persona()
        workspace = Path(config.harness.workspace)
        if not workspace.is_absolute():
            workspace = REPO_ROOT / workspace
        self.harness = harness_tool.create_bridge(
            enabled=config.harness.enabled,
            backend=config.harness.backend,
            command=config.harness.command,
            timeout=config.harness.timeout_seconds,
            api_key=config.llm.resolve_api_key(),
            model=config.harness.model or config.llm.model,
            workspace=str(workspace),
            web_options=config.harness.web,
        )
        if config.harness.enabled:
            if self.harness.available:
                logger.info("Harness 桥接已启用（backend=%s）", config.harness.backend)
            else:
                logger.warning(
                    "Harness 桥接已启用（backend=%s）但当前不可用（cli 需装 dsh；web 需装 playwright）：任务分流将跳过",
                    config.harness.backend,
                )
        self.knowledge = KnowledgeBase(store.conn, config.knowledge, lock=self.lock)
        if config.knowledge.enabled:
            logger.info("知识库已启用（auto_ingest=%s）", config.knowledge.auto_ingest)
        self._prompt_dir = Path(config.database).parent / "prompts"  # 提示词全文落盘目录（排查用）
        self._prompt_seq = itertools.count(1)  # 落盘序号（Windows 时钟粒度粗，时间戳会重复，附序号保证唯一）
        self._memory_root = Path(config.database).parent / "memory"  # 记忆快照目录（与 manage --memory-root 默认一致）
        self._growth_running: set[str] = set()  # 正在压记忆的会话（全局单飞；跑完自动让位）
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="engine-tick", daemon=True)
        self._thread.start()
        logger.info("判定运行时已启动（批窗口 %.1f 秒，mode=%s）", self.config.merge_window_seconds, self.config.mode)
        # 启动补处理：把重启前攒够批次的会话交给记忆更新（被动触发的入口之一）
        self._scan_pending_growth()

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
            self._remember_images(conv, message, now)
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
        self._maybe_grow(message.conversation_id)

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
            policy = ConversationPolicy(
                self._categories, self_names=names, config=self.config.policy, rng=self._policy_rng
            )
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

    def _remember_images(self, conv: ConversationState, message: IncomingMessage, now: datetime) -> None:
        """记住消息里的图片（旁观态也算），供随后被呼唤/引用时一并附带。"""
        if not message.images:
            return
        window = RECENT_IMAGE_WINDOW_MINUTES * 60
        conv.recent_images = [
            (ref, mid, seen)
            for ref, mid, seen in conv.recent_images
            if (now - seen).total_seconds() <= window
        ]
        for ref in message.images:
            key = _image_key(ref)
            conv.recent_images = [(r, m, t) for r, m, t in conv.recent_images if _image_key(r) != key]
            conv.recent_images.append((ref, message.message_id, now))
        conv.recent_images = conv.recent_images[-MAX_RECENT_IMAGES:]

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
            batch_image_refs = [ref for message in batch for ref in message.images]
            batch_image_keys = {_image_key(ref) for ref in batch_image_refs}
            quoted_ids = {message.replies_to for message in batch if message.replies_to}
            image_window = RECENT_IMAGE_WINDOW_MINUTES * 60
            fresh_recent_images = [
                (ref, mid, seen)
                for ref, mid, seen in conv.recent_images
                if (now - seen).total_seconds() <= image_window
            ]
            # 被本批消息引用的图片优先，其次批次内图片，最后按从新到旧补位
            quoted_recent = [item for item in reversed(fresh_recent_images) if item[1] in quoted_ids]
            other_recent = [item for item in reversed(fresh_recent_images) if item[1] not in quoted_ids]
            image_refs: list = []
            seen_image_keys: set[str] = set()
            recent_consumed: set[str] = set()
            for ref, _mid, _seen in quoted_recent:
                key = _image_key(ref)
                if key not in seen_image_keys:
                    seen_image_keys.add(key)
                    image_refs.append(ref)
                    recent_consumed.add(key)
            for ref in batch_image_refs:
                key = _image_key(ref)
                if key not in seen_image_keys:
                    seen_image_keys.add(key)
                    image_refs.append(ref)
            for ref, _mid, _seen in other_recent:
                key = _image_key(ref)
                if key not in seen_image_keys:
                    seen_image_keys.add(key)
                    image_refs.append(ref)
                    recent_consumed.add(key)
            if batch_image_keys & recent_consumed:
                recent_consumed -= batch_image_keys  # 批次内已有的不重复消费
            # 消费后移除：近期图片只在“首次被携带”的那一轮生效（与近期链接同规则；批次内图片同样消费）
            consumed_keys = recent_consumed | batch_image_keys
            conv.recent_images = [
                (ref, mid, seen)
                for ref, mid, seen in fresh_recent_images
                if _image_key(ref) not in consumed_keys
            ]
            link_refs = [link for message in batch for link in message.links]
            window = RECENT_LINK_WINDOW_MINUTES * 60
            fresh_recent = [
                (url, seen) for url, seen in conv.recent_links if (now - seen).total_seconds() <= window
            ]
            for url, _seen in fresh_recent:
                if url not in link_refs:
                    link_refs.append(url)
            # 消费后移除：近期链接只在“首次被携带”的那一轮生效，避免每条消息重复注入
            conv.recent_links = [pair for pair in fresh_recent if pair[0] not in link_refs]
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
            extra = f"（含最近窗口 {len(recent_consumed)} 张）" if recent_consumed else ""
            logger.info("%s：附带 %d 张图片参与生成%s", conversation_id, len(images), extra)
        pages = (
            web_tools.collect_page_texts(link_refs, limit=self.config.max_links_per_call)
            if link_refs
            else []
        )
        with self.lock:
            excerpts = self._knowledge_excerpts(batch)
            if excerpts:
                logger.info(
                    "%s：知识库注入 %d 块：%s",
                    conversation_id,
                    len(excerpts),
                    "、".join(f"{item['title'][:24]}（{item['score']}）" for item in excerpts),
                )
            prompt = build_reply_prompt(
                self._soul,
                self._style,
                self.store,
                PLATFORM,
                conversation_id,
                batch,
                must_reply,
                recent_limit=self.config.recent_messages,
                has_images=bool(images),
                pages=pages,
                knowledge=excerpts,
                exclude_user_ids=(self.self_id,) if self.self_id else (),
            )
        self._dump_prompt(conversation_id, "reply", prompt)
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
        if parsed["action"] == "task":
            self._start_task(conv, prompt, str(parsed.get("task") or "").strip())
            return
        with self.lock:
            self._deliver(conv, parsed, must_reply, now)

    def _start_task(self, conv: ConversationState, prompt: str, task: str) -> None:
        """把重型任务交给 Harness 后台执行，完成后生成汇报。"""
        conversation_id = conv.conversation_id
        if not self.harness.enabled or not self.harness.available:
            logger.info("%s：模型请求深度任务，但 Harness 未启用或不可用，跳过", conversation_id)
            return
        if conv.task_running:
            logger.info("%s：已有任务在执行，跳过新任务", conversation_id)
            return
        conv.task_running = True
        thread = threading.Thread(
            target=self._task_worker,
            args=(conv, prompt, task),
            name=f"harness-{conversation_id}",
            daemon=True,
        )
        thread.start()
        logger.info("%s：任务已交给 Harness 执行（%d 字）：%s", conversation_id, len(task), task[:60])

    def _dump_prompt(self, conversation_id: str, kind: str, prompt: str) -> None:
        """把发送给模型的提示词全文落盘（runtime/prompts/，滚动保留最新若干轮），便于排查上下文；失败不影响生成。"""
        try:
            self._prompt_dir.mkdir(parents=True, exist_ok=True)
            safe_id = "".join(ch if ch.isalnum() else "_" for ch in conversation_id)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self._prompt_dir / f"{stamp}-{next(self._prompt_seq):06d}-{safe_id}-{kind}.md"
            path.write_text(prompt, encoding="utf-8")
            for stale in sorted(self._prompt_dir.glob("*.md"))[:-PROMPT_KEEP_LATEST]:
                stale.unlink(missing_ok=True)
            logger.info("%s：提示词全文已落盘：%s", conversation_id, path.name)
        except OSError as exc:
            logger.warning("%s：提示词落盘失败（忽略）：%s", conversation_id, exc)

    def _maybe_grow(self, conversation_id: str) -> None:
        """被动触发记忆更新：待压缩消息满一批时启动后台压缩（全局单飞，抢不到等下批消息）。"""
        if not self.config.growth.enabled:
            return
        with self.lock:
            if self._growth_running:
                return
            pending = self.store.count_pending_messages(PLATFORM, conversation_id)
            if pending < self.config.growth.batch_size:
                return
            self._growth_running.add(conversation_id)
        logger.info("%s：待压缩消息 %d 条，启动记忆更新", conversation_id, pending)
        threading.Thread(
            target=self._growth_worker,
            args=(conversation_id,),
            name=f"growth-{conversation_id}",
            daemon=True,
        ).start()

    def _growth_worker(self, conversation_id: str) -> None:
        """后台压缩到不足一批为止，再导出快照；失败不推游标，等下批消息后重试。"""
        healthy = False
        try:
            conn = db.connect(self.config.database)  # 独立连接：LLM 调用期间不占共享锁
            try:
                store = MemoryStore(conn)
                config = UpdateConfig(
                    batch_size=self.config.growth.batch_size,
                    recent_logs=self.config.growth.recent_logs,
                    exclude_user_ids=(self.self_id,) if self.self_id else (),
                )
                batches = 0
                failed = False
                while True:
                    try:
                        result = run_update(store, self.generate, PLATFORM, conversation_id, config)
                    except Exception as exc:
                        logger.warning("%s：记忆更新失败（游标未动，下批消息后重试）：%s", conversation_id, exc)
                        failed = True
                        break
                    if result is None:
                        break
                    batches += 1
                    logger.info(
                        "%s：记忆更新第 %d 批完成（msg-%d ~ msg-%d，画像 %d 人、新条目 %d、新绰号 %d、删绰号 %d、记录第 %d 段）",
                        conversation_id,
                        batches,
                        result.msg_from,
                        result.msg_to,
                        result.profiles_updated,
                        result.entries_added,
                        result.aliases_added,
                        result.aliases_removed,
                        result.log_seq,
                    )
                if batches:
                    updated = growth_snapshot.export_all(
                        store,
                        self._memory_root,
                        exclude_user_ids=(self.self_id,) if self.self_id else (),
                    )
                    logger.info("%s：记忆快照导出（%d 个文件有更新）", conversation_id, len(updated))
                healthy = not failed and batches > 0  # 全部成功收尾才接力，失败等下批消息重试
            finally:
                conn.close()
        except Exception:
            logger.exception("%s：记忆更新线程异常", conversation_id)
        finally:
            with self.lock:
                self._growth_running.discard(conversation_id)
            if healthy:
                self._scan_pending_growth()  # 接力：单飞位空出后接着安排其他攒满的会话

    def _scan_pending_growth(self) -> None:
        """把攒满一批的其他会话交给记忆更新（全局单飞：先到先得，其余等下一轮接力）。"""
        for conv in self.store.list_conversations():
            if conv["platform"] == PLATFORM:
                self._maybe_grow(conv["conversation_id"])

    def _knowledge_excerpts(self, batch) -> list:
        """按本批文本检索知识库（失败静默，不影响生成）。"""
        if not self.knowledge.enabled:
            return []
        query = " ".join(message.text for message in batch)[-500:]
        try:
            return self.knowledge.search(query)
        except Exception as exc:
            logger.warning("知识库检索失败（跳过注入）：%s", exc)
            return []

    def _web_classify_generator(self):
        """入库审核走网页版（零 API token）；未启用或不可用时返回 None（回退 API 生成器）。"""
        if self.knowledge.classify_backend != "web":
            return None
        from bot.tools import web_harness

        if not web_harness._playwright_available():
            logger.warning("网页版入库审核不可用（未安装 playwright），回退 API")
            return None
        options = replace(
            self.config.harness.web,
            deep_think=False,
            search=False,
            new_chat=True,
            save_documents=False,
        )
        driver = web_harness.PlaywrightWebChat(options)
        timeout = self.config.harness.timeout_seconds

        def generate(prompt: str) -> str:
            return driver.ask(prompt, timeout=timeout)

        return generate

    def _ingest_knowledge(self, conversation_id: str, task: str, content: str) -> None:
        """调研结果自动入库（分类后仅知识类）；失败不影响任务汇报。

        审核默认走网页版（knowledge.classify_backend）；网页版失败时回退 API 重试一次，
        避免因登录态等问题丢掉知识。
        """
        if not (self.knowledge.enabled and self.knowledge.auto_ingest):
            return
        title = task.splitlines()[0][:60] if task else "调研资料"
        classify = self.generate
        web_generate = self._web_classify_generator()
        if web_generate is not None:
            classify = web_generate
        try:
            outcome = self.knowledge.ingest_document(
                title=title, source=task, content=content, classify=classify
            )
            if outcome.get("status") == "failed" and classify is not self.generate:
                logger.warning("%s：网页版入库审核失败，回退 API 重试", conversation_id)
                outcome = self.knowledge.ingest_document(
                    title=title, source=task, content=content, classify=self.generate
                )
        except Exception as exc:
            logger.warning("%s：知识入库异常（忽略）：%s", conversation_id, exc)
            return
        status = outcome.get("status")
        if status == "ingested":
            logger.info(
                "%s：知识入库完成（%s，%d 块）",
                conversation_id,
                outcome.get("title", ""),
                outcome.get("chunks", 0),
            )
        elif status == "skipped":
            logger.info("%s：资料判定为新闻，不入知识库", conversation_id)
        elif status == "duplicate":
            logger.info("%s：知识库已有同内容资料，跳过", conversation_id)
        else:
            logger.warning("%s：知识入库未完成（%s）", conversation_id, status)

    def _task_worker(self, conv: ConversationState, prompt: str, task: str) -> None:
        conversation_id = conv.conversation_id
        try:
            result = self.harness.run_task(task)
        finally:
            conv.task_running = False
        if not result:
            logger.warning("%s：Harness 任务失败或无输出，本轮结束", conversation_id)
            return
        full_result = result
        result = result[:4000]
        logger.info("%s：Harness 任务完成（%d 字），生成汇报", conversation_id, len(result))
        try:
            follow_up = (
                prompt
                + "\n\n【深度执行结果】\n"
                + result
                + "\n\n请基于以上结果，用你的风格给群里回一条消息（仍然只输出 json，action 只能 reply 或 silence）。"
            )
            self._dump_prompt(conversation_id, "report", follow_up)
            with self.lock:
                conv.call_times.append(self._clock())
            try:
                content = self.generate(follow_up)
            except Exception as exc:
                logger.warning("%s：任务汇报生成失败：%s", conversation_id, exc)
                return
            try:
                final = parse_reply(content)
            except ValueError as exc:
                logger.warning("%s：任务汇报解析失败：%s", conversation_id, exc)
                return
            if final["action"] != "reply":
                logger.info("%s：任务完成但模型选择不发言", conversation_id)
                return
            with self.lock:
                self._deliver(conv, final, False, self._clock())
        finally:
            # 汇报先发；入库在后（网页版审核较慢，不影响群回复）
            self._ingest_knowledge(conversation_id, task, full_result)

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
