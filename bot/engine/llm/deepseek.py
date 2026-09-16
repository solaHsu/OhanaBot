"""DeepSeek 模型客户端（OpenAI 兼容接口，JSON 输出模式）。

Key 接口：优先使用构造参数 api_key，其次读环境变量 DEEPSEEK_API_KEY。
文档：https://api-docs.deepseek.com/zh-cn/

用法：
    client = DeepSeekClient()            # 读取 DEEPSEEK_API_KEY
    content = client.generate(prompt)    # 返回模型文本（提示词要求 JSON）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class DeepSeekClient:
    """对话补全的最小封装：一条 prompt 进、一段文本出。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ):
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    def generate(self, prompt: str, images: list[str] | None = None) -> str:
        """调用 chat/completions 并返回文本内容（可附图 data URL；要求 JSON 输出，见提示词模板）。"""
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
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"DeepSeek API 错误（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API 连接失败：{exc.reason}") from exc

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length":
            raise RuntimeError("DeepSeek 输出被 max_tokens 截断，请调大 max_tokens 后重跑")
        if not content.strip():
            raise RuntimeError("DeepSeek 返回了空内容（JSON 模式偶发），请重跑")
        return content

    def build_payload(self, prompt: str, images: list[str] | None = None) -> dict:
        """请求体（便于排查与测试）；有图片时用 OpenAI 兼容的内容块数组。"""
        content: str | list[dict] = prompt
        if images:
            content = [{"type": "text", "text": prompt}]
            content += [{"type": "image_url", "image_url": {"url": url}} for url in images]
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
