"""任务分流桥接：把重型任务交给后台执行器（设计见 docs/engine-spec.md §6）。

- `HarnessBridge`：子进程调用 `dsh --profile headless "<任务>"`：stdout 为最终回答、
  stderr 为推理流（丢弃）；超时或失败返回 None；Windows 下自动处理 .cmd 包装与
  %APPDATA%\\npm 回退路径；
- `create_bridge`：按 `harness.backend` 选实现（cli = 本文件；web = `bot/tools/web_harness.py`），
  两个实现接口一致（enabled / available / run_task）；
- 由引擎在模型返回 action=task 时调用；未启用时一切照旧。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 240.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class HarnessBridge:
    """dsh headless 的最小桥接。"""

    def __init__(
        self,
        command: str = "dsh",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        enabled: bool = False,
        api_key: str = "",
        model: str = "",
        workspace: str = "",
    ):
        self.command = command or "dsh"
        self.timeout = timeout
        self.enabled = enabled
        self.api_key = api_key or ""
        self.model = model or ""
        self.workspace = workspace or ""

    @property
    def available(self) -> bool:
        return self._resolve_command() is not None

    def run_task(self, task: str) -> str | None:
        """跑一个 headless 任务，返回最终回答；不可用 / 超时 / 失败返回 None。"""
        task = (task or "").strip()
        if not self.enabled or not task:
            return None
        program = self._resolve_command()
        if not program:
            logger.warning("找不到 dsh 命令（%s），任务分流跳过", self.command)
            return None
        argv = [program, "--profile", "headless", task]
        if os.name == "nt" and program.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c"] + argv
        env = dict(os.environ)
        if self.api_key:
            env["DEEPSEEK_API_KEY"] = self.api_key
        if self.model:
            env["DSH_MODEL"] = self.model
        cwd = None
        if self.workspace:
            try:
                Path(self.workspace).mkdir(parents=True, exist_ok=True)
                cwd = self.workspace
            except OSError as exc:
                logger.warning("Harness 工作目录不可用（%s）：%s", self.workspace, exc)
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            logger.warning("Harness 任务启动失败：%s", exc)
            return None
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except Exception:
                stdout, stderr = "", ""
            logger.warning("Harness 任务超时（%.0f 秒），已终止进程树", self.timeout)
            return None
        if process.returncode != 0:
            tail = _clean(stderr or "")[-200:]
            logger.warning("Harness 任务退出码 %s：%s", process.returncode, tail)
            return None
        output = _clean(stdout or "")
        return output or None

    def _resolve_command(self) -> str | None:
        found = shutil.which(self.command)
        if found:
            return found
        if os.name == "nt" and self.command in ("dsh", "dsh.cmd") and os.environ.get("APPDATA"):
            fallback = Path(os.environ["APPDATA"]) / "npm" / "dsh.cmd"
            if fallback.is_file():
                return str(fallback)
        return None


def create_bridge(
    *,
    enabled: bool,
    backend: str = "cli",
    command: str = "dsh",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    api_key: str = "",
    model: str = "",
    workspace: str = "",
    web_options=None,
):
    """按 harness.backend 选择桥接实现：cli = dsh headless；web = DeepSeek 网页版自动化。

    两个实现接口一致（enabled / available / run_task），引擎侧无感知。
    """
    if str(backend or "cli").strip().lower() == "web":
        from bot.tools.web_harness import WebHarnessBridge

        return WebHarnessBridge(enabled=enabled, timeout=timeout, workspace=workspace, options=web_options)
    return HarnessBridge(
        enabled=enabled,
        command=command,
        timeout=timeout,
        api_key=api_key,
        model=model,
        workspace=workspace,
    )


def _kill_tree(process: subprocess.Popen) -> None:
    """超时后终止整个进程树（Windows 用 taskkill /T，其他平台 kill）。

    .cmd 包装层会派生 node 孙进程；只杀直接子进程会让管道被孙进程占住，
    导致后续 communicate 永久阻塞。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
            )
            return
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()
