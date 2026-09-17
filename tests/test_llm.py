"""LLM 客户端的测试：请求体组装（思考开关 / max_tokens）与截断、空内容报错。

运行：python -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
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


class ConfigParseTests(unittest.TestCase):
    def test_llm_options_parsed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text("llm:\n  reasoning_effort: high\n  max_tokens: 8192\n", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.llm.reasoning_effort, "high")
        self.assertEqual(config.llm.max_tokens, 8192)

    def test_llm_defaults_without_section(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text("llm:\n  model: deepseek-flash\n", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.llm.reasoning_effort, "none")
        self.assertEqual(config.llm.max_tokens, 4096)


if __name__ == "__main__":
    unittest.main()
