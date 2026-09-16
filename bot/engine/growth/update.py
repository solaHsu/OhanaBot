"""记忆更新任务：每满一批消息 → 拼上下文 → 调模型 → 解析 → 单事务写库。

设计依据 analysis/proposals/memory_plan_v0.2.md §6：
- 触发：会话待压缩消息 ≥ batch_size（默认 200）；
- 上下文：本批消息 + 涉及人物资料（发言人 + 被 @ 的人）+ 最近几段日常记录；
- 产物：基本画像覆盖、绰号/条目追加、一段新日常记录、游标前移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from bot.engine.growth.snapshot import BASIC_LABELS, EMPTY, render_nickname_chain
from bot.engine.storage.memory_store import NAME_ALIAS, NAME_GROUP_NICKNAME, MemoryStore

PROMPT_PATH = Path(__file__).parent / "prompts" / "memory_update.txt"
PROMPT_TEMPLATE = PROMPT_PATH.read_text(encoding="utf-8")


@dataclass
class UpdateConfig:
    """更新任务参数（后续接入 config 文件，先以参数传递）。"""

    batch_size: int = 200  # 每满多少条消息触发一次
    recent_logs: int = 3  # 上下文携带的最近日常记录段数
    business_tz: timezone = timezone(timedelta(hours=8))  # 业务时区 Asia/Shanghai（无夏令时）


@dataclass
class UpdateResult:
    """一次批更新的结果摘要。"""

    platform: str
    conversation_id: str
    msg_from: int
    msg_to: int
    profiles_updated: int = 0
    log_text: str = ""
    log_seq: int | None = None
    entries_added: int = 0
    aliases_added: int = 0


def run_update(
    store: MemoryStore,
    generate: Callable[[str], str],
    platform: str,
    conversation_id: str,
    config: UpdateConfig | None = None,
    apply: bool = True,
) -> UpdateResult | None:
    """执行一次批更新；消息不足一批时返回 None。

    apply=False 时为 dry-run：调用模型并解析，但不写库、不前进游标。
    """
    config = config or UpdateConfig()
    if store.count_pending_messages(platform, conversation_id) < config.batch_size:
        return None
    batch = store.fetch_message_batch(platform, conversation_id, limit=config.batch_size)
    if not batch:
        return None

    batch_from = batch[0]["id"]
    batch_to = batch[-1]["id"]
    if apply:
        store.refresh_nicknames(platform, conversation_id, up_to_id=batch_to)

    users = _involved_users(batch)
    prompt = _fill_template(
        profiles=_render_profiles(store, platform, conversation_id, users),
        recent_logs=_render_recent_logs(store, platform, conversation_id, config.recent_logs),
        messages=_render_messages(batch, config.business_tz),
    )

    parsed = parse_update_output(generate(prompt))
    result = UpdateResult(
        platform=platform,
        conversation_id=conversation_id,
        msg_from=batch_from,
        msg_to=batch_to,
        profiles_updated=len(parsed["profiles"]),
        log_text=parsed["log"],
    )
    if apply:
        counters = store.apply_update(
            platform,
            conversation_id,
            parsed["profiles"],
            parsed["log"],
            msg_from=batch_from,
            msg_to=batch_to,
            seen_at=batch[-1]["sent_at"],
        )
        result.log_seq = counters["log_seq"]
        result.entries_added = counters["entries_added"]
        result.aliases_added = counters["aliases_added"]
    return result


def parse_update_output(content: str) -> dict:
    """解析模型输出的更新 JSON（含代码围栏容错），返回 {"log", "profiles"}。"""
    text = _strip_code_fence(content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    log_text = str(data.get("log") or "").strip()
    if not log_text:
        raise ValueError("模型输出缺少 log")
    profiles = []
    for item in data.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
        entries = [str(e).strip() for e in (item.get("entries") or []) if str(e).strip()]
        profiles.append({"user_id": user_id, "basic": basic, "aliases": aliases, "entries": entries})
    return {"log": log_text, "profiles": profiles}


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏（模型偶尔会包一层）。"""
    if not text.startswith("```"):
        return text
    first_newline = text.find("\n")
    if first_newline == -1:
        return text
    text = text[first_newline + 1 :].rstrip()
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _fill_template(profiles: str, recent_logs: str, messages: str) -> str:
    return (
        PROMPT_TEMPLATE.replace("{profiles}", profiles)
        .replace("{recent_logs}", recent_logs)
        .replace("{messages}", messages)
    )


def _involved_users(batch: list[dict]) -> dict[str, str]:
    """涉及人物 = 本批消息的发言者 + 被 @ 的人；值取该人最新的昵称。"""
    users: dict[str, str] = {}
    for message in batch:
        sender = message["user_id"]
        name = message["display_name"] or users.get(sender, "")
        users[sender] = name
        for mentioned in message.get("mentions") or []:
            users.setdefault(mentioned, "")
    return users


def _render_profiles(
    store: MemoryStore, platform: str, conversation_id: str, users: dict[str, str]
) -> str:
    """渲染相关人物现有资料（供模型参考去重与覆盖）。"""
    if not users:
        return "（本批消息没有识别到具体人物）"
    blocks = []
    for user_id, display_name in users.items():
        person = store.get_person(platform, conversation_id, user_id)
        names = store.list_names(platform, conversation_id, user_id)
        entries = store.list_entries(platform, conversation_id, user_id)
        basic = (person or {}).get("basic") or {}
        nicknames = [n for n in names if n["kind"] == NAME_GROUP_NICKNAME]
        aliases = [n for n in names if n["kind"] == NAME_ALIAS]

        lines = [f"## 人物 {platform}:{user_id}（当前昵称：{display_name or '未知'}）", "基本画像："]
        rendered = [
            f"- {label}：{str(basic.get(key, '')).strip()}"
            for key, label in BASIC_LABELS
            if str(basic.get(key, "")).strip()
        ]
        lines += rendered or [EMPTY]
        lines.append(f"群昵称历史：{render_nickname_chain(nicknames)}")
        lines.append("已有绰号：" + ("、".join(a["text"] for a in aliases) if aliases else EMPTY))
        if entries:
            lines.append("已有条目：")
            lines += [f"- {e['text']}" for e in entries]
        else:
            lines.append("已有条目：" + EMPTY)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_recent_logs(store: MemoryStore, platform: str, conversation_id: str, count: int) -> str:
    logs = store.list_logs(platform, conversation_id)[-count:] if count > 0 else []
    if not logs:
        return "（暂无历史日常记录）"
    return "\n\n".join(
        f"### 第 {log['seq']} 段（msg-{log['msg_from']} ~ msg-{log['msg_to']}）\n{log['text']}"
        for log in logs
    )


def _render_messages(batch: list[dict], tz: timezone) -> str:
    lines = []
    for message in batch:
        name = message["display_name"] or message["user_id"]
        lines.append(f"[{_format_time(message['sent_at'], tz)}] {name}({message['user_id']}): {message['text']}")
    return "\n".join(lines)


def _format_time(sent_at: str, tz: timezone) -> str:
    try:
        moment = datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return sent_at
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
