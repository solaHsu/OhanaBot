"""引擎配置：读 config/bot.local.yaml（优先，不提交）；密钥只从环境变量取。

设计依据 docs/implementation-plan.md §2（配置基线）与 docs/engine-spec.md §1。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "bot.example.yaml"
LOCAL_CONFIG = REPO_ROOT / "config" / "bot.local.yaml"


@dataclass
class OneBotConfig:
    api_base: str = "http://127.0.0.1:3000"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    event_path: str = "/"
    expected_self_id: str | None = None
    access_token_env: str = "ONEBOT_ACCESS_TOKEN"
    event_secret_env: str = "ONEBOT_EVENT_SECRET"
    access_token: str = ""  # 直接值（仅建议填在不提交的本地文件）
    event_secret: str = ""
    allowed_groups: list[str] = field(default_factory=list)
    allowed_private_users: list[str] = field(default_factory=list)
    self_aliases: list[str] = field(default_factory=lambda: ["ohana", "欧哈娜"])

    def resolve_access_token(self) -> str:
        """访问令牌：本地直接值优先，否则按变量名查环境变量。"""
        return self.access_token or os.environ.get(self.access_token_env, "")

    def resolve_event_secret(self) -> str:
        """事件签名密钥：本地直接值优先，否则按变量名查环境变量。"""
        return self.event_secret or os.environ.get(self.event_secret_env, "")


@dataclass
class LLMConfig:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""  # 直接值（仅建议填在不提交的本地文件）
    api_key_env: str = "DEEPSEEK_API_KEY"

    def resolve_api_key(self) -> str:
        """API Key：本地直接值优先，否则按变量名查环境变量。"""
        return self.api_key or os.environ.get(self.api_key_env, "")


@dataclass
class HarnessConfig:
    enabled: bool = False  # 重型任务分流到 dsh headless（需先安装 dsh）
    command: str = "dsh"
    timeout_seconds: float = 240.0
    model: str = ""  # 留空则用 dsh 默认模型
    workspace: str = "runtime/harness"  # 任务工作目录（相对仓库根；产物统一落此）


@dataclass
class EngineConfig:
    mode: str = "dry_run"
    database: Path = REPO_ROOT / "runtime" / "ohana.sqlite3"
    send_enabled: bool = False
    merge_window_seconds: float = 3.0  # 参与态批窗口
    replies_per_hour: int = 20  # 每小时 LLM 调用上限
    max_reply_characters: int = 300  # 单条回复长度上限
    max_images_per_call: int = 3  # 每次生成附带的本批图片上限
    image_max_dimension: int = 768  # 图片压缩后长边上限（0 表示不压缩）
    max_links_per_call: int = 2  # 每次生成抓取的链接上限
    admins: list[str] = field(default_factory=list)  # /exit 等指令的可用账号
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)


def load_config(path: Path | str | None = None) -> EngineConfig:
    """加载配置；path 缺省时用 config/bot.local.yaml。"""
    config_path = Path(path) if path else LOCAL_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件：{config_path}。请复制 {EXAMPLE_CONFIG} 为 config/bot.local.yaml 并填写。"
        )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    onebot_raw = data.get("onebot") or {}
    engine_raw = data.get("engine") or {}

    database = Path(data.get("database") or "runtime/ohana.sqlite3")
    if not database.is_absolute():
        database = REPO_ROOT / database

    self_id = onebot_raw.get("expected_self_id")
    admins_raw = data.get("admins") or onebot_raw.get("allowed_private_users") or []
    llm_raw = data.get("llm") or {}
    harness_raw = data.get("harness") or {}
    image_max_dimension_raw = engine_raw.get("image_max_dimension")
    return EngineConfig(
        mode=str(data.get("mode") or "dry_run"),
        database=database,
        send_enabled=bool(engine_raw.get("send_enabled", False)),
        merge_window_seconds=float(engine_raw.get("merge_window_seconds") or 3.0),
        replies_per_hour=int(engine_raw.get("replies_per_hour") or 20),
        max_reply_characters=int(engine_raw.get("max_reply_characters") or 300),
        max_images_per_call=int(engine_raw.get("max_images_per_call") or 3),
        image_max_dimension=int(image_max_dimension_raw) if image_max_dimension_raw is not None else 768,
        max_links_per_call=int(engine_raw.get("max_links_per_call") or 2),
        admins=[str(uid) for uid in admins_raw],
        onebot=OneBotConfig(
            api_base=str(onebot_raw.get("api_base") or "http://127.0.0.1:3000"),
            listen_host=str(onebot_raw.get("listen_host") or "127.0.0.1"),
            listen_port=int(onebot_raw.get("listen_port") or 8080),
            event_path=str(onebot_raw.get("event_path") or "/"),
            expected_self_id=str(self_id) if self_id else None,
            access_token_env=str(onebot_raw.get("access_token_env") or "ONEBOT_ACCESS_TOKEN"),
            event_secret_env=str(onebot_raw.get("event_secret_env") or "ONEBOT_EVENT_SECRET"),
            access_token=str(onebot_raw.get("access_token") or ""),
            event_secret=str(onebot_raw.get("event_secret") or ""),
            allowed_groups=[str(gid) for gid in (onebot_raw.get("allowed_groups") or [])],
            allowed_private_users=[str(uid) for uid in (onebot_raw.get("allowed_private_users") or [])],
            self_aliases=[str(alias) for alias in (onebot_raw.get("self_aliases") or ["ohana", "欧哈娜"])],
        ),
        llm=LLMConfig(
            model=str(llm_raw.get("model") or "deepseek-flash"),
            base_url=str(llm_raw.get("base_url") or "https://api.deepseek.com"),
            api_key=str(llm_raw.get("api_key") or ""),
            api_key_env=str(llm_raw.get("api_key_env") or "DEEPSEEK_API_KEY"),
        ),
        harness=HarnessConfig(
            enabled=bool(harness_raw.get("enabled", False)),
            command=str(harness_raw.get("command") or "dsh"),
            timeout_seconds=float(harness_raw.get("timeout_seconds") or 240.0),
            model=str(harness_raw.get("model") or ""),
            workspace=str(harness_raw.get("workspace") or "runtime/harness"),
        ),
    )
