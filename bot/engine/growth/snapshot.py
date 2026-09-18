"""Markdown 快照导出（只读投影，标准 §3/§4/§5 的格式）。

- 人物一人一份：runtime/memory/persons/<平台_类型_会话ID>/<用户ID>.md
- 日常记录一会话一份：runtime/memory/logs/<平台_类型_会话ID>.md（最新一段在最上）
- 内容有变化才重写；写入采用临时文件 + 原子替换。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from bot.engine.storage.memory_store import (
    NAME_ALIAS,
    NAME_GROUP_NICKNAME,
    MemoryStore,
)

DEFAULT_MEMORY_ROOT = Path("runtime/memory")

# 基本画像维度 → 快照中文标签（顺序即渲染顺序）
BASIC_LABELS = (
    ("personality", "性格"),
    ("speech", "说话风格"),
    ("interests", "兴趣与擅长"),
    ("preferences", "偏好"),
    ("relations", "群内关系"),
    ("other", "其他"),
)

EMPTY = "（暂无）"


def export_all(
    store: MemoryStore,
    root: Path | str = DEFAULT_MEMORY_ROOT,
    exclude_user_ids: tuple[str, ...] = (),
) -> list[Path]:
    """导出全部快照，返回内容有更新的文件；exclude 内的用户不导出（如 Ohana 自身）。"""
    root = Path(root)
    updated: list[Path] = []
    for conv in store.list_conversations():
        platform = conv["platform"]
        conversation_id = conv["conversation_id"]
        updated += export_persons(store, root, platform, conversation_id, exclude_user_ids)
        updated += export_logs(store, root, platform, conversation_id)
    return updated


def export_persons(
    store: MemoryStore,
    root: Path,
    platform: str,
    conversation_id: str,
    exclude_user_ids: tuple[str, ...] = (),
) -> list[Path]:
    """导出某会话下每个人的一份 Markdown；exclude 内的用户跳过。"""
    updated = []
    conv_dir = root / "persons" / _conv_dir_name(platform, conversation_id)
    excluded = set(exclude_user_ids)
    for user_id in store.list_user_ids(platform, conversation_id):
        if user_id in excluded:
            continue
        person = store.get_person(platform, conversation_id, user_id)
        content = render_person(
            platform,
            conversation_id,
            user_id,
            person["basic"] if person else {},
            store.list_names(platform, conversation_id, user_id),
            store.list_entries(platform, conversation_id, user_id),
        )
        path = conv_dir / f"{_safe_name(user_id)}.md"
        if _write_if_changed(path, content):
            updated.append(path)
    return updated


def export_logs(store: MemoryStore, root: Path, platform: str, conversation_id: str) -> list[Path]:
    """导出某会话的日常记录；还没有任何记录时不生成文件。"""
    logs = store.list_logs(platform, conversation_id)
    if not logs:
        return []
    content = render_logs(platform, conversation_id, logs)
    path = root / "logs" / f"{_conv_dir_name(platform, conversation_id)}.md"
    return [path] if _write_if_changed(path, content) else []


# ---------- 渲染 ----------

def render_person(
    platform: str,
    conversation_id: str,
    user_id: str,
    basic: dict,
    names: list[dict],
    entries: list[dict],
) -> str:
    """单个人物一份 Markdown（格式见标准 §3 样例）。"""
    conversation = f"{platform}:{conversation_id}"
    person_key = f"{platform}:{user_id}"
    nicknames = [n for n in names if n["kind"] == NAME_GROUP_NICKNAME]
    aliases = [n for n in names if n["kind"] == NAME_ALIAS]
    current = nicknames[-1]["text"] if nicknames else ""
    if current:
        heading = f"# 人物：{person_key}（{current}）· 会话 {conversation}"
    else:
        heading = f"# 人物：{person_key} · 会话 {conversation}"

    lines = [heading, "（自动生成，勿手改）", "", "## 基本画像"]
    has_basic = False
    for key, label in BASIC_LABELS:
        value = str(basic.get(key, "")).strip()
        if value:
            lines.append(f"- {label}：{value}")
            has_basic = True
    if not has_basic:
        lines.append(f"- {EMPTY}")

    lines += ["", "## 称呼", f"- 群昵称历史：{render_nickname_chain(nicknames)}"]
    alias_text = "、".join(a["text"] for a in aliases) if aliases else EMPTY
    lines.append(f"- 绰号：{alias_text}")

    lines += ["", "## 条目"]
    if entries:
        for entry in reversed(entries):  # 新的在上
            lines.append(f"- {entry['text'].strip()}")
    else:
        lines.append(f"- {EMPTY}")
    return "\n".join(lines) + "\n"


def render_logs(platform: str, conversation_id: str, logs: list[dict]) -> str:
    """单个会话的日常记录（最新一段在最上）。"""
    lines = ["# 日常记录快照（自动生成，勿手改）", "", f"## 会话 {platform}:{conversation_id}", ""]
    for log in sorted(logs, key=lambda r: r["seq"], reverse=True):
        lines.append(f"### 第 {log['seq']} 段（msg-{log['msg_from']} ~ msg-{log['msg_to']}）")
        lines.append(log["text"].strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_nickname_chain(nicknames: list[dict]) -> str:
    """'崖崖（2026-05 ~ 2026-08）→ 阿崖（2026-08 至今）'；最后一条视为当前昵称。"""
    if not nicknames:
        return EMPTY
    parts = []
    last_index = len(nicknames) - 1
    for index, name in enumerate(nicknames):
        start = _month(name["first_seen"])
        if index == last_index:
            parts.append(f"{name['text']}（{start} 至今）")
        else:  # 结束于下一次变更的时间（取两者较晚）
            end = _month(max(name["last_seen"], nicknames[index + 1]["first_seen"]))
            parts.append(f"{name['text']}（{start} ~ {end}）")
    return "→ ".join(parts)


def _month(timestamp: str) -> str:
    """'2026-08-11T10:00:00Z' → '2026-08'。"""
    return timestamp[:7]


def _conv_dir_name(platform: str, conversation_id: str) -> str:
    """会话目录名（文件系统安全）：qq + group:123456 → qq_group_123456。"""
    return _safe_name(f"{platform}_{conversation_id}")


def _safe_name(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in value)


def _write_if_changed(path: Path, content: str) -> bool:
    """内容有变化才写；写临时文件后原子替换。"""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True
