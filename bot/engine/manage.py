"""本机管理命令：记忆系统初始化、快照导出与更新任务。

用法（在仓库根目录执行）：
    python -m bot.engine.manage init     # 建库、建目录、建 bot/ 链接（可重复执行）
    python -m bot.engine.manage export   # 导出全部 Markdown 快照（内容有变化才写）
    python -m bot.engine.manage update   # 对攒满一批消息的会话执行记忆更新（需 DEEPSEEK_API_KEY）
    python -m bot.engine.manage harness-login  # 登录 DeepSeek 网页版并保存登录态（harness.backend=web 用）

设计依据 analysis/proposals/memory_plan_v0.2.md §5/§6：
数据在 runtime/memory/，bot/memory/conversations/ 下留链接指向对应位置。
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

from bot.engine.growth import snapshot
from bot.engine.growth.update import UpdateConfig, run_update
from bot.engine.llm import DEFAULT_MODEL, DeepSeekClient
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
CONVERSATIONS_DIR = REPO_ROOT / "bot" / "memory" / "conversations"

# 需要清理的旧空目录（拼写错误 / 已被链接取代）
LEGACY_DIRS = ("persons", "dayily")
# 链接：conversations 下的名字 → runtime/memory 下的目标
LINKS = (("persons", "persons"), ("logs", "logs"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.engine.manage", description="记忆系统管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(REPO_ROOT / "runtime" / "ohana.sqlite3"), help="SQLite 数据库路径")
    common.add_argument(
        "--memory-root", default=str(REPO_ROOT / "runtime" / "memory"), help="快照根目录"
    )
    sub.add_parser("init", parents=[common], help="初始化数据库、目录与链接")
    sub.add_parser("export", parents=[common], help="导出全部 Markdown 快照")

    p_update = sub.add_parser(
        "update", parents=[common], help="对攒满一批消息的会话执行记忆更新（需 DEEPSEEK_API_KEY）"
    )
    p_update.add_argument("--conversation", default="", help="只处理指定会话，如 qq:group:123456")
    p_update.add_argument("--batch-size", type=int, default=200, help="每批消息数（满即触发，默认 200）")
    p_update.add_argument("--recent-logs", type=int, default=3, help="上下文携带的最近日常记录段数（默认 3）")
    p_update.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    p_update.add_argument("--api-key", default="", help="API Key（默认读环境变量 DEEPSEEK_API_KEY）")
    p_update.add_argument("--dry-run", action="store_true", help="只调用模型预览，不写库")

    p_login = sub.add_parser(
        "harness-login", help="登录 DeepSeek 网页版并保存登录态（harness.backend=web 用）"
    )
    p_login.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_login.add_argument("--profile-dir", default="", help="登录态目录（默认取配置 harness.web.profile_dir）")
    p_login.add_argument("--url", default="", help="网页版地址（默认取配置 harness.web.chat_url）")
    p_login.add_argument("--timeout", type=float, default=300.0, help="等待登录的最长秒数（默认 300）")
    p_login.add_argument("--reset", action="store_true", help="换账号：先备份现有登录态（改名）再重新登录")

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(Path(args.db), Path(args.memory_root))
    if args.command == "update":
        return cmd_update(args, Path(args.db), Path(args.memory_root))
    if args.command == "harness-login":
        return cmd_harness_login(args)
    return cmd_export(Path(args.db), Path(args.memory_root))


def cmd_init(db_path: Path, memory_root: Path) -> int:
    conn = db.connect(db_path)
    db.migrate(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"数据库：{db_path}（schema v{version}）")

    (memory_root / "persons").mkdir(parents=True, exist_ok=True)
    (memory_root / "logs").mkdir(parents=True, exist_ok=True)
    print(f"数据目录：{memory_root}（persons / logs）")

    for name in LEGACY_DIRS:
        path = CONVERSATIONS_DIR / name
        if not path.is_dir() or _is_link_like(path):
            continue
        try:
            if any(path.iterdir()):
                print(f"跳过旧目录 {path}（非空，未自动删除）")
                continue
            path.rmdir()
            print(f"已移除旧空目录：{path}")
        except OSError as exc:
            print(f"跳过旧目录 {path}（{exc}）")

    for link_name, target_name in LINKS:
        link = CONVERSATIONS_DIR / link_name
        target = memory_root / target_name
        result = _ensure_dir_link(link, target)
        print(f"链接：{link} → {target}（{result}）")
    return 0


def cmd_export(db_path: Path, memory_root: Path) -> int:
    conn = db.connect(db_path)
    db.migrate(conn)
    store = MemoryStore(conn)
    updated = snapshot.export_all(store, memory_root)
    print(f"导出完成：{len(updated)} 个文件有更新")
    for path in updated:
        print(f"  {path}")
    return 0


def cmd_harness_login(args) -> int:
    """有头打开 DeepSeek 网页版完成登录；登录态保存在 harness.web.profile_dir。"""
    from bot.engine.config import load_config
    from bot.tools import web_harness

    try:
        config = load_config(Path(args.config) if args.config else None)
    except FileNotFoundError as exc:
        print(exc)
        return 2
    profile_dir = args.profile_dir or config.harness.web.profile_dir
    chat_url = args.url or config.harness.web.chat_url
    return web_harness.login(profile_dir, chat_url, timeout=args.timeout, reset=args.reset)


def cmd_update(args, db_path: Path, memory_root: Path) -> int:
    conn = db.connect(db_path)
    db.migrate(conn)
    store = MemoryStore(conn)

    conversations = store.list_conversations()
    if args.conversation:
        if ":" not in args.conversation:
            print(f"会话格式应为 平台:类型:ID（如 qq:group:123456）：{args.conversation}")
            return 2
        platform, conversation_id = args.conversation.split(":", 1)
        conversations = [
            c
            for c in conversations
            if c["platform"] == platform and c["conversation_id"] == conversation_id
        ]
        if not conversations:
            print(f"未找到会话：{args.conversation}")
            return 2

    client = DeepSeekClient(api_key=args.api_key or None, model=args.model)
    if not client.api_key:
        print("未配置 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，或用 --api-key 传入。")
        return 2

    config = UpdateConfig(batch_size=args.batch_size, recent_logs=args.recent_logs)
    batch_count = 0
    for conv in conversations:
        scope = f"{conv['platform']}:{conv['conversation_id']}"
        while True:
            try:
                result = run_update(
                    store,
                    client.generate,
                    conv["platform"],
                    conv["conversation_id"],
                    config,
                    apply=not args.dry_run,
                )
            except (RuntimeError, ValueError, KeyError) as exc:
                print(f"更新失败（{scope}）：{exc}")
                return 1
            if result is None:
                break
            batch_count += 1
            if args.dry_run:
                preview = result.log_text.replace("\n", " ")[:100]
                print(
                    f"{scope}：msg-{result.msg_from} ~ msg-{result.msg_to} → "
                    f"画像 {result.profiles_updated} 人（dry-run 未写入）"
                )
                print(f"  log 预览：{preview}")
                break
            print(
                f"{scope}：msg-{result.msg_from} ~ msg-{result.msg_to} → "
                f"画像 {result.profiles_updated} 人、新条目 {result.entries_added}、"
                f"新绰号 {result.aliases_added}、记录第 {result.log_seq} 段"
            )

    if batch_count == 0:
        print(f"没有攒满 {args.batch_size} 条的会话，未触发更新")
        return 0
    if not args.dry_run:
        updated = snapshot.export_all(store, memory_root)
        print(f"快照导出：{len(updated)} 个文件有更新")
    return 0


def _is_link_like(path: Path) -> bool:
    """symlink 或 Windows junction；两者都不应当作旧目录清理。"""
    if path.is_symlink():
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(flag and attributes & flag)


def _ensure_dir_link(link: Path, target: Path) -> str:
    """确保目录链接存在；返回 已存在 / symlink / junction / pointer（说明文件）。"""
    if link.is_symlink() or link.exists():
        return "已存在"
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            return "junction"
    note = link.with_name(f"{link.name}.说明.txt")
    note.write_text(f"链接创建失败；实际数据位于：{target}\n", encoding="utf-8")
    return f"pointer（{note.name}）"


if __name__ == "__main__":
    sys.exit(main())
