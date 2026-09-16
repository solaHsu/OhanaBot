"""OneBot 11 消息事件解析。

设计依据 docs/engine-spec.md §2：优先使用消息段数组，兼容 CQ 字符串；
图片保留引用、卡片提取文本、其余媒体转为类型占位；引用内的 at 不视作直接提及；私聊暂不进入本引擎。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.engine.storage.db import now_iso

_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
    "music": "[音乐]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]")
_CQ_PARAM_RE = re.compile(r"(?:^|,)([a-zA-Z0-9_]+)=([^,]*)")
_CQ_UNESCAPE = (("&#44;", ","), ("&#91;", "["), ("&#93;", "]"), ("&amp;", "&"))


@dataclass(frozen=True)
class ImageRef:
    """图片段里的获取途径（优先 url 下载，本地 file 兜底）。"""

    url: str = ""
    file: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    """解析后的群消息（内部统一格式）。"""

    conversation_id: str  # 如 group:123456
    conversation_type: str  # group（当前仅支持）
    message_id: str
    user_id: str
    display_name: str  # 群名片优先，其次昵称
    text: str
    mentions: tuple[str, ...]
    mentions_self: bool
    replies_to: str | None
    sent_at: str  # UTC ISO8601
    images: tuple[ImageRef, ...] = ()  # 图片引用（供多模态生成）
    links: tuple[str, ...] = ()  # 卡片跳转链接（供网页抓取）


def parse_message_event(payload: dict, self_id: str | None = None) -> IncomingMessage | None:
    """把一条 OneBot 事件解析为内部消息；非群消息返回 None。"""
    if payload.get("post_type") != "message":
        return None
    if payload.get("message_type") != "group":
        return None  # 私聊暂缓（仅管理员指令通道，后续实现）
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    message_id = payload.get("message_id")
    if group_id is None or user_id is None or message_id is None:
        return None

    effective_self = str(self_id or payload.get("self_id") or "")
    text, mentions, replies_to, images, links = _parse_message_field(payload.get("message"))
    sender = payload.get("sender") or {}
    display_name = str(sender.get("card") or sender.get("nickname") or user_id)

    timestamp = payload.get("time")
    if timestamp is not None:
        sent_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        sent_at = now_iso()

    return IncomingMessage(
        conversation_id=f"group:{group_id}",
        conversation_type="group",
        message_id=str(message_id),
        user_id=str(user_id),
        display_name=display_name,
        text=text.strip(),
        mentions=tuple(mentions),
        mentions_self=bool(effective_self and effective_self in mentions),
        replies_to=replies_to,
        sent_at=sent_at,
        images=tuple(images),
        links=tuple(links),
    )


def _parse_message_field(message) -> tuple[str, list[str], str | None, list[ImageRef], list[str]]:
    """返回 (文本, 被 @ 的号码列表, 引用的消息 ID, 图片引用列表, 卡片链接列表)。"""
    mentions: list[str] = []
    replies_to: str | None = None
    images: list[ImageRef] = []
    links: list[str] = []
    parts: list[str] = []

    if isinstance(message, list):  # 消息段数组（优先）
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            data = segment.get("data") or {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                if qq and qq != "all":
                    mentions.append(qq)
                    parts.append(f"@{qq}")
            elif seg_type == "reply":
                replies_to = str(data.get("id") or "") or None
            elif seg_type == "image":
                images.append(ImageRef(url=str(data.get("url") or ""), file=str(data.get("file") or "")))
                parts.append("[图片]")
            elif seg_type in ("json", "xml"):
                card = _extract_card_text(seg_type, data.get("data"))
                parts.append(f"[卡片：{card}]" if card else "[卡片]")
                card_url = _extract_card_url(seg_type, data.get("data"))
                if card_url:
                    links.append(card_url)
            else:
                parts.append(_PLACEHOLDERS.get(seg_type, f"[{seg_type}]"))
    elif isinstance(message, str):  # CQ 字符串（兼容）
        position = 0
        for match in _CQ_RE.finditer(message):
            parts.append(_unescape_cq(message[position : match.start()]))
            position = match.end()
            cq_type = match.group(1)
            params = dict(_CQ_PARAM_RE.findall(match.group(2) or ""))
            if cq_type == "at":
                qq = str(params.get("qq") or "")
                if qq and qq != "all":
                    mentions.append(qq)
                    parts.append(f"@{qq}")
            elif cq_type == "reply":
                replies_to = str(params.get("id") or "") or None
            elif cq_type == "image":
                images.append(
                    ImageRef(url=str(params.get("url") or ""), file=str(params.get("file") or ""))
                )
                parts.append("[图片]")
            elif cq_type in ("json", "xml"):
                raw_card = _unescape_cq(str(params.get("data") or ""))
                card = _extract_card_text(cq_type, raw_card)
                parts.append(f"[卡片：{card}]" if card else "[卡片]")
                card_url = _extract_card_url(cq_type, raw_card)
                if card_url:
                    links.append(card_url)
            elif cq_type != "text":
                parts.append(_PLACEHOLDERS.get(cq_type, f"[{cq_type}]"))
        parts.append(_unescape_cq(message[position:]))
    elif message is not None:
        parts.append(str(message))

    links = list(dict.fromkeys(links))[:3]
    return "".join(parts), mentions, replies_to, images, links


def _unescape_cq(text: str) -> str:
    for source, target in _CQ_UNESCAPE:
        text = text.replace(source, target)
    return text


# ---------- 卡片（json / xml 段）文本提取 ----------

_CARD_PROMPT_PREFIX_RE = re.compile(r"^\[[^\]]{1,12}\]\s*")
_CARD_XML_TEXT_RE = r"<{tag}[^>]*>(.*?)</{tag}>"


def _extract_card_text(kind: str, raw) -> str:
    """从 json/xml 卡片里提取可读文本；失败返回空串。"""
    text = str(raw or "")
    if not text:
        return ""
    try:
        if kind == "json":
            fields = _card_fields_from_json(text)
        else:
            fields = _card_fields_from_xml(text)
    except Exception:
        return ""
    parts: list[str] = []
    for field in fields:
        value = _clean_card_value(field)
        if value and value not in parts:
            parts.append(value)
    return "；".join(parts)[:200]


def _card_fields_from_json(raw: str) -> list[str]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return []
    fields: list[object] = [data.get("prompt"), data.get("desc")]
    meta = data.get("meta")
    if isinstance(meta, dict):
        for value in meta.values():
            if isinstance(value, dict):
                fields += [value.get("title"), value.get("desc"), value.get("summary")]
    return [field for field in fields if isinstance(field, str)]


def _card_fields_from_xml(raw: str) -> list[str]:
    fields: list[str] = []
    brief = re.search(r"brief=[\"']([^\"']*)[\"']", raw)
    if brief:
        fields.append(brief.group(1))
    for tag in ("title", "summary"):
        match = re.search(_CARD_XML_TEXT_RE.format(tag=tag), raw, re.S)
        if match:
            fields.append(match.group(1))
    source = re.search(r"<source[^>]*name=[\"']([^\"']*)[\"']", raw)
    if source:
        fields.append(source.group(1))
    return fields


def _clean_card_value(value: object) -> str:
    """去 CDATA、解实体、压空白；若以【…】/[…] 开头则去前缀；截断到 100 字。"""
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("<![CDATA[", "").replace("]]>", "")
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _CARD_PROMPT_PREFIX_RE.sub("", cleaned).strip()
    return cleaned[:100]


def _extract_card_url(kind: str, raw) -> str:
    """从卡片里提取跳转链接（http/https）；找不到返回空串。"""
    text = str(raw or "")
    if not text:
        return ""
    try:
        if kind == "json":
            return _card_url_from_json(text)
        return _card_url_from_xml(text)
    except Exception:
        return ""


def _card_url_from_json(raw: str) -> str:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return ""
    return _find_http_url(data)


def _find_http_url(node, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(node, dict):
        for key in ("jumpUrl", "qqdocurl", "url"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in node.values():
            found = _find_http_url(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_http_url(item, depth + 1)
            if found:
                return found
    return ""


def _card_url_from_xml(raw: str) -> str:
    match = re.search(r"<msg[^>]*\burl=[\"']([^\"']+)[\"']", raw)
    if match and match.group(1).startswith(("http://", "https://")):
        return match.group(1)
    return ""
