"""Local JSON bridge for the desktop workspace."""

import argparse
import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bot.core import ChatCore, DispatchError, SQLiteStore

from .chat import _ACTOR, _TARGET, model_settings, register_local_chat

_APP_NAMES = {"qq": "QQ", "feishu": "飞书"}
_DEFAULT_APPS = {app: {"groups": [], "accept_private": False} for app in _APP_NAMES}
_UI = Path(__file__).resolve().parents[2] / "desktop" / "ui"
_MAX_REQUEST = 128_000


class Workspace:
    def __init__(self, core: ChatCore, connection: sqlite3.Connection) -> None:
        self.core = core
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS desktop_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS desktop_activity (
                id INTEGER PRIMARY KEY, time TEXT NOT NULL, category TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL
            );
        """)
        settings = model_settings()
        self.model = settings[1] if all(settings[:2]) else None
        register_local_chat(core, settings, echo=self.model is None)

    def get(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM desktop_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO desktop_settings (key, value) VALUES (?, ?)", (key, json.dumps(value)),
        )

    def record(self, category: str, title: str, detail: str, status: str = "success") -> None:
        self.connection.execute(
            "INSERT INTO desktop_activity (time, category, title, detail, status) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), category, title, detail, status),
        )

    def snapshot(self, conversation: str | None, notice: str | None = None, reply_failed: bool = False) -> dict:
        return {
            "apps": {app: self.get(f"app:{app}", default) for app, default in _DEFAULT_APPS.items()},
            "conversation": conversation,
            "messages": [asdict(message) for message in self.core.history(conversation, _ACTOR)] if conversation else [],
            "activities": [dict(row) for row in self.connection.execute("SELECT * FROM desktop_activity ORDER BY id DESC LIMIT 100")],
            "model": self.model,
            "notice": notice,
            "reply_failed": reply_failed,
        }

    def request(self, action: str, payload: dict) -> dict:
        conversation = self.get("conversation")
        notice = None
        reply_failed = False
        if action == "open_chat":
            conversation = self.core.open([_ACTOR]).id
            with self.connection:
                self.put("conversation", conversation)
        elif action == "restore_chat":
            conversation = payload.get("conversation", "")
            if not isinstance(conversation, str) or not conversation.strip():
                raise DispatchError("请输入会话 ID")
            conversation = conversation.strip()
            self.core.history(conversation, _ACTOR)
            with self.connection:
                self.put("conversation", conversation)
        elif action == "save_app":
            self.save_app(payload)
        elif action == "send":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
                raise DispatchError("消息须为 1 至 16000 个字符")
            if payload.get("conversation") != conversation:
                raise DispatchError("当前会话已切换，请重新打开会话再发送")
            if conversation is None:
                conversation = self.core.open([_ACTOR]).id
                with self.connection:
                    self.put("conversation", conversation)
            try:
                asyncio.run(self.core.submit(conversation, _ACTOR, _TARGET, text))
            except DispatchError as exc:
                reply_failed = True
                notice = str(exc)
            try:
                with self.connection:
                    if reply_failed:
                        self.record("chat", "回复失败", notice, "error")
                    elif self.model is None:
                        self.record("chat", "完成本地回显", "离线调试 · 已写入会话")
                    else:
                        self.record("chat", "生成会话回复", f"{self.model} · 已写入会话")
            except sqlite3.Error:
                notice = f"{notice}。活动记录暂时无法写入。" if reply_failed else "回复已保存，活动记录暂时无法写入。"
        elif action != "load":
            raise DispatchError("未知的桌面操作")
        return self.snapshot(conversation, notice, reply_failed)

    def save_app(self, payload: dict) -> None:
        app = payload.get("app")
        groups = payload.get("groups")
        private = payload.get("accept_private")
        if not isinstance(app, str) or app not in _APP_NAMES:
            raise DispatchError("未知应用")
        if not isinstance(private, bool) or not isinstance(groups, list) or len(groups) > 100:
            raise DispatchError("应用设置格式有误")
        if any(not isinstance(group, str) or len(group.strip()) > 120 for group in groups):
            raise DispatchError("群标识应为最多 120 字的文本")
        groups = list(dict.fromkeys(group.strip() for group in groups if group.strip()))
        with self.connection:
            self.put(f"app:{app}", {"groups": groups, "accept_private": private})
            private_label = "允许私聊" if private else "关闭私聊"
            self.record("settings", f"保存 {_APP_NAMES[app]} 设置", f"{len(groups)} 个群 · {private_label} · 本地配置")


def dispatch(database: Path, request: dict) -> dict:
    try:
        if not isinstance(request, dict) or not isinstance(request.get("payload", {}), dict):
            raise DispatchError("请求格式有误")
        with closing(SQLiteStore(database)) as store, closing(sqlite3.connect(database)) as connection:
            workspace = Workspace(ChatCore(store), connection)
            return {"data": workspace.request(request.get("action"), request.get("payload", {}))}
    except DispatchError as exc:
        return {"error": str(exc)}
    except (sqlite3.Error, OSError, ValueError):
        return {"error": "无法读写本地工作区"}


def _preview(database: Path, port: int) -> None:
    class PreviewHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=_UI, **kwargs)

        def do_POST(self):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.path != "/api/desktop" or self.headers.get("Host") != host:
                self.send_error(404)
                return
            if self.headers.get("Origin") != f"http://{host}" or self.headers.get_content_type() != "application/json":
                self.send_error(403)
                return
            try:
                size = int(self.headers["Content-Length"])
                if not 0 < size <= _MAX_REQUEST:
                    raise ValueError
                result = dispatch(database, json.loads(self.rfile.read(size)))
            except (TypeError, ValueError):
                self.send_error(400)
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ohana desktop bridge")
    parser.add_argument("--db", type=Path, default=Path("runtime/local-chat.sqlite3"))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.preview:
        try:
            _preview(args.db, args.port)
        except KeyboardInterrupt:
            pass
        return
    try:
        request = json.loads(sys.stdin.buffer.read(_MAX_REQUEST + 1))
        result = dispatch(args.db, request)
    except ValueError:
        result = {"error": "请求格式有误"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
