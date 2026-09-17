"""网页版桥接的测试：启用/可用条件、提示词包装、成功落档、失败与排队、工厂与配置解析。

运行：python -m unittest discover -s tests -v
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from bot.engine.config import EngineConfig, load_config
from bot.tools import harness, web_harness


class _FakeDriver:
    def __init__(self, result="调研结果正文", error=None):
        self.result = result
        self.error = error
        self.prompts = []

    def ask(self, prompt, timeout):
        self.prompts.append((prompt, timeout))
        if self.error:
            raise self.error
        return self.result


class WebHarnessBridgeTests(unittest.TestCase):
    def _bridge(self, driver=None, **overrides):
        options = web_harness.WebHarnessOptions(**overrides)
        factory = (lambda: driver) if driver is not None else None
        return web_harness.WebHarnessBridge(
            enabled=True, timeout=99.0, options=options, driver_factory=factory
        )

    def _patch_playwright(self, available):
        original = web_harness._playwright_available
        web_harness._playwright_available = lambda: available
        self.addCleanup(setattr, web_harness, "_playwright_available", original)

    def test_disabled_returns_none(self):
        bridge = web_harness.WebHarnessBridge(enabled=False)
        self.assertIsNone(bridge.run_task("任务"))

    def test_empty_task_returns_none(self):
        bridge = self._bridge(driver=_FakeDriver())
        self.assertIsNone(bridge.run_task(""))

    def test_unavailable_without_playwright_returns_none(self):
        bridge = web_harness.WebHarnessBridge(enabled=True)
        self._patch_playwright(False)
        self.assertFalse(bridge.available)
        self.assertIsNone(bridge.run_task("任务"))

    def test_success_returns_text_and_shapes_prompt(self):
        driver = _FakeDriver(result="调研结果正文")
        bridge = self._bridge(driver=driver)
        self.assertEqual(bridge.run_task("调研 X 主题"), "调研结果正文")
        prompt, timeout = driver.prompts[0]
        self.assertIn("调研 X 主题", prompt)
        self.assertIn("Markdown", prompt)
        self.assertEqual(timeout, 99.0)

    def test_login_required_returns_none(self):
        driver = _FakeDriver(error=web_harness.LoginRequired("未登录"))
        bridge = self._bridge(driver=driver)
        self.assertIsNone(bridge.run_task("任务"))

    def test_driver_error_returns_none(self):
        driver = _FakeDriver(error=RuntimeError("boom"))
        bridge = self._bridge(driver=driver)
        self.assertIsNone(bridge.run_task("任务"))

    def test_empty_result_returns_none(self):
        driver = _FakeDriver(result="   ")
        bridge = self._bridge(driver=driver)
        self.assertIsNone(bridge.run_task("任务"))

    def test_document_saved(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        driver = _FakeDriver(result="正文内容")
        bridge = self._bridge(driver=driver)
        bridge.workspace = tmp.name
        self.assertEqual(bridge.run_task("插件生态调研"), "正文内容")
        files = list(Path(tmp.name).glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("web-"))
        content = files[0].read_text(encoding="utf-8")
        self.assertIn("插件生态调研", content)
        self.assertIn("正文内容", content)

    def test_document_saving_disabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        driver = _FakeDriver()
        bridge = self._bridge(driver=driver, save_documents=False)
        bridge.workspace = tmp.name
        self.assertEqual(bridge.run_task("任务"), "调研结果正文")
        self.assertEqual(list(Path(tmp.name).glob("*.md")), [])

    def test_busy_queue_returns_none(self):
        driver = _FakeDriver()
        bridge = self._bridge(driver=driver, queue_wait_seconds=0.0)
        bridge._lock.acquire()
        self.addCleanup(bridge._lock.release)
        self.assertIsNone(bridge.run_task("任务"))
        self.assertEqual(driver.prompts, [])

    def test_result_cleaned_before_return_and_save(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        driver = _FakeDriver(result="结论一-\n7\n。结论二")
        bridge = self._bridge(driver=driver)
        bridge.workspace = tmp.name
        self.assertEqual(bridge.run_task("任务"), "结论一。结论二")
        content = list(Path(tmp.name).glob("*.md"))[0].read_text(encoding="utf-8")
        self.assertIn("结论一。结论二", content)


class CitationCleanTests(unittest.TestCase):
    def test_citation_tail_removed(self):
        self.assertEqual(web_harness.clean_citation_noise("文本理解-\n59\n。继续"), "文本理解。继续")

    def test_citation_at_answer_end_removed(self):
        self.assertEqual(web_harness.clean_citation_noise("技术报告已公开-\n59"), "技术报告已公开")

    def test_plain_number_line_kept(self):
        text = "价格是\n2026\n元吗？"
        self.assertEqual(web_harness.clean_citation_noise(text), text)

    def test_markdown_list_item_kept(self):
        text = "- 2026 年发布\n- 2027 年上市"
        self.assertEqual(web_harness.clean_citation_noise(text), text)


class CreateBridgeTests(unittest.TestCase):
    def test_backend_web_selects_web_bridge(self):
        bridge = harness.create_bridge(enabled=True, backend="web")
        self.assertIsInstance(bridge, web_harness.WebHarnessBridge)

    def test_backend_cli_selects_cli_bridge(self):
        bridge = harness.create_bridge(enabled=True, backend="cli", command="dsh")
        self.assertIsInstance(bridge, harness.HarnessBridge)

    def test_unknown_backend_falls_back_to_cli(self):
        bridge = harness.create_bridge(enabled=True, backend="")
        self.assertIsInstance(bridge, harness.HarnessBridge)

    def test_web_options_passed_through(self):
        options = web_harness.WebHarnessOptions(stable_seconds=9.0)
        bridge = harness.create_bridge(enabled=True, backend="WEB", web_options=options)
        self.assertEqual(bridge.options.stable_seconds, 9.0)


class WebHarnessConfigTests(unittest.TestCase):
    def _load(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_engine_defaults(self):
        config = EngineConfig()
        self.assertEqual(config.harness.backend, "cli")
        self.assertEqual(config.harness.web.profile_dir, "runtime/harness/web_profile")
        self.assertTrue(config.harness.web.headless)
        self.assertTrue(config.harness.web.save_documents)

    def test_web_options_parsed(self):
        config = self._load(
            """
harness:
  enabled: true
  backend: web
  timeout_seconds: 300
  web:
    profile_dir: runtime/harness/profile-x
    headless: false
    deep_think: false
    search: false
    new_chat: false
    stable_seconds: 8
    queue_wait_seconds: 5
    save_documents: false
    selectors:
      input: "textarea#x"
"""
        )
        self.assertTrue(config.harness.enabled)
        self.assertEqual(config.harness.backend, "web")
        self.assertEqual(config.harness.timeout_seconds, 300.0)
        web = config.harness.web
        self.assertEqual(web.profile_dir, "runtime/harness/profile-x")
        self.assertFalse(web.headless)
        self.assertFalse(web.deep_think)
        self.assertFalse(web.search)
        self.assertFalse(web.new_chat)
        self.assertEqual(web.stable_seconds, 8.0)
        self.assertEqual(web.queue_wait_seconds, 5.0)
        self.assertFalse(web.save_documents)
        self.assertEqual(web.selectors, {"input": "textarea#x"})

    def test_missing_web_section_keeps_defaults(self):
        config = self._load(
            """
harness:
  enabled: true
  backend: web
"""
        )
        self.assertEqual(config.harness.web.headless, True)
        self.assertEqual(config.harness.web.chat_url, "https://chat.deepseek.com/")


class SelectorDefaultsTests(unittest.TestCase):
    def test_search_toggle_texts(self):
        self.assertIn("智能搜索", web_harness.SEARCH_TEXTS)

    def test_default_selectors(self):
        driver = web_harness.PlaywrightWebChat(web_harness.WebHarnessOptions())
        self.assertEqual(driver._answer_selectors[0], ".ds-assistant-message-main-content")
        self.assertIn(".ds-markdown", driver._answer_selectors)
        self.assertIn("following", driver._copy_selector)

    def test_selector_overrides(self):
        options = web_harness.WebHarnessOptions(selectors={"copy": "xpath=.//b", "answer": "#x"})
        driver = web_harness.PlaywrightWebChat(options)
        self.assertEqual(driver._copy_selector, "xpath=.//b")
        self.assertEqual(driver._answer_selectors[0], "#x")
        self.assertIn(".ds-markdown", driver._answer_selectors)


class ProfileResetTests(unittest.TestCase):
    def test_backup_profile_renames_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / "web_profile"
        profile.mkdir()
        (profile / "Cookies").write_text("x", encoding="utf-8")
        backup = web_harness._backup_profile(profile)
        self.assertIsNotNone(backup)
        self.assertFalse(profile.exists())
        self.assertTrue(backup.name.startswith("web_profile.backup-"))
        self.assertTrue((backup / "Cookies").exists())

    def test_backup_profile_missing_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(web_harness._backup_profile(Path(tmp.name) / "no-such-dir"))


class LoginTests(unittest.TestCase):
    def test_login_without_playwright_returns_2(self):
        original = web_harness._playwright_available
        web_harness._playwright_available = lambda: False
        self.addCleanup(setattr, web_harness, "_playwright_available", original)
        with contextlib.redirect_stdout(io.StringIO()):
            code = web_harness.login("runtime/harness/web_profile", "https://chat.deepseek.com/")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
