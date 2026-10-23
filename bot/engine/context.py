"""回复生成的上下文组装与输出解析（标准 §3.3 / engine-spec §5）。

- 人格与风格：启动时读取 bot/memory/soul.md 与 style.md（冻结基线）；
- 记忆注入：本会话最近的日常记录 + 相关人物（本批发言人 + 被 @ 者 + 最近对话的发言人）的画像资料；
- 输出约定：{"action": "reply"|"silence", "text": "..."}（json 模式）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.engine.growth.snapshot import BASIC_LABELS
from bot.engine.storage.memory_store import NAME_GROUP_NICKNAME, MemoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = Path(__file__).parent / "prompts" / "chat_reply.txt"
PROMPT_TEMPLATE = PROMPT_PATH.read_text(encoding="utf-8")

_BUSINESS_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai（无夏令时）

MUST_REPLY_NOTE = "（注意：上面这批消息在直接找 Ohana，必须给出回复，不能返回 silence。）"
IMAGE_NOTE = "（本批消息里 [图片] 的实际画面已随提示一并提供，可直接看图内容。）"
ENTRIES_PER_PERSON = 5
RECENT_LOGS = 2
MAX_PROFILES = 30  # 相关人物资料注入的人数上限（本批优先，其次最近发言人按新→旧；超出的老发言人靠后丢弃）


def load_persona() -> tuple[str, str]:
    """读取灵魂与风格基线（内容变更需重启生效）。"""
    soul = (REPO_ROOT / "bot" / "memory" / "soul.md").read_text(encoding="utf-8").strip()
    style = (REPO_ROOT / "bot" / "memory" / "style.md").read_text(encoding="utf-8").strip()
    return soul, style


def build_reply_prompt(
    soul: str,
    style: str,
    store: MemoryStore,
    platform: str,
    conversation_id: str,
    batch: list,
    must_reply: bool,
    recent_limit: int = 200,
    has_images: bool = False,
    pages: list | None = None,
    knowledge: list | None = None,
    exclude_user_ids: tuple[str, ...] = (),
) -> str:
    """把 人格 + 记忆 + 最近对话 + 本批消息 + （可选）知识库摘录 拼成一次生成的提示词。"""
    batch_ids = {message.message_id for message in batch}
    recent_rows = store.list_recent_messages(platform, conversation_id, limit=recent_limit + len(batch))
    recent = [row for row in recent_rows if row["message_id"] not in batch_ids][-recent_limit:]

    excluded = set(exclude_user_ids)
    user_ids = _participant_ids(batch, recent, excluded)[:MAX_PROFILES]
    return (
        PROMPT_TEMPLATE.replace("{soul}", soul)
        .replace("{style}", style)
        .replace("{logs}", _render_logs(store, platform, conversation_id))
        .replace("{profiles}", _render_profiles(store, platform, conversation_id, user_ids))
        .replace("{recent}", "\n".join(_format_row(row) for row in recent) or "（暂无）")
        .replace("{batch}", "\n".join(_format_batch(message) for message in batch))
        .replace("{image_note}", IMAGE_NOTE if has_images else "")
        .replace("{must_note}", MUST_REPLY_NOTE if must_reply else "")
        .replace("{pages}", _render_pages(pages))
        .replace("{knowledge}", _render_knowledge(knowledge))
    )


def _participant_ids(batch: list, recent: list[dict], excluded: set[str]) -> list[str]:
    """相关人物候选：本批发言人 + 本批被 @ 的人 + 最近对话的发言人。

    去重后按优先级排序：本批相关者在前（最贴近当前回复），其余最近发言人按新→旧排；
    exclude 内的用户（如 Ohana 自身）不参与。
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(user_id: str) -> None:
        if user_id and user_id not in excluded and user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)

    for message in batch:
        add(message.user_id)
        for mentioned in getattr(message, "mentions", ()) or ():
            add(mentioned)
    for row in reversed(recent):  # recent 为旧→新，倒序让最近发言者优先
        add(row["user_id"])
    return ordered


def parse_reply(content: str) -> dict:
    """解析模型输出（容忍代码围栏），返回 {"action", "text"}；action=task 时附 task。"""
    text = _strip_code_fence(content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"输出不是合法 json：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("输出不是 json 对象")
    action = str(data.get("action") or "").strip().lower()
    if action not in ("reply", "silence", "task"):
        raise ValueError(f"未知 action：{action!r}")
    reply_text = str(data.get("text") or "").strip()
    if action == "reply" and not reply_text:
        raise ValueError("action=reply 但 text 为空")
    parsed = {"action": action, "text": reply_text}
    if action == "task":
        task = str(data.get("task") or "").strip()
        if not task:
            raise ValueError("action=task 但 task 为空")
        parsed["task"] = task
    return parsed


# ---------- 渲染 ----------

def _format_row(row: dict) -> str:
    return f"[{_format_time(row['sent_at'])}] {row['display_name'] or row['user_id']}({row['user_id']}): {row['text']}"


def _format_batch(message) -> str:
    return f"[{_format_time(message.sent_at)}] {message.display_name or message.user_id}({message.user_id}): {message.text}"


def _format_time(sent_at: str) -> str:
    try:
        moment = datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return sent_at
    return moment.astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d %H:%M")


def _render_pages(pages: list | None) -> str:
    if not pages:
        return ""
    blocks = [f"### {url}\n{text}" for url, text in pages]
    return "【对方分享的链接内容】\n" + "\n\n".join(blocks)


def _render_knowledge(excerpts: list | None) -> str:
    """知识库摘录段；无命中返回空串（不占用 token）。"""
    if not excerpts:
        return ""
    blocks = [f"### {item['title']}（{item['date']}）\n{item['text']}" for item in excerpts]
    return (
        "【知识库摘录】\n"
        "（以下内容与当前话题相关时才可作为参考；不相关就完全忽略，也不要提“知识库”这个词）\n\n"
        + "\n\n".join(blocks)
    )


def _render_logs(store: MemoryStore, platform: str, conversation_id: str) -> str:
    logs = store.list_logs(platform, conversation_id)[-RECENT_LOGS:]
    if not logs:
        return "（暂无）"
    return "\n\n".join(f"### 第 {log['seq']} 段\n{log['text']}" for log in logs)


def _render_profiles(store: MemoryStore, platform: str, conversation_id: str, user_ids: list[str]) -> str:
    blocks = []
    for user_id in user_ids:
        person = store.get_person(platform, conversation_id, user_id)
        entries = store.list_entries(platform, conversation_id, user_id)
        names = store.list_names(platform, conversation_id, user_id, NAME_GROUP_NICKNAME)
        if not (person or entries or names):
            continue  # 没有任何资料的人不占位，避免噪音与 token 浪费
        current = names[-1]["text"] if names else ""
        heading = f"## {platform}:{user_id}" + (f"（{current}）" if current else "")
        lines = [heading]
        basic = (person or {}).get("basic") or {}
        rendered = [
            f"- {label}：{str(basic.get(key, '')).strip()}"
            for key, label in BASIC_LABELS
            if str(basic.get(key, "")).strip()
        ]
        lines += rendered or ["（暂无资料）"]
        if entries:
            lines.append("条目：")
            lines += [f"- {entry['text']}" for entry in entries[-ENTRIES_PER_PERSON:]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "（暂无）"


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    first_newline = text.find("\n")
    if first_newline == -1:
        return text
    text = text[first_newline + 1 :].rstrip()
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
