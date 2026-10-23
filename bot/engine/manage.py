"""本机管理命令：记忆系统初始化、快照导出与更新任务。

用法（在仓库根目录执行）：
    python -m bot.engine.manage init     # 建库、建目录、建 bot/ 链接（可重复执行）
    python -m bot.engine.manage export   # 导出全部 Markdown 快照（内容有变化才写）
    python -m bot.engine.manage update   # 对攒满一批消息的会话执行记忆更新（需 DEEPSEEK_API_KEY）
    python -m bot.engine.manage refine   # 离线重写人物画像：融合成长期稳定版（需 DEEPSEEK_API_KEY）
    python -m bot.engine.manage harness-login  # 登录 DeepSeek 网页版并保存登录态（harness.backend=web 用）
    python -m bot.engine.manage knowledge rebuild  # 扫描 runtime/harness/ 资料重建知识库（自动分类）

设计依据 analysis/proposals/memory_plan_v0.2.md §5/§6：
数据在 runtime/memory/，bot/memory/conversations/ 下留链接指向对应位置。
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from dataclasses import replace
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

    p_refine = sub.add_parser(
        "refine", parents=[common], help="离线重写人物画像：融合成长期稳定版（需 DEEPSEEK_API_KEY）"
    )
    p_refine.add_argument("--conversation", default="", help="只处理指定会话，如 qq:group:123456")
    p_refine.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    p_refine.add_argument("--api-key", default="", help="API Key（默认读配置或环境变量）")
    p_refine.add_argument("--dry-run", action="store_true", help="只调用模型预览，不写库")

    p_login = sub.add_parser(
        "harness-login", help="登录 DeepSeek 网页版并保存登录态（harness.backend=web 用）"
    )
    p_login.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_login.add_argument("--profile-dir", default="", help="登录态目录（默认取配置 harness.web.profile_dir）")
    p_login.add_argument("--url", default="", help="网页版地址（默认取配置 harness.web.chat_url）")
    p_login.add_argument("--timeout", type=float, default=300.0, help="等待登录的最长秒数（默认 300）")
    p_login.add_argument("--reset", action="store_true", help="换账号：先备份现有登录态（改名）再重新登录")

    p_knowledge = sub.add_parser("knowledge", help="知识库：rebuild 重建入库 / stats 统计")
    kn_sub = p_knowledge.add_subparsers(dest="knowledge_command", required=True)
    p_rebuild = kn_sub.add_parser("rebuild", help="扫描目录内 Markdown 资料重新入库（自动分类）")
    p_rebuild.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_rebuild.add_argument("--workspace", default="runtime/harness", help="资料目录（默认 runtime/harness）")
    p_rebuild.add_argument("--api-key", default="", help="API Key（默认读环境变量 DEEPSEEK_API_KEY）")
    p_rebuild.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    p_rebuild.add_argument("--no-classify", action="store_true", help="跳过分类，全部按知识入库")
    p_rebuild.add_argument("--backend", choices=("", "api", "web"), default="", help="审核后端（默认取配置 knowledge.classify_backend）")
    p_rebuild.add_argument("--limit", type=int, default=0, help="只处理前 N 份（0 = 全部）")
    p_rebuild.add_argument("--dry-run", action="store_true", help="只预览分类结果，不写库")
    p_stats = kn_sub.add_parser("stats", help="知识库统计与最近入库列表")
    p_stats.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_consolidate = kn_sub.add_parser("consolidate", help="批量生成实体档案与主题综述（整合层；需 DEEPSEEK_API_KEY）")
    p_consolidate.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_consolidate.add_argument("--scope", choices=("all", "entity", "cluster"), default="all", help="只做实体档案或主题综述（默认 all）")
    p_consolidate.add_argument("--min-chunks", type=int, default=2, help="实体档案至少关联的文本块数（默认 2）")
    p_consolidate.add_argument("--limit", type=int, default=50, help="本次最多生成的档案/综述数（默认 50）")
    p_consolidate.add_argument("--max-members", type=int, default=30, help="超过该成员数的板块不生成综述（默认 30）")
    p_consolidate.add_argument("--api-key", default="", help="API Key（默认读配置或环境变量）")
    p_consolidate.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    p_consolidate.add_argument("--backend", choices=("", "api", "web"), default="", help="生成后端（默认取配置 knowledge.consolidate_backend）")
    p_consolidate.add_argument("--think", action="store_true", help="网页版后端开启深度思考（默认关闭，批量更快）")
    p_consolidate.add_argument("--force", action="store_true", help="忽略来源签名，强制重新生成")
    p_consolidate.add_argument("--dry-run", action="store_true", help="只预览将生成哪些档案/综述，不写库")
    p_export = kn_sub.add_parser("export", help="导出整合结果 Markdown 快照（默认 runtime/knowledge/）")
    p_export.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_export.add_argument("--out", default="runtime/knowledge", help="输出目录（相对仓库根；默认 runtime/knowledge）")
    p_query = kn_sub.add_parser("query", help="试检索：预览某段文本会注入哪些知识摘录")
    p_query.add_argument("text", help="查询文本（把群里那句话原样放进来）")
    p_query.add_argument("--config", default="", help="配置文件路径（默认 config/bot.local.yaml）")
    p_query.add_argument("--limit", type=int, default=0, help="覆盖 top_k（0 = 用配置值）")

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(Path(args.db), Path(args.memory_root))
    if args.command == "update":
        return cmd_update(args, Path(args.db), Path(args.memory_root))
    if args.command == "refine":
        return cmd_refine(args, Path(args.db), Path(args.memory_root))
    if args.command == "harness-login":
        return cmd_harness_login(args)
    if args.command == "knowledge":
        return cmd_knowledge(args)
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
    engine_config = _load_engine_config()
    self_id = str(engine_config.onebot.expected_self_id or "") if engine_config else ""
    updated = snapshot.export_all(store, memory_root, exclude_user_ids=(self_id,) if self_id else ())
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


def cmd_knowledge(args) -> int:
    """知识库管理：rebuild 重建入库 / stats 统计。"""
    from bot.engine.config import load_config
    from bot.engine.knowledge import KnowledgeBase

    try:
        config = load_config(Path(args.config) if args.config else None)
    except FileNotFoundError as exc:
        print(exc)
        return 2
    conn = db.connect(config.database)
    db.migrate(conn)
    base = KnowledgeBase(conn, config.knowledge)

    if args.knowledge_command == "stats":
        info = base.stats()
        print(
            f"知识库：{info['docs']} 份文档、{info['chunks']} 个文本块、{info['entities']} 个实体、"
            f"{info['edges']} 条关系、{info['summaries']} 份档案/综述（共 {info['chars']} 字符）"
        )
        rows = conn.execute(
            "SELECT title, created_at FROM knowledge_docs ORDER BY doc_id DESC LIMIT 10"
        ).fetchall()
        for row in rows:
            print(f"  [{row['created_at'][:10]}] {row['title']}")
        return 0

    if args.knowledge_command == "consolidate":
        return cmd_knowledge_consolidate(args, base, config)
    if args.knowledge_command == "export":
        return cmd_knowledge_export(args, base)
    if args.knowledge_command == "query":
        return cmd_knowledge_query(args, base)

    return cmd_knowledge_rebuild(args, base, config)


def cmd_knowledge_rebuild(args, base, config) -> int:
    """扫描目录中的 Markdown 资料逐份分类入库（dry-run 只预览分类结果）。"""
    from bot.engine.knowledge import build_classify_prompt, parse_classification

    workspace = Path(args.workspace)
    if not workspace.is_absolute():
        workspace = REPO_ROOT / workspace
    files = sorted(workspace.glob("*.md"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"目录下没有 Markdown 资料：{workspace}")
        return 0

    classify = None
    if not args.no_classify:
        classify, _backend = _backend_generate(args, config, config.knowledge.classify_backend)
        if classify is None:
            print("或用 --no-classify 跳过分类、全部按知识入库。")
            return 2
    print(f"扫描 {len(files)} 份资料：{workspace}")

    counts = {"ingested": 0, "skipped": 0, "duplicate": 0, "failed": 0}
    for path in files:
        text = path.read_text(encoding="utf-8")
        title = _extract_title(text) or path.stem
        if args.dry_run:
            if classify is None:
                print(f"  {path.name} → knowledge（dry-run 未写入）")
                continue
            try:
                meta = parse_classification(classify(build_classify_prompt(title, text)))
                print(f"  {path.name} → {meta['kind']}（实体 {len(meta['entities'])} 个；dry-run 未写入）")
            except Exception as exc:
                print(f"  {path.name} → 分类失败：{exc}")
            continue
        outcome = base.ingest_document(
            title=title,
            source=str(path),
            content=text,
            classify=classify,
            force_kind="knowledge" if args.no_classify else None,
        )
        status = outcome.get("status", "failed")
        counts[status] = counts.get(status, 0) + 1
        if status == "ingested":
            print(f"  {path.name} → 入库（{outcome.get('title')}，{outcome.get('chunks')} 块，{outcome.get('entities')} 实体）")
        elif status == "skipped":
            print(f"  {path.name} → 跳过（新闻类）")
        elif status == "duplicate":
            print(f"  {path.name} → 跳过（已有同内容）")
        else:
            reason = outcome.get("reason", "")
            print(f"  {path.name} → 失败（{status}{'：' + reason if reason else ''}）")
    if args.dry_run:
        print("预览完成（未写入知识库）。")
        return 0
    print(
        f"完成：入库 {counts.get('ingested', 0)}、跳过 {counts.get('skipped', 0) + counts.get('duplicate', 0)}、失败 {counts.get('failed', 0)}"
    )
    return 0


def _extract_title(text: str) -> str:
    """取第一行一级标题作资料标题；没有则返回空串。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:80]
    return ""


def cmd_knowledge_consolidate(args, base, config) -> int:
    """批量生成实体档案与主题综述（整合层）；来源未变化自动跳过。"""
    generate, backend = _backend_generate(
        args, config, config.knowledge.consolidate_backend, think=args.think
    )
    if generate is None:
        return 2
    print(
        f"整合范围：{args.scope}（min_chunks={args.min_chunks}，limit={args.limit}，"
        f"max_members={args.max_members}，backend={backend}"
        f"{'，force' if args.force else ''}{'，dry-run' if args.dry_run else ''}）"
    )
    report = base.consolidate(
        generate,
        scope=args.scope,
        min_chunks=args.min_chunks,
        limit=args.limit,
        max_members=args.max_members,
        force=args.force,
        dry_run=args.dry_run,
        on_step=print,
    )
    print(
        "完成：档案 生成 {entity_done} / 跳过 {entity_skipped} / 失败 {entity_failed}；"
        "综述 生成 {cluster_done} / 跳过 {cluster_skipped} / 失败 {cluster_failed}".format(**report)
    )
    return 0


def _backend_generate(args, config, configured_backend: str, think: bool = False):
    """按后端构造生成器；web 不可用时自动回退 API。返回 (generate, 实际后端)，构造失败为 (None, backend)。"""
    backend = (getattr(args, "backend", "") or configured_backend or "api").strip().lower()
    if backend == "web":
        from bot.tools import web_harness

        options = replace(
            config.harness.web,
            deep_think=think,
            search=False,
            new_chat=True,
            save_documents=False,
        )
        if not web_harness._playwright_available():
            print("网页版不可用（未安装 playwright），回退到 API 模型。")
        elif not web_harness.check_login(options.profile_dir, options.chat_url):
            print(f"网页版未登录或登录态过期（{web_harness.LOGIN_HINT}），回退到 API 模型。")
        else:
            print("网页版后端：每份资料开新对话；请与引擎任务错峰运行（共用浏览器登录态）。")
            driver = web_harness.PlaywrightWebChat(options)
            timeout = config.harness.timeout_seconds

            def generate(prompt: str) -> str:
                return driver.ask(prompt, timeout=timeout)

            return generate, "web"
        backend = "api"
    client = DeepSeekClient(
        api_key=args.api_key or config.llm.resolve_api_key() or None,
        model=args.model,
        retries=config.llm.retries,
    )
    if not client.api_key:
        print("未配置 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，或用 --api-key 传入。")
        return None, backend
    return client.generate, "api"


def cmd_knowledge_query(args, base) -> int:
    """用给定文本试检索，打印将注入的知识摘录（离线验证用）。"""
    excerpts = base.search(args.text, limit=args.limit or None)
    if not excerpts:
        print("无命中：该话题不会注入知识（聊天侧同样如此）。")
        return 0
    total = 0
    for item in excerpts:
        total += len(item["text"])
        print(f"[{item['score']}] {item['title']}（{item['date']}）")
        print(f"    {item['text'][:150]}")
    print(f"共 {len(excerpts)} 块、{total} 字（聊天侧同样走这条检索；注入预算 {base.max_chars} 字）")
    return 0


def cmd_knowledge_export(args, base) -> int:
    """导出实体档案与主题综述为 Markdown 快照（内容有变化才写）。"""
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    updated = base.export_snapshots(out)
    if updated:
        print(f"导出完成：{len(updated)} 个文件有更新")
        for path in updated:
            print(f"  {path}")
    elif base.stats().get("summaries"):
        print(f"导出完成：内容无变化（{out}）")
    else:
        print("知识库还没有档案/综述：先运行 python -m bot.engine.manage knowledge consolidate")
    return 0


def _load_engine_config():
    """尽力加载引擎配置（缺失时返回 None）：用于 API Key 解析与自身 QQ 号排除。"""
    from bot.engine.config import load_config

    try:
        return load_config()
    except FileNotFoundError:
        return None


def cmd_update(args, db_path: Path, memory_root: Path) -> int:
    conn = db.connect(db_path)
    db.migrate(conn)
    store = MemoryStore(conn)

    engine_config = _load_engine_config()
    self_id = str(engine_config.onebot.expected_self_id or "") if engine_config else ""

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

    client = DeepSeekClient(
        api_key=args.api_key
        or (engine_config.llm.resolve_api_key() if engine_config else os.environ.get("DEEPSEEK_API_KEY", "")),
        model=args.model,
        retries=(engine_config.llm.retries if engine_config else 2),
    )
    if not client.api_key:
        print("未配置 DeepSeek API Key：请在配置 llm.api_key 填写，或设置环境变量 DEEPSEEK_API_KEY，或用 --api-key 传入。")
        return 2

    config = UpdateConfig(
        batch_size=args.batch_size,
        recent_logs=args.recent_logs,
        exclude_user_ids=(self_id,) if self_id else (),
    )
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
        updated = snapshot.export_all(store, memory_root, exclude_user_ids=(self_id,) if self_id else ())
        print(f"快照导出：{len(updated)} 个文件有更新")
    return 0


def cmd_refine(args, db_path: Path, memory_root: Path) -> int:
    """离线重写人物画像：把已有 basic 融合成长期稳定版（不依赖新消息）。"""
    from bot.engine.growth import refine as growth_refine

    conn = db.connect(db_path)
    db.migrate(conn)
    store = MemoryStore(conn)

    engine_config = _load_engine_config()
    self_id = str(engine_config.onebot.expected_self_id or "") if engine_config else ""

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

    client = DeepSeekClient(
        api_key=args.api_key
        or (engine_config.llm.resolve_api_key() if engine_config else os.environ.get("DEEPSEEK_API_KEY", "")),
        model=args.model,
        retries=(engine_config.llm.retries if engine_config else 2),
    )
    if not client.api_key:
        print("未配置 DeepSeek API Key：请在配置 llm.api_key 填写，或设置环境变量 DEEPSEEK_API_KEY，或用 --api-key 传入。")
        return 2

    total = {"refined": 0, "skipped": 0, "failed": 0, "entries_added": 0}
    for conv in conversations:
        scope = f"{conv['platform']}:{conv['conversation_id']}"
        print(f"{scope}：")
        report = growth_refine.refine_profiles(
            store,
            client.generate,
            conv["platform"],
            conv["conversation_id"],
            exclude_user_ids=(self_id,) if self_id else (),
            apply=not args.dry_run,
            on_step=print,
        )
        for key in total:
            total[key] += report[key]

    print(
        f"完成：重写 {total['refined']} 人、跳过 {total['skipped']}、失败 {total['failed']}、"
        f"新增条目 {total['entries_added']}"
    )
    if not args.dry_run:
        updated = snapshot.export_all(store, memory_root, exclude_user_ids=(self_id,) if self_id else ())
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
