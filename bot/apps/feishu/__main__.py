"""Run the office assistant from a local interactive terminal or a single request."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import closing

from bot.core import ChatCore, DispatchError, SQLiteStore

from .api import FeishuClient, ServiceError, request_json
from .assistant import OFFICE, OfficeAssistant


def _completion(url: str, model: str, key: str, prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = request_json(url, headers=headers, body=json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }, ensure_ascii=False).encode("utf-8"))
    try:
        choice = response["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ServiceError("摘要模型返回了无效的回复") from exc
    if choice.get("finish_reason") == "length" or not isinstance(text, str) or not text.strip():
        raise ServiceError("摘要模型回复为空或被截断")
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ohana 飞书办公助手")
    parser.add_argument("--group", action="append", required=True, metavar="别名=oc_群ID")
    parser.add_argument("--db", default="runtime/chat.sqlite3")
    parser.add_argument("--conversation", help="继续已有的本地会话")
    parser.add_argument("--once", help="处理一条请求并退出")
    args = parser.parse_args(argv)

    groups = {}
    for group in args.group:
        alias, _, chat_id = group.partition("=")
        alias, chat_id = alias.strip(), chat_id.strip()
        if not alias or alias in groups or not chat_id.startswith("oc_"):
            parser.error(f"群配置无效或别名重复：{group}")
        groups[alias] = chat_id

    model_url = os.getenv("OHANA_MODEL_URL", "")
    model = os.getenv("OHANA_MODEL", "")
    key = os.getenv("OHANA_MODEL_API_KEY", "")
    if bool(model_url) != bool(model):
        parser.error("请同时配置 OHANA_MODEL_URL 与 OHANA_MODEL")

    async def summarize(prompt: str) -> str:
        return await asyncio.to_thread(_completion, model_url, model, key, prompt)

    try:
        client = FeishuClient(os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", ""))
        with closing(SQLiteStore(args.db)) as store, asyncio.Runner() as runner:
            core = ChatCore(store)
            OfficeAssistant(core, client, groups, summarize if model_url else None)
            return _chat(core, runner, args.conversation, args.once)
    except (ValueError, DispatchError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _chat(core: ChatCore, runner: asyncio.Runner, conversation: str | None, once: str | None) -> int:
    actor = "local-operator"
    if conversation:
        core.history(conversation, actor)
    else:
        conversation = core.open([actor]).id

    def reply(text: str) -> None:
        response = runner.run(core.submit(conversation, actor, OFFICE, text))
        print(response.text)

    if once is not None:
        reply(once)
        return 0
    print(f"飞书办公助手 · 会话 {conversation}。输入「帮助」查看用法，/exit 退出。")
    while True:
        try:
            text = input("你> ").strip()
            if text == "/exit":
                return 0
            if text:
                reply(text)
        except DispatchError as exc:
            print(str(exc), file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            return 0


if __name__ == "__main__":
    sys.exit(main())
