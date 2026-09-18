"""LLM 客户端的测试：请求体组装（思考开关 / max_tokens）与截断、空内容报错。

运行：python -m unittest discover -s tests -v
"""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bot.engine.config import load_config
from bot.engine.llm import deepseek
from bot.engine.llm.deepseek import DeepSeekClient


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _ok_payload(content='{"action": "reply", "text": "好"}'):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def _empty_payload():
    return {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", None, io.BytesIO(b"boom"))


def patch_urlopen_sequence(testcase, results):
    """按序返回 payload dict / 抛出异常（超出长度时重复最后一项）；返回调用计数。"""
    calls = {"n": 0}
    original = deepseek.urllib.request.urlopen

    def fake(request, timeout=None):
        item = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    deepseek.urllib.request.urlopen = fake
    testcase.addCleanup(setattr, deepseek.urllib.request, "urlopen", original)
    return calls


class PayloadTests(unittest.TestCase):
    def test_default_disables_thinking(self):
        payload = DeepSeekClient(api_key="k").build_payload("hi")
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_reasoning_effort_passthrough(self):
        client = DeepSeekClient(api_key="k", reasoning_effort="high")
        self.assertEqual(client.build_payload("hi")["reasoning_effort"], "high")

    def test_empty_reasoning_effort_omits_key(self):
        client = DeepSeekClient(api_key="k", reasoning_effort="")
        self.assertNotIn("reasoning_effort", client.build_payload("hi"))

    def test_image_payload_uses_content_blocks(self):
        payload = DeepSeekClient(api_key="k").build_payload("hi", images=["data:image/png;base64,xx"])
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "hi"})
        self.assertEqual(content[1]["type"], "image_url")


class GenerateTests(unittest.TestCase):
    def _patch_urlopen(self, payload):
        original = deepseek.urllib.request.urlopen
        deepseek.urllib.request.urlopen = lambda request, timeout=None: _FakeResponse(payload)
        self.addCleanup(setattr, deepseek.urllib.request, "urlopen", original)

    def test_success_returns_content(self):
        client = DeepSeekClient(api_key="k")
        self._patch_urlopen({"choices": [{"message": {"content": "{\"a\": 1}"}, "finish_reason": "stop"}]})
        self.assertEqual(client.generate("hi"), "{\"a\": 1}")

    def test_truncated_raises_with_hint(self):
        client = DeepSeekClient(api_key="k")
        self._patch_urlopen({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        with self.assertRaises(RuntimeError) as ctx:
            client.generate("hi")
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertIn("reasoning_effort", str(ctx.exception))

    def test_empty_content_raises(self):
        client = DeepSeekClient(api_key="k")
        self._patch_urlopen({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})
        with self.assertRaises(RuntimeError):
            client.generate("hi")


class RetryTests(unittest.TestCase):
    def setUp(self):
        self._original_sleep = deepseek.time.sleep
        deepseek.time.sleep = lambda _seconds: None  # 测试不真等退避
        self.addCleanup(setattr, deepseek.time, "sleep", self._original_sleep)

    def test_empty_content_retried_then_success(self):
        client = DeepSeekClient(api_key="k")
        calls = patch_urlopen_sequence(self, [_empty_payload(), _ok_payload("重试成功")])
        self.assertEqual(client.generate("hi"), "重试成功")
        self.assertEqual(calls["n"], 2)

    def test_transient_failure_exhausts_retries(self):
        client = DeepSeekClient(api_key="k", retries=1)
        calls = patch_urlopen_sequence(self, [_empty_payload(), _empty_payload()])
        with self.assertRaises(RuntimeError) as ctx:
            client.generate("hi")
        self.assertIn("空内容", str(ctx.exception))
        self.assertEqual(calls["n"], 2)

    def test_retries_zero_disables(self):
        client = DeepSeekClient(api_key="k", retries=0)
        calls = patch_urlopen_sequence(self, [_empty_payload()])
        with self.assertRaises(RuntimeError):
            client.generate("hi")
        self.assertEqual(calls["n"], 1)

    def test_truncation_not_retried(self):
        client = DeepSeekClient(api_key="k")
        calls = patch_urlopen_sequence(
            self, [{"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}]
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.generate("hi")
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertEqual(calls["n"], 1)

    def test_server_error_retried(self):
        client = DeepSeekClient(api_key="k")
        calls = patch_urlopen_sequence(self, [_http_error(500), _ok_payload("恢复")])
        self.assertEqual(client.generate("hi"), "恢复")
        self.assertEqual(calls["n"], 2)

    def test_auth_error_not_retried(self):
        client = DeepSeekClient(api_key="k")
        calls = patch_urlopen_sequence(self, [_http_error(401)])
        with self.assertRaises(RuntimeError):
            client.generate("hi")
        self.assertEqual(calls["n"], 1)


class ConfigParseTests(unittest.TestCase):
    def test_llm_options_parsed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text(
            "llm:\n  reasoning_effort: high\n  max_tokens: 8192\n  retries: 0\n",
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.llm.reasoning_effort, "high")
        self.assertEqual(config.llm.max_tokens, 8192)
        self.assertEqual(config.llm.retries, 0)

    def test_llm_defaults_without_section(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text("llm:\n  model: deepseek-flash\n", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.llm.reasoning_effort, "none")
        self.assertEqual(config.llm.max_tokens, 4096)
        self.assertEqual(config.llm.retries, 2)


if __name__ == "__main__":
    unittest.main()
