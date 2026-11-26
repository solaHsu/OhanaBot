"""Interactive local chat backed by ChatCore and a configurable model."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import deque
from contextlib import closing

from bot.core import ChatCore, DispatchError, SQLiteStore, Turn

from ._http import ServiceError, request_json

_ACTOR = "local-operator"
_TARGET = "local.chat"
_HELP = "/history 查看当前会话记录 · /help 查看命令 · /exit 退出"


def model_settings() -> tuple[str, str, str]:
    return (
        os.getenv("OHANA_MODEL_URL", "").strip(),
        os.getenv("OHANA_MODEL", "").strip(),
        os.getenv("OHANA_MODEL_API_KEY", ""),
    )


def _complete(url: str, model: str, key: str, messages: list[dict[str, str]]) -> str:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": model, "messages": messages}, ensure_ascii=False).encode("utf-8")
    response = request_json(url, headers=headers, body=body)
    try:
        choice = response["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ServiceError("模型返回了无效的消息结构") from exc
    if choice.get("finish_reason") == "length":
        raise ServiceError("模型回复被截断")
    if not isinstance(text, str) or not text.strip():
        raise ServiceError("模型返回了空回复")
    return text.strip()


def _model_context(turn: Turn, limit: int) -> list[dict[str, str]]:
    completed = deque(maxlen=(limit - 1) // 2)
    pending = {}
    for message in turn.history:
        if message.id == turn.message.id:
            break
        if message.role == "input":
            pending[message.id] = message.text
        elif message.in_reply_to in pending:
            completed.append((pending.pop(message.in_reply_to), message.text))

    messages = []
    for question, answer in completed:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": turn.message.text})
    return messages


def register_local_chat(
    core: ChatCore,
    settings: tuple[str, str, str],
    *,
    echo: bool = False,
    debug: bool = False,
    context_messages: int = 40,
) -> None:
    model_url, model, key = settings
    async def handle(turn: Turn) -> str:
        messages = _model_context(turn, context_messages)
        if debug:
            print(f"[debug] 输入 {turn.message.id} · 上下文 {len(messages)}/{len(turn.history)} 条", file=sys.stderr)
        if echo:
            return f"回显：{turn.message.text}"
        try:
            return await asyncio.to_thread(_complete, model_url, model, key, messages)
        except ServiceError as exc:
            raise DispatchError(f"模型调用失败：{exc}") from exc
    core.register(_TARGET, handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ohana 本地聊天终端")
    parser.add_argument("--db", default="runtime/local-chat.sqlite3", help="会话库路径，可用 :memory:")
    parser.add_argument("--conversation", metavar="ID", help="恢复指定会话")
    parser.add_argument("--once", metavar="消息", help="发送一条消息并退出")
    parser.add_argument("--echo", action="store_true", help="离线回显，用于调试收发和存储")
    parser.add_argument("--debug", action="store_true", help="在标准错误显示上下文条数、耗时和消息 ID")
    parser.add_argument("--context-messages", type=int, default=40, metavar="N", help="模型上下文的最近消息条数，默认 40")
    args = parser.parse_args(argv)
    if args.context_messages < 1:
        parser.error("--context-messages 必须大于 0")

    settings = model_settings()
    if not args.echo and not all(settings[:2]):
        parser.error("请配置 OHANA_MODEL_URL 和 OHANA_MODEL，或用 --echo 离线调试")

    try:
        with closing(SQLiteStore(args.db)) as store, asyncio.Runner() as runner:
            core = ChatCore(store)
            register_local_chat(core, settings, echo=args.echo, debug=args.debug, context_messages=args.context_messages)
            conversation = args.conversation
            if conversation is None:
                conversation = core.open([_ACTOR]).id
            else:
                core.history(conversation, _ACTOR)
            return _run(core, runner, conversation, args.once, args.debug)
    except (DispatchError, sqlite3.Error, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run(core: ChatCore, runner: asyncio.Runner, conversation: str, once: str | None, debug: bool) -> int:
    def reply(text: str) -> str:
        started = time.monotonic()
        result = runner.run(core.submit(conversation, _ACTOR, _TARGET, text))
        if debug:
            print(f"[debug] 回复 {result.id} · 耗时 {time.monotonic() - started:.2f}s", file=sys.stderr)
        return result.text

    if once is not None:
        print(f"会话 ID：{conversation}", file=sys.stderr)
        print(reply(once))
        return 0

    print(f"Ohana · 会话 ID：{conversation}\n{_HELP}")
    while True:
        try:
            text = input("你> ")
        except EOFError:
            return 0
        command = text.strip()
        if command == "/exit":
            return 0
        if command == "/help":
            print(_HELP)
        elif command == "/history":
            for message in core.history(conversation, _ACTOR):
                speaker = "你" if message.role == "input" else "Ohana"
                print(f"{speaker}> {message.text}")
        elif command:
            try:
                print(f"Ohana> {reply(text)}")
            except DispatchError as exc:
                print(f"错误：{exc}", file=sys.stderr)


if __name__ == "__main__":
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    sys.exit(main())
