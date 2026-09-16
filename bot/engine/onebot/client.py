"""OneBot 11 HTTP API 客户端（最小动作集：登录信息、群/私聊发送）。

设计依据 docs/engine-spec.md §1：Bearer 鉴权；只开放实现需要的动作。
文档参考：OneBot 11 标准（NapCat / Lagrange 通用）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class OneBotClient:
    """同步 HTTP 客户端；错误统一抛 RuntimeError（附平台返回摘要）。"""

    def __init__(self, api_base: str, access_token: str = "", timeout: float = 30.0):
        self.api_base = api_base.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    def _call(self, action: str, **params) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(
            f"{self.api_base}/{action}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"OneBot API 错误（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OneBot API 连接失败：{exc.reason}") from exc

    def get_login_info(self) -> dict:
        """返回 {"user_id", "nickname"}；失败抛 RuntimeError。"""
        result = self._call("get_login_info")
        return result.get("data") or {}

    def send_group_msg(self, group_id: str, text: str) -> dict:
        """发送纯文本群消息；返回平台原始响应（含 status/retcode/data）。"""
        return self._call(
            "send_group_msg",
            group_id=int(group_id),
            message=[{"type": "text", "data": {"text": text}}],
        )

    def send_private_msg(self, user_id: str, text: str) -> dict:
        """发送纯文本私聊消息（管理员指令通道预留）。"""
        return self._call(
            "send_private_msg",
            user_id=int(user_id),
            message=[{"type": "text", "data": {"text": text}}],
        )
