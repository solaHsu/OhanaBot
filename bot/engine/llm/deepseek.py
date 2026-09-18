"""DeepSeek 模型客户端（OpenAI 兼容接口，JSON 输出模式）。

Key 接口：优先使用构造参数 api_key，其次读环境变量 DEEPSEEK_API_KEY。
默认关闭思考（reasoning_effort="none"）：思考 token 计入 max_tokens 配额，
短回复场景既省钱又能避免截断；需要思考时显式传入 high/minimal 等档位。
对可重试类失败（空输出 / 限流 429 / 5xx / 网络抖动）自动退避重试
（llm.retries，默认 2 次）；截断与鉴权错误不重试，直接报错。
文档：https://api-docs.deepseek.com/zh-cn/

用法：
    client = DeepSeekClient()            # 读取 DEEPSEEK_API_KEY
    content = client.generate(prompt)    # 返回模型文本（提示词要求 JSON）
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

_RETRYABLE_HTTP_STATUS = (429, 500, 502, 503, 504)
_RETRY_BASE_SECONDS = 0.8


class _TransientError(RuntimeError):
    """可重试的调用失败：空输出 / 限流 / 服务端错误 / 网络抖动。"""


class DeepSeekClient:
    """对话补全的最小封装：一条 prompt 进、一段文本出；可重试类失败自动退避重试。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        reasoning_effort: str = "none",
        retries: int = 2,
    ):
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = (reasoning_effort or "").strip()  # none=关闭思考；思考 token 计入 max_tokens
        self.retries = max(int(retries), 0)  # 空输出/限流/网络抖动自动重试次数；截断与鉴权错误不重试

    def generate(self, prompt: str, images: list[str] | None = None) -> str:
        """调用 chat/completions 并返回文本内容（可附图 data URL；要求 JSON 输出，见提示词模板）。"""
        attempts = self.retries + 1
        delay = _RETRY_BASE_SECONDS
        for attempt in range(1, attempts + 1):
            try:
                return self._request_once(prompt, images)
            except _TransientError as exc:
                if attempt >= attempts:
                    raise
                logger.info(
                    "DeepSeek 调用失败（%s），%.1f 秒后重试（第 %d/%d 次）",
                    exc,
                    delay,
                    attempt + 1,
                    attempts,
                )
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("DeepSeek 调用失败")  # 不可达

    def _request_once(self, prompt: str, images: list[str] | None = None) -> str:
        """单次调用；可重试类失败抛 _TransientError，其余直接抛 RuntimeError。"""
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，或用 --api-key 传入")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(self.build_payload(prompt, images), ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            message = f"DeepSeek API 错误（HTTP {exc.code}）：{detail}"
            if exc.code in _RETRYABLE_HTTP_STATUS:
                raise _TransientError(message) from exc
            raise RuntimeError(message) from exc
        except urllib.error.URLError as exc:
            raise _TransientError(f"DeepSeek API 连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise _TransientError(f"DeepSeek API 连接超时：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _TransientError(f"DeepSeek 返回了非 JSON 响应：{exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length":
            raise RuntimeError(
                "DeepSeek 输出被 max_tokens 截断（思考 token 也占配额）："
                "请调大 llm.max_tokens，或把 llm.reasoning_effort 设为 none 关闭思考"
            )
        if not content.strip():
            raise _TransientError("DeepSeek 返回了空内容（JSON 模式偶发）")
        return content

    def build_payload(self, prompt: str, images: list[str] | None = None) -> dict:
        """请求体（便于排查与测试）；有图片时用 OpenAI 兼容的内容块数组。"""
        content: str | list[dict] = prompt
        if images:
            content = [{"type": "text", "text": prompt}]
            content += [{"type": "image_url", "image_url": {"url": url}} for url in images]
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload
