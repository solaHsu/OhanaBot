"""Feishu HTTP adapter. These credentials represent an enterprise app."""

import json
import threading
import time
from urllib.parse import urlencode

from .._http import ServiceError, request_json


class FeishuClient:
    _TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
    _MESSAGES_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, app_id: str, app_secret: str) -> None:
        if not app_id or not app_secret:
            raise ValueError("请配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at:
                return self._token
            body = urlencode({
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
            }).encode("ascii")
            response = request_json(self._TOKEN_URL, headers={
                "Content-Type": "application/x-www-form-urlencoded",
            }, body=body)
            self._check(response)
            token, expires_in = response.get("access_token"), response.get("expires_in")
            if not token or not isinstance(expires_in, int) or expires_in <= 0:
                raise ServiceError("飞书返回了无效的凭证")
            self._token = token
            self._expires_at = now + expires_in - min(60, expires_in / 2)
            return token

    @staticmethod
    def _check(response: dict) -> None:
        if response.get("code") != 0:
            code = response.get("code", "未知")
            raise ServiceError(f"飞书 API 错误（代码 {code}）")

    def _messages(self, params: dict, body: dict | None = None) -> dict:
        data = request_json(f"{self._MESSAGES_URL}?{urlencode(params)}", headers={
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }, body=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self._check(data)
        result = data.get("data")
        if not isinstance(result, dict):
            raise ServiceError("飞书返回了无效的消息数据")
        return result

    def history(self, chat_id: str, start: int, end: int) -> list[dict]:
        params = {
            "container_id_type": "chat", "container_id": chat_id,
            "start_time": start, "end_time": end, "sort_type": "ByCreateTimeAsc", "page_size": 50,
        }
        messages = []
        seen_tokens = set()
        while True:
            page = self._messages(params)
            messages.extend(page.get("items") or [])
            if not page.get("has_more"):
                return messages
            token = page.get("page_token")
            if not token or token in seen_tokens:
                raise ServiceError("飞书返回了无效的历史消息分页游标")
            seen_tokens.add(token)
            params["page_token"] = token

    def send_text(self, chat_id: str, text: str, request_id: str) -> str:
        result = self._messages({"receive_id_type": "chat_id"}, body={
            "receive_id": chat_id, "msg_type": "text", "uuid": request_id,
            "content": json.dumps({"text": text}, ensure_ascii=False),
        })
        message_id = result.get("message_id")
        if not message_id:
            raise ServiceError("飞书未返回发送结果，请在群里核对")
        return message_id
