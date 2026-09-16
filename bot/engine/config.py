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

    def resolve_access_token(self) -> str:
        """访问令牌：本地直接值优先，否则按变量名查环境变量。"""
        return self.access_token or os.environ.get(self.access_token_env, "")

    def resolve_event_secret(self) -> str:
        """事件签名密钥：本地直接值优先，否则按变量名查环境变量。"""
        return self.event_secret or os.environ.get(self.event_secret_env, "")


@dataclass
class EngineConfig:
    mode: str = "dry_run"
    database: Path = REPO_ROOT / "runtime" / "ohana.sqlite3"
    send_enabled: bool = False
    onebot: OneBotConfig = field(default_factory=OneBotConfig)


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
    return EngineConfig(
        mode=str(data.get("mode") or "dry_run"),
        database=database,
        send_enabled=bool(engine_raw.get("send_enabled", False)),
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
        ),
    )
