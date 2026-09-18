"""Ohana 引擎入口：加载配置 → 启动 OneBot 事件服务 → 消息入库。

用法（在仓库根目录执行）：
    python -m bot.engine.main                 # 使用 config/bot.local.yaml
    python -m bot.engine.main --config 路径    # 指定配置文件

当前阶段：事件接收 → 入库 → 判定运行时（3 秒批）→ 回复生成 → 发送（dry-run 只出草稿；live + send_enabled 才真正发送）。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading

from bot.engine.config import load_config
from bot.engine.ingest import MessageIngestor
from bot.engine.llm import DeepSeekClient
from bot.engine.onebot.client import OneBotClient
from bot.engine.onebot.server import EventServer
from bot.engine.runtime import EngineRuntime
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore

logger = logging.getLogger("ohana.engine")


def _ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(prog="bot.engine.main", description="Ohana 引擎")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/bot.local.yaml）")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    conn = db.connect(config.database)
    db.migrate(conn)
    store = MemoryStore(conn)
    logger.info("数据库就绪：%s", config.database)

    client = OneBotClient(config.onebot.api_base, config.onebot.resolve_access_token())
    login_nickname = "Ohana"
    try:
        info = client.get_login_info()
        login_id = str(info.get("user_id") or "")
        login_nickname = str(info.get("nickname") or login_nickname)
        if config.onebot.expected_self_id and login_id and login_id != config.onebot.expected_self_id:
            logger.warning(
                "OneBot 登录账号 %s 与配置 %s 不一致，请核对后再正式运行",
                login_id,
                config.onebot.expected_self_id,
            )
        else:
            logger.info("OneBot 在线：%s（%s）", login_id, info.get("nickname", ""))
    except RuntimeError as exc:
        logger.warning("暂未连接 OneBot API（%s）；事件接收仍会启动", exc)

    if config.onebot.expected_self_id is None:
        logger.warning("未配置 expected_self_id，身份校验与 @ 判定将不可用")
    secret = config.onebot.resolve_event_secret()
    if not secret:
        logger.warning("未配置事件密钥（env %s），跳过签名校验，仅限本地调试", config.onebot.event_secret_env)
    if not config.onebot.allowed_groups:
        logger.warning("allowed_groups 为空：所有群消息都会被忽略（请先填测试群号）")

    lock = threading.RLock()

    llm = DeepSeekClient(
        api_key=config.llm.resolve_api_key() or None,
        model=config.llm.model,
        base_url=config.llm.base_url,
        max_tokens=config.llm.max_tokens,
        reasoning_effort=config.llm.reasoning_effort,
        retries=config.llm.retries,
    )
    if not llm.api_key:
        logger.warning(
            "未配置 LLM Key（生成会失败）：在 config/bot.local.yaml 的 llm.api_key 填写，或设置环境变量 %s",
            config.llm.api_key_env,
        )

    def send_group(conversation_id: str, text: str) -> dict:
        group_id = conversation_id.split(":", 1)[1]
        return client.send_group_msg(group_id, text)

    runtime = EngineRuntime(
        config,
        store,
        generate=llm.generate,
        send_group=send_group,
        lock=lock,
        self_name=login_nickname,
    )
    ingestor = MessageIngestor(config, store, on_message=runtime.handle, lock=lock)
    runtime.start()

    def health_check() -> str:
        # 用独立的短连接探测，避免与事件线程共享主连接
        probe = sqlite3.connect(config.database)
        try:
            probe.execute("SELECT 1").fetchone()
        finally:
            probe.close()
        return "ok"

    server = EventServer(
        config.onebot.listen_host,
        config.onebot.listen_port,
        config.onebot.event_path,
        ingestor.handle,
        secret=secret,
        health_check=health_check,
    )
    logger.info(
        "监听 http://%s:%s%s（mode=%s，send_enabled=%s）",
        config.onebot.listen_host,
        server.port,
        config.onebot.event_path,
        config.mode,
        config.send_enabled,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
