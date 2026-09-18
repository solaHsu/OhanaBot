"""引擎配置：读 config/bot.local.yaml（优先，不提交）；密钥只从环境变量取。

设计依据 docs/implementation-plan.md §2（配置基线）与 docs/engine-spec.md §1。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from bot.engine.policy.machine import PolicyConfig
from bot.tools.web_harness import WebHarnessOptions

POLICY_FIELD_TYPES = {item.name: item.type for item in fields(PolicyConfig)}

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
    reasoning_effort: str = "none"  # none=关闭思考（省钱防截断）；minimal/high 等档位开启
    max_tokens: int = 4096  # 输出上限（思考 token 也计入）

    def resolve_api_key(self) -> str:
        """API Key：本地直接值优先，否则按变量名查环境变量。"""
        return self.api_key or os.environ.get(self.api_key_env, "")


@dataclass
class KnowledgeConfig:
    enabled: bool = True  # 知识库（GraphRAG-lite）总开关：入库 + 检索注入
    auto_ingest: bool = True  # 调研任务完成后自动分类入库（仅知识类，新闻不入库）
    top_k: int = 2  # 每次注入的知识块上限
    max_chars: int = 600  # 注入文本预算（字符）
    min_matches: int = 2  # 词法命中词数下限（不足时仅实体图扩展可入选）
    graph_hops: int = 1  # 实体图扩展跳数（0 = 不做图扩展）
    inject_summaries: bool = True  # 命中实体时优先注入实体档案/主题综述（需先 consolidate 生成）
    consolidate_backend: str = "web"  # 档案/综述生成后端：web = 网页版（零 API token）；api = DeepSeek API
    classify_backend: str = "web"  # 入库审核（rebuild 与任务完成后自动入库）后端：web / api


@dataclass
class HarnessConfig:
    enabled: bool = False  # 重型任务分流（cli 需 dsh；web 需 playwright）
    backend: str = "cli"  # cli = dsh headless；web = DeepSeek 网页版自动化
    command: str = "dsh"  # 仅 cli 用
    timeout_seconds: float = 240.0
    model: str = ""  # 仅 cli 用；留空则用 dsh 默认模型
    workspace: str = "runtime/harness"  # 任务工作目录（相对仓库根；产物统一落此）
    web: WebHarnessOptions = field(default_factory=WebHarnessOptions)  # 仅 web 用


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
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)  # 判定参数（yaml `policy:` 可选覆盖）


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
    knowledge_raw = data.get("knowledge") or {}
    policy_raw = data.get("policy") or {}
    policy_overrides: dict = {}
    for key, value in policy_raw.items():
        annotation = POLICY_FIELD_TYPES.get(key)
        if value is None or annotation is None:
            continue
        policy_overrides[key] = float(value) if annotation == "float" else int(value)
    web_raw = harness_raw.get("web") or {}
    selectors_raw = web_raw.get("selectors") or {}
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
            reasoning_effort=str(llm_raw.get("reasoning_effort") or "none"),
            max_tokens=int(llm_raw.get("max_tokens") or 4096),
        ),
        harness=HarnessConfig(
            enabled=bool(harness_raw.get("enabled", False)),
            backend=str(harness_raw.get("backend") or "cli"),
            command=str(harness_raw.get("command") or "dsh"),
            timeout_seconds=float(harness_raw.get("timeout_seconds") or 240.0),
            model=str(harness_raw.get("model") or ""),
            workspace=str(harness_raw.get("workspace") or "runtime/harness"),
            web=WebHarnessOptions(
                profile_dir=str(web_raw.get("profile_dir") or "runtime/harness/web_profile"),
                chat_url=str(web_raw.get("chat_url") or "https://chat.deepseek.com/"),
                headless=bool(web_raw.get("headless", True)),
                deep_think=bool(web_raw.get("deep_think", True)),
                search=bool(web_raw.get("search", True)),
                new_chat=bool(web_raw.get("new_chat", True)),
                stable_seconds=float(web_raw.get("stable_seconds") or 5.0),
                queue_wait_seconds=float(web_raw.get("queue_wait_seconds") or 120.0),
                save_documents=bool(web_raw.get("save_documents", True)),
                selectors={str(key): str(value) for key, value in selectors_raw.items()},
            ),
        ),
        knowledge=KnowledgeConfig(
            enabled=bool(knowledge_raw.get("enabled", True)),
            auto_ingest=bool(knowledge_raw.get("auto_ingest", True)),
            top_k=int(knowledge_raw.get("top_k") or 2),
            max_chars=int(knowledge_raw.get("max_chars") or 600),
            min_matches=int(knowledge_raw.get("min_matches") or 2),
            graph_hops=int(knowledge_raw.get("graph_hops") if knowledge_raw.get("graph_hops") is not None else 1),
            inject_summaries=bool(knowledge_raw.get("inject_summaries", True)),
            consolidate_backend=str(knowledge_raw.get("consolidate_backend") or "web"),
            classify_backend=str(knowledge_raw.get("classify_backend") or "web"),
        ),
        policy=PolicyConfig(**policy_overrides),
    )
