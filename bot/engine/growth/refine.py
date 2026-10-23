"""人物画像离线重写：把已有画像融合成一份长期稳定的 basic，并把事件挪进条目。

设计依据 analysis/proposals/memory_plan_v0.2.md §3/§6（基本画像＝融合重写的长期描述）。
场景：提示词或规则调整后，对存量画像做一次对齐（不依赖新消息；与日常更新解耦）。
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Callable

from bot.engine.growth.update import _render_profiles, clean_basic
from bot.engine.storage.memory_store import NAME_GROUP_NICKNAME, MemoryStore

PROMPT_PATH = Path(__file__).parent / "prompts" / "profile_refine.txt"
PROMPT_TEMPLATE = PROMPT_PATH.read_text(encoding="utf-8")

# 近重复判定：去空白与标点后比较（含包含关系与相似度）
_ENTRY_NOISE_RE = re.compile(r"[\s，。、；：,.\-—!！?？\"'“”‘’()（）]")
_SIMILARITY = 0.7


def _normalize_entry(text: str) -> str:
    return _ENTRY_NOISE_RE.sub("", text)


def _is_near_duplicate(candidate: str, existing: list[str]) -> bool:
    """候选条目是否与已有条目近重复（相同 / 包含 / 高度相似）。"""
    cand = _normalize_entry(candidate)
    if not cand:
        return True
    for one in existing:
        if not one:
            continue
        if cand == one or cand in one or one in cand:
            return True
        if difflib.SequenceMatcher(None, cand, one).ratio() >= _SIMILARITY:
            return True
    return False


def build_refine_prompt(profile_text: str) -> str:
    """把某人的现有资料填进重写提示词。"""
    return PROMPT_TEMPLATE.replace("{profile}", profile_text)


def parse_refine_output(content: str) -> dict:
    """解析重写输出（容忍代码围栏），返回 {"basic", "entries"}。"""
    text = _strip_code_fence(content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    basic = clean_basic(data.get("basic"))
    if not basic:
        raise ValueError("模型输出缺少 basic")
    entries = [str(e).strip() for e in (data.get("entries") or []) if str(e).strip()]
    return {"basic": basic, "entries": entries}


def refine_profiles(
    store: MemoryStore,
    generate: Callable[[str], str],
    platform: str,
    conversation_id: str,
    exclude_user_ids: tuple[str, ...] = (),
    apply: bool = True,
    on_step: Callable[[str], None] | None = None,
) -> dict:
    """逐个重写该会话下已有资料的人；返回 {refined, skipped, failed, entries_added}。

    只处理有资料（非空 basic 或已有条目）的人；没资料的人跳过。
    apply=False 时只调用模型预览，不写库。
    """
    excluded = set(exclude_user_ids)
    report = {"refined": 0, "skipped": 0, "failed": 0, "entries_added": 0}
    for user_id in store.list_user_ids(platform, conversation_id):
        if user_id in excluded:
            continue
        basic = (store.get_person(platform, conversation_id, user_id) or {}).get("basic") or {}
        entries = store.list_entries(platform, conversation_id, user_id)
        if not any(str(value).strip() for value in basic.values()) and not entries:
            report["skipped"] += 1
            continue
        names = store.list_names(platform, conversation_id, user_id, NAME_GROUP_NICKNAME)
        display = names[-1]["text"] if names else ""
        profile_text = _render_profiles(store, platform, conversation_id, {user_id: display})
        try:
            parsed = parse_refine_output(generate(build_refine_prompt(profile_text)))
        except Exception as exc:  # 单个失败不影响其余人
            report["failed"] += 1
            if on_step:
                on_step(f"  {user_id}：失败（{exc}）")
            continue
        if not apply:
            report["refined"] += 1
            if on_step:
                filled = sum(1 for value in parsed["basic"].values() if str(value).strip())
                on_step(f"  {user_id}：预览（basic {filled} 维、待加条目 {len(parsed['entries'])}；未写入）")
            continue
        store.upsert_person_basic(platform, conversation_id, user_id, parsed["basic"])
        known = [_normalize_entry(entry["text"]) for entry in entries]
        added = 0
        for text in parsed["entries"]:
            if _is_near_duplicate(text, known):  # 不把已记录过的事件再塞一遍
                continue
            if store.add_entry(platform, conversation_id, user_id, text):
                known.append(_normalize_entry(text))
                added += 1
        report["refined"] += 1
        report["entries_added"] += added
        if on_step:
            on_step(f"  {user_id}：已重写（basic 覆盖、新增条目 {added}）")
    return report


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
