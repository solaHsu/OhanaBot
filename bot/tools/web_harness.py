"""DeepSeek 网页版桥接：把重型调研任务交给 chat.deepseek.com 执行。

替代 dsh headless 的零 API 方案（设计见 docs/engine-spec.md §6）：

- `WebHarnessBridge` 与 `bot/tools/harness.py` 的 CLI 桥接口径一致：
  `enabled` / `available` / `run_task(task) -> str | None`，由引擎按 `harness.backend` 选择；
- 浏览器用 Playwright 持久化用户目录（`harness.web.profile_dir`）保存登录态，
  首次登录运行 `python -m bot.engine.manage harness-login`；
- 同一进程内任务串行执行（一把锁），结果可选落成 Markdown 文档（workspace 下）；
- 取回答优先点网页版「复制」按钮，拿 Markdown 源文（保留标题/表格/列表），
  失败时退回纯文本抽取（并清洗引用角标残留）；
- playwright 按需导入：未安装时 `available=False`，任务分流自动跳过；
- 页面改版时用 `harness.web.selectors`（input / answer / copy / stop）覆盖默认选择器。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_HINT = "python -m bot.engine.manage harness-login"
INSTALL_HINT = "pip install playwright，再运行 python -m playwright install chromium"

# 默认选择器与按钮文案（页面改版时优先在 harness.web.selectors 覆盖）
DEFAULT_INPUT_SELECTORS = ("textarea#chat-input", "textarea", "div[role='textbox']")
DEFAULT_ANSWER_SELECTOR = ".ds-assistant-message-main-content"  # 主回答（不含思考区）
FALLBACK_ANSWER_SELECTOR = ".ds-markdown"  # 兜底：旧版页面的主回答类名
DEFAULT_COPY_SELECTOR = "xpath=following::div[@role='button'][1]"  # 主回答后第一个按钮＝复制
NEW_CHAT_TEXTS = ("开启新对话", "新对话", "New chat")
DEEP_THINK_TEXTS = ("深度思考", "DeepThink", "Deep Think")
SEARCH_TEXTS = ("智能搜索", "联网搜索", "Search")
STOP_TEXTS = ("停止", "停止生成", "Stop generating")
_INPUT_WAIT_SECONDS = 30.0  # 打开页面后等输入框出现的窗口（兼作登录态检查）
_POLL_SECONDS = 1.0
# 网页版引用角标（如「-59」）在纯文本提取时会拆成「连字符 + 独立数字行」，落档前清洗掉
_CITATION_NOISE_RE = re.compile(r"[\-\u2013\u2212]\n\d{1,4}(?:\n|$)")
# 无头模式 UA 去掉 HeadlessChrome 标记，避免被网页风控拦下
_HEADLESS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 点「复制」前安装剪贴板钩子：截获网页版写入的 Markdown 源文（不依赖系统剪贴板读取权限）
_CLIPBOARD_HOOK_JS = """() => {
    window.__ohanaCopied = null;
    try {
        const original = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async (t) => {
            window.__ohanaCopied = t;
            try { return await original(t); } catch (e) {}
        };
    } catch (e) {
        window.__ohanaCopied = null;
    }
}"""


@dataclass
class WebHarnessOptions:
    """网页版桥接选项（由 config.harness.web 解析而来）。"""

    profile_dir: str = "runtime/harness/web_profile"  # 登录态目录（相对仓库根）
    chat_url: str = "https://chat.deepseek.com/"
    headless: bool = True  # 有头窗口仅首次登录需要
    deep_think: bool = True  # 发送前尝试打开「深度思考」
    search: bool = True  # 发送前尝试打开「智能搜索（联网）」
    new_chat: bool = True  # 每次任务开新对话
    stable_seconds: float = 5.0  # 输出连续无变化超过该秒数视为完成
    queue_wait_seconds: float = 120.0  # 等待其它任务让出浏览器的最长时间
    save_documents: bool = True  # 结果落成 Markdown 文档
    selectors: dict[str, str] = field(default_factory=dict)  # 覆盖 input / answer / copy / stop


class LoginRequired(RuntimeError):
    """页面找不到输入框：多半是未登录或登录态过期。"""


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def build_research_prompt(task: str, today: str | None = None) -> str:
    """把群里来的任务包装成调研指令：联网核实 + 输出自包含 Markdown 文档。"""
    date = today or datetime.now().strftime("%Y-%m-%d")
    return (
        "你是资料调研员。请完成下面的任务：先联网搜索核实事实与最新进展"
        f"（今天是 {date}），再直接输出结果。\n"
        f"任务：{task}\n\n"
        "要求：\n"
        "- 输出一份自包含的 Markdown 文档：第一行是标题，正文按主题分节，"
        "信息密度高，可被直接引用为文案资料。\n"
        "- 关键事实（版本号、日期、价格、数据）附来源链接；不确定或存在争议的"
        "信息要标注，不要编造。\n"
        "- 不要寒暄、不要询问确认、不要输出思考过程，直接开始正文。"
    )


def clean_citation_noise(text: str) -> str:
    """去掉网页版引用角标在纯文本里的残留（段落末尾的「连字符 + 独立数字行」）。"""
    return _CITATION_NOISE_RE.sub("", text)


class PlaywrightWebChat:
    """一次任务的完整浏览器流程：开新对话 → 打开能力 → 提问 → 等输出稳定 → 复制/抽取回答。"""

    def __init__(self, options: WebHarnessOptions):
        self.options = options
        input_override = options.selectors.get("input", "").strip()
        self._input_selectors = tuple(
            selector for selector in (input_override,) + DEFAULT_INPUT_SELECTORS if selector
        )
        self._answer_selectors = tuple(
            dict.fromkeys(
                selector
                for selector in (
                    options.selectors.get("answer", "").strip() or DEFAULT_ANSWER_SELECTOR,
                    FALLBACK_ANSWER_SELECTOR,
                )
                if selector
            )
        )
        self._copy_selector = options.selectors.get("copy", "").strip() or DEFAULT_COPY_SELECTOR
        self._stop_selector = options.selectors.get("stop", "").strip()

    @property
    def profile_dir(self) -> Path:
        return _resolve_dir(self.options.profile_dir)

    def ask(self, prompt: str, timeout: float) -> str:
        """发送一条提示词并取回回答；未登录抛 LoginRequired，等答案超时抛 TimeoutError。"""
        from playwright.sync_api import sync_playwright

        deadline = time.monotonic() + max(timeout, 30.0)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.options.headless,
                locale="zh-CN",
                user_agent=_HEADLESS_UA if self.options.headless else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                page.goto(self.options.chat_url, wait_until="domcontentloaded", timeout=45000)
                input_locator = self._wait_input(page)
                if self.options.new_chat and self._click_first_text(page, NEW_CHAT_TEXTS):
                    input_locator = self._wait_input(page)
                self._ensure_toggle(page, DEEP_THINK_TEXTS, self.options.deep_think)
                self._ensure_toggle(page, SEARCH_TEXTS, self.options.search)
                baseline = self._answer_locator(page).count()
                input_locator.click()
                input_locator.fill(prompt)
                page.wait_for_timeout(200)
                page.keyboard.press("Enter")
                answer = self._wait_answer(page, baseline, deadline)
                fallback = ""
                try:
                    fallback = answer.inner_text().strip()
                except Exception:
                    pass
                copied = self._copy_answer(page, answer)
                return (copied or fallback).strip()
            finally:
                context.close()

    # ---------- 页面操作 ----------

    def _wait_input(self, page):
        """等输入框可见（触发登录态检查）；等不到抛 LoginRequired。"""
        deadline = time.monotonic() + _INPUT_WAIT_SECONDS
        while time.monotonic() < deadline:
            for selector in self._input_selectors:
                locator = page.locator(selector).last
                try:
                    if locator.count() and locator.is_visible():
                        return locator
                except Exception:
                    continue
            page.wait_for_timeout(int(_POLL_SECONDS * 1000))
        raise LoginRequired("页面未出现输入框（未登录或页面结构变化）")

    def _click_first_text(self, page, texts: tuple[str, ...]) -> bool:
        """按文案（子串匹配，容忍「开启新对话」类前缀）点第一个可见元素；点不到返回 False。"""
        for text in texts:
            locator = page.get_by_text(text)
            try:
                count = min(locator.count(), 3)
            except Exception:
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if not element.is_visible():
                        continue
                    element.click(timeout=3000)
                    page.wait_for_timeout(500)
                    return True
                except Exception:
                    continue
        return False

    def _ensure_toggle(self, page, texts: tuple[str, ...], want: bool) -> None:
        """尝试打开「深度思考」/「智能搜索」；优先直接扫 ds-toggle-button 根节点（带 aria-pressed 状态）。

        状态判定不了时会点一下（页面本身已开则可能反向关闭），可把对应配置项关掉。
        """
        if not want:
            return
        try:
            roots = page.locator(".ds-toggle-button")
            for index in range(min(roots.count(), 8)):
                element = roots.nth(index)
                label = (element.inner_text() or "").strip()
                if not any(text in label for text in texts):
                    continue
                if not element.is_visible():
                    continue
                if self._is_active(element):
                    return
                element.click(timeout=3000)
                logger.debug("网页版桥接：已点击「%s」尝试开启", label)
                return
        except Exception:
            pass
        for text in texts:
            locator = page.get_by_text(text)
            try:
                count = min(locator.count(), 3)
            except Exception:
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if not element.is_visible():
                        continue
                    if self._is_active(element):
                        return
                    element.click(timeout=3000)
                    logger.debug("网页版桥接：已点击「%s」尝试开启", text)
                    return
                except Exception:
                    continue

    def _is_active(self, element) -> bool:
        """状态启发式：aria-pressed / aria-checked 优先，其次向上三层找激活样式类名。"""
        try:
            pressed = element.get_attribute("aria-pressed") or element.get_attribute("aria-checked")
            if pressed is not None:
                return pressed == "true"
            return bool(
                element.evaluate(
                    """(el) => {
                        let node = el;
                        for (let i = 0; i < 3 && node; i += 1) {
                            const cls = (node.className || '').toString().toLowerCase();
                            if (/active|selected|checked|enabled/.test(cls)) return true;
                            node = node.parentElement;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    def _generating(self, page) -> bool:
        """是否仍在生成：优先自定义 stop 选择器，其次扫描停止按钮文案。"""
        try:
            if self._stop_selector and page.locator(self._stop_selector).count():
                return True
            return bool(
                page.evaluate(
                    """() => {
                        const texts = ['停止', '停止生成', 'Stop generating'];
                        for (const el of document.querySelectorAll('button, [role="button"]')) {
                            if (texts.includes((el.innerText || '').trim())) return true;
                        }
                        return false;
                    }"""  # 与 STOP_TEXTS 保持一致（JS 内不可见 Python 常量）
                )
            )
        except Exception:
            return False

    def _answer_locator(self, page):
        """主回答定位：优先新页面类名（不含思考区），找不到时退回 ds-markdown。"""
        for selector in self._answer_selectors:
            locator = page.locator(selector)
            try:
                if locator.count():
                    return locator
            except Exception:
                continue
        return page.locator(self._answer_selectors[0])

    def _wait_answer(self, page, baseline: int, deadline: float):
        """轮询回答区：出现新块后，连续 stable_seconds 无变化且不在生成中即视为完成；返回回答节点。"""
        started = False
        last_length = -1
        stable_since: float | None = None
        while time.monotonic() < deadline:
            answers = self._answer_locator(page)
            try:
                count = answers.count()
            except Exception:
                count = baseline
            if not started and count > baseline:
                started = True
            if started and count > 0:
                node = answers.nth(count - 1)
                try:
                    text = node.inner_text()
                except Exception:
                    text = ""
                now = time.monotonic()
                if text and len(text) != last_length:
                    last_length, stable_since = len(text), now
                elif (
                    text
                    and stable_since is not None
                    and now - stable_since >= self.options.stable_seconds
                    and not self._generating(page)
                ):
                    return node
            page.wait_for_timeout(int(_POLL_SECONDS * 1000))
        raise TimeoutError("等待网页版回答超时")

    def _copy_answer(self, page, answer) -> str | None:
        """点网页版「复制」按钮取回 Markdown 源文；失败返回 None（调用方退回纯文本）。"""
        try:
            page.evaluate(_CLIPBOARD_HOOK_JS)
            answer.hover()
            page.wait_for_timeout(400)
            button = answer.locator(self._copy_selector)
            if not button.count():
                return None
            button.first.click(timeout=3000)
        except Exception:
            return None
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            try:
                copied = page.evaluate("() => window.__ohanaCopied")
            except Exception:
                return None
            if copied:
                return str(copied)
            page.wait_for_timeout(300)
        return None


class WebHarnessBridge:
    """dsh 桥接的网页版替代：同接口（enabled / available / run_task）。"""

    def __init__(
        self,
        enabled: bool = False,
        timeout: float = 240.0,
        workspace: str = "",
        options: WebHarnessOptions | None = None,
        driver_factory=None,
    ):
        self.enabled = enabled
        self.timeout = timeout
        self.workspace = workspace or ""
        self.options = options or WebHarnessOptions()
        self._driver_factory = driver_factory
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        if self._driver_factory is not None:
            return True
        return _playwright_available()

    def run_task(self, task: str) -> str | None:
        """跑一个调研任务，返回最终回答；不可用 / 未登录 / 超时 / 失败返回 None。"""
        task = (task or "").strip()
        if not self.enabled or not task:
            return None
        if not self.available:
            logger.warning("网页版桥接不可用（%s），任务分流跳过", INSTALL_HINT)
            return None
        if not self._lock.acquire(timeout=max(self.options.queue_wait_seconds, 0.0)):
            logger.warning("网页版桥接正忙（已等 %.0f 秒），本次任务跳过", self.options.queue_wait_seconds)
            return None
        try:
            driver = self._driver_factory() if self._driver_factory else PlaywrightWebChat(self.options)
            result = driver.ask(build_research_prompt(task), timeout=self.timeout)
        except LoginRequired:
            logger.warning("网页版未登录或登录态过期：请先运行 %s", LOGIN_HINT)
            return None
        except Exception as exc:
            logger.warning("网页版任务失败：%s", exc)
            return None
        finally:
            self._lock.release()
        result = clean_citation_noise((result or "").strip()).strip()
        if not result:
            logger.warning("网页版任务返回空结果")
            return None
        self._save_document(task, result)
        return result

    def _save_document(self, task: str, result: str) -> None:
        """把结果落成 Markdown 文档（Ohana 的文案资料）；失败不影响任务结果。"""
        if not self.options.save_documents or not self.workspace:
            return
        try:
            workspace = Path(self.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            path = workspace / f"web-{now:%Y%m%d-%H%M%S}-{_slug(task)}.md"
            title = task.splitlines()[0][:80]
            header = (
                f"# {title}\n\n"
                f"> 来源：DeepSeek 网页版（调研桥接） | 时间：{now:%Y-%m-%d %H:%M} | 任务：{task}\n\n"
            )
            path.write_text(header + result.rstrip() + "\n", encoding="utf-8")
            logger.info("网页版任务产出文档：%s", path)
        except OSError as exc:
            logger.warning("网页版任务文档落盘失败：%s", exc)


def login(profile_dir: str | Path, chat_url: str, timeout: float = 300.0, reset: bool = False) -> int:
    """有头打开网页版，检测到输入框（登录完成）后保存登录态并退出。

    reset=True（换账号）时先把现有登录态目录改名成带时间戳的备份，再开全新会话。
    """
    if not _playwright_available():
        print(f"未安装 playwright：{INSTALL_HINT}")
        return 2
    from playwright.sync_api import sync_playwright

    directory = _resolve_dir(profile_dir)
    if reset:
        try:
            backup = _backup_profile(directory)
        except OSError as exc:
            print(f"备份旧登录态失败（可能被浏览器占用，先关闭其它实例再试）：{exc}")
            return 2
        if backup is not None:
            print(f"已备份旧登录态：{backup}（如需回切旧账号，把目录名改回 {directory.name} 即可）")
    directory.mkdir(parents=True, exist_ok=True)
    print(f"登录态目录：{directory}")
    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(str(directory), headless=False, locale="zh-CN")
        except Exception as exc:
            print(f"浏览器启动失败：{exc}（若提示未安装浏览器，请运行 python -m playwright install chromium）")
            return 2
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(chat_url, wait_until="domcontentloaded", timeout=45000)
            print("请在打开的窗口里完成登录（扫码或账号密码）；检测到输入框即视为完成并自动保存。")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    for selector in DEFAULT_INPUT_SELECTORS:
                        locator = page.locator(selector).last
                        if locator.count() and locator.is_visible():
                            print(f"登录成功，登录态已保存：{directory}")
                            return 0
                    page.wait_for_timeout(2000)
                except Exception as exc:
                    print(f"窗口已关闭或页面不可用（{exc}），未保存登录态。")
                    return 1
            print("等待登录超时，未保存有效登录态。")
            return 1
        finally:
            context.close()


def _backup_profile(directory: Path) -> Path | None:
    """换账号用：把现有登录态目录改名备份；目录不存在返回 None，改名失败抛 OSError。"""
    if not directory.exists():
        return None
    backup = directory.with_name(f"{directory.name}.backup-{datetime.now():%Y%m%d-%H%M%S}")
    directory.rename(backup)
    return backup


def _resolve_dir(path: str | Path) -> Path:
    directory = Path(path)
    return directory if directory.is_absolute() else REPO_ROOT / directory


def _slug(text: str, limit: int = 32) -> str:
    """文件名片段：保留中英文与数字，其余折叠为连字符。"""
    first_line = (text.strip().splitlines() or [""])[0]
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", first_line).strip("-")
    return slug[:limit] or "task"
