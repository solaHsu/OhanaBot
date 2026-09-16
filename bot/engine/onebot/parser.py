"""OneBot 11 消息事件解析。

设计依据 docs/engine-spec.md §2：优先使用消息段数组，兼容 CQ 字符串；
图片等转为类型占位；引用内的 at 不视作直接提及；私聊暂不进入本引擎。
"""

from __future__ import annotations

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
    text, mentions, replies_to, images = _parse_message_field(payload.get("message"))
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
    )


def _parse_message_field(message) -> tuple[str, list[str], str | None, list[ImageRef]]:
    """返回 (文本, 被 @ 的号码列表, 引用的消息 ID, 图片引用列表)。"""
    mentions: list[str] = []
    replies_to: str | None = None
    images: list[ImageRef] = []
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
            elif cq_type != "text":
                parts.append(_PLACEHOLDERS.get(cq_type, f"[{cq_type}]"))
        parts.append(_unescape_cq(message[position:]))
    elif message is not None:
        parts.append(str(message))

    return "".join(parts), mentions, replies_to, images


def _unescape_cq(text: str) -> str:
    for source, target in _CQ_UNESCAPE:
        text = text.replace(source, target)
    return text
