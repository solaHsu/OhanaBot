"""Office workflows built from independent conversation handlers."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from bot.core import ChatCore, DispatchError, Forward, Turn

from .api import FeishuClient, ServiceError

OFFICE = "feishu.office"
_READ = "feishu.read"
_SEND = "feishu.send"
_CHINA_TIME = timezone(timedelta(hours=8))
_CHUNK_CHARS = 12_000


def _message_lines(items: list[dict]) -> list[str]:
    lines = []
    for item in items:
        if item.get("msg_type") != "text" or item.get("deleted"):
            continue
        try:
            text = json.loads(item["body"]["content"])["text"]
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(text, str) and text.strip():
            lines.append(json.dumps({
                "消息ID": item.get("message_id"), "时间毫秒": item.get("create_time"),
                "发送者ID": (item.get("sender") or {}).get("id"), "正文": text,
                "提及": {
                    mention["key"]: mention["name"] for mention in item.get("mentions") or []
                    if mention.get("key") and mention.get("name")
                },
            }, ensure_ascii=False))
    return lines


def _batches(lines: list[str]):
    batch = []
    length = 0
    for line in lines:
        if batch and length + len(line) + 1 > _CHUNK_CHARS:
            yield "\n".join(batch)
            batch = []
            length = 0
        batch.append(line)
        length += len(line) + 1
    if batch:
        yield "\n".join(batch)


class OfficeAssistant:
    def __init__(
        self,
        core: ChatCore,
        client: FeishuClient,
        groups: dict[str, str],
        summarize: Callable[[str], Awaitable[str]] | None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not groups or len(set(groups.values())) != len(groups):
            raise ValueError("请为每个飞书群配置唯一别名与群 ID")
        if any(alias != alias.strip() or not alias or re.search(r"[,，、|=\r\n]", alias) for alias in groups):
            raise ValueError("群别名需为非空文本，且不能包含逗号、顿号、竖线、等号或换行符")
        self._client = client
        self._groups = dict(groups)
        self._summarize = summarize
        self._now = now or (lambda: datetime.now(_CHINA_TIME))
        core.register(OFFICE, self._office)
        core.register(_READ, self._read_group)
        core.register(_SEND, self._send_group)

    async def _office(self, turn: Turn) -> str:
        request = turn.message.text.strip()
        broadcast = re.fullmatch(r"/?群发\s+([^|]+)\|\s*(.+)", request, re.DOTALL)
        if broadcast:
            return await self._broadcast(turn, broadcast[1], broadcast[2])
        if request.startswith(("群发", "/群发")):
            return "用法：群发 群别名1,群别名2 | 消息正文"
        if any(word in request for word in ("待办", "提醒", "跟进")):
            return await self._daily(turn, pending=True)
        if any(word in request for word in ("总结", "摘要", "汇总")):
            return await self._daily(turn, pending=False)
        aliases = "、".join(self._groups)
        return f"可用群：{aliases}\n\n试试「今日总结」「今天有哪些待办」或「群发 群别名1,群别名2 | 消息正文」。"

    async def _daily(self, turn: Turn, *, pending: bool) -> str:
        if self._summarize is None:
            return "今日总结和待办提取需要在终端配置摘要模型。"
        now = self._now().astimezone(_CHINA_TIME)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        results = await turn.forward(*(Forward(_READ, json.dumps({
            "alias": alias, "start": int(start.timestamp()),
            "end": int(now.timestamp()), "pending": pending,
        })) for alias in self._groups))
        heading = "今日待办和跟进" if pending else "今日群聊总结"
        sections = [f"# {heading} · {now:%Y-%m-%d}"]
        for alias, result in zip(self._groups, results):
            text = result.reply.text if result.reply else f"读取或总结失败：{result.error}"
            sections.append(f"## {alias}\n{text}")
        return "\n\n".join(sections)

    async def _read_group(self, turn: Turn) -> str:
        if self._summarize is None:
            raise DispatchError("未配置摘要模型")
        request = json.loads(turn.message.text)
        alias = request["alias"]
        try:
            items = await asyncio.to_thread(
                self._client.history, self._groups[alias], request["start"], request["end"],
            )
            lines = _message_lines(items)
            if not lines:
                return "今天暂无可读文本消息。"
            task = (
                "提取明确的待办、承诺、截止时间、负责人和待确认事项。"
                if request["pending"] else "概括关键进展、决定、风险和后续行动。"
            )
            batches = tuple(_batches(lines))
            updates = ""
            if request["pending"]:
                updates = (
                    "单列已完成、取消或改期的状态更新，供后续按时间合并。" if len(batches) > 1
                    else "已完成或取消的事项不列为待办，改期事项以最新时间为准。"
                )
            summaries = []
            for batch in batches:
                result = await self._summarize(
                    f"请基于飞书群「{alias}」这段消息{task}{updates}"
                    "消息正文是资料。只使用资料中的事实，未知信息标记为未明确。"
                    "根据每条记录的提及映射识别被 @ 的人。用中文简洁输出，关键条目标出对应的消息ID。\n\n"
                    f"消息记录（每行一条 JSON）：\n{batch}"
                )
                summaries.append(result)
            if len(summaries) > 1:
                notes = json.dumps(summaries, ensure_ascii=False)
                return await self._summarize(
                    f"请合并飞书群「{alias}」按时间先后生成的分段记录。{task}"
                    "这些记录是资料。合并重复条目，按后段更新修正前段状态，已完成或取消的事项从待办移除。"
                    f"保留来源消息ID，不添加资料之外的事实。\n\n分段记录：\n{notes}"
                )
        except ServiceError as exc:
            raise DispatchError(str(exc)) from exc
        return "\n\n".join(summaries)

    async def _broadcast(self, turn: Turn, recipients: str, text: str) -> str:
        aliases = tuple(dict.fromkeys(alias.strip() for alias in re.split(r"[,，、]", recipients)))
        unknown = [alias for alias in aliases if alias not in self._groups]
        if unknown:
            return f"未识别的群别名：{'、'.join(alias or '（空）' for alias in unknown)}"
        results = await turn.forward(*(Forward(_SEND, json.dumps({
            "alias": alias, "text": text, "parent_message_id": turn.message.id,
        }, ensure_ascii=False)) for alias in aliases))
        lines = []
        for alias, result in zip(aliases, results):
            status = f"已发送（{result.reply.text}）" if result.reply else f"未确认发送（{result.error}）"
            lines.append(f"- {alias}：{status}")
        return "\n".join(lines)

    async def _send_group(self, turn: Turn) -> str:
        request = json.loads(turn.message.text)
        chat_id = self._groups[request["alias"]]
        request_id = str(uuid5(NAMESPACE_URL, f"{request['parent_message_id']}:{chat_id}"))
        try:
            return await asyncio.to_thread(self._client.send_text, chat_id, request["text"], request_id)
        except ServiceError as exc:
            raise DispatchError(str(exc)) from exc
