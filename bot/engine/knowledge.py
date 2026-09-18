"""知识库（GraphRAG-lite）：调研产物自动入库 + 词法召回 + 实体图扩展注入。

设计（见 docs/engine-spec.md §6）：
- 入库：分类调用（复用引擎的 generate）判定 知识/新闻，并顺带抽取实体与关系；
  仅知识入库；内容哈希去重；文档 → 文本块（预分词）→ 实体（多对多）→ 关系边。
- 检索：jieba 分词 + BM25 召回；查询命中实体时沿关系图扩展 1 跳，
  对直连块与邻居块加权；热路径零 LLM 调用，毫秒级。
- 存储：SQLite（knowledge_docs / knowledge_chunks / knowledge_entities /
  knowledge_edges / knowledge_chunk_entities）；索引在进程内缓存，
  通过轻量签名查询感知外部写入（manage 命令）自动重建。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

from bot.engine.storage.db import now_iso, transaction

logger = logging.getLogger(__name__)

# BM25 参数与图扩展权重（常量；调整需同步测试）
K1 = 1.5
B = 0.75
_DIRECT_BOOST = 3.0
_NEIGHBOR_BOOST = 1.5

CHUNK_TARGET = 350  # 每块目标字符数
CHUNK_MAX = 500  # 单块硬上限
CHUNK_LIMIT = 120  # 单文档分块上限
ENTITY_LIMIT = 12
RELATION_LIMIT = 10
CLASSIFY_MAX_CHARS = 6000

_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_+.#-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

# 高频虚词（jieba 已去掉单字，这里只兜底常见双字虚词）
_STOPWORDS = {
    "我们", "你们", "他们", "她们", "什么", "这个", "那个", "就是", "可以", "不是",
    "没有", "一个", "这样", "那样", "但是", "因为", "所以", "如果", "还是", "已经",
    "自己", "一下", "这些", "那些", "真的", "好像", "不过", "然后", "而且", "以及",
    "关于", "为了", "通过", "根据", "目前", "现在", "主要", "进行", "需要", "表示",
}

JIEBA_READY = True  # 进程级探测结果（jieba 缺失时退化为字符双字切分）

CLASSIFY_PROMPT = """你是知识库入库审核员。阅读下面的资料，完成两件事：
1. 判定资料类型：
   - "knowledge"：长期有效的知识（原理、方法、作品与人物评价、历史事实、技术资料等）；
   - "news"：时效信息（发布/上线/日程、活动、融资、价格、赛程等会随时间失效的内容）。
2. 若是 knowledge：抽取关键实体与实体间关系。实体要具体（作品、人物、组织、技术、产品），
   不要抽泛词（如“电影”“公司”“AI”“行业”）。
只输出一个 json 对象，不要解释、不要多余文字：
{"kind": "knowledge 或 news", "title": "不超过 30 字的标题", "entities": ["实体1", "实体2"], "relations": [["实体A", "实体B", "关系短语"]]}
限制：entities 最多 12 个；relations 最多 10 条，且两端都必须在 entities 里。
资料（可能被截断）：
{doc}"""


# ---------- 文本处理 ----------

def _tokenize(text: str) -> list[str]:
    """分词：ASCII 词 + 中文词（jieba 优先，缺失退化为字符双字切分）。"""
    lowered = (text or "").lower()
    tokens: list[str] = []
    tokens += [w for w in _ASCII_RE.findall(lowered) if len(w) >= 2 or w.isdigit()]
    for run in _CJK_RE.findall(lowered):
        tokens += _cjk_tokens(run)
    return tokens


def _cjk_tokens(run: str) -> list[str]:
    words = _cut_cjk(run)
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def _cut_cjk(run: str) -> list[str]:
    global JIEBA_READY
    if JIEBA_READY:
        try:
            import jieba  # noqa: PLC0415 —— 延迟导入：未安装时退化，不影响引擎启动

            return [w for w in jieba.cut_for_search(run) if w.strip()]
        except Exception:
            JIEBA_READY = False
            logger.warning("jieba 不可用，知识库分词退化为字符双字切分")
    if len(run) <= 1:
        return [run] if run else []
    return [run[i : i + 2] for i in range(len(run) - 1)]


def _chunk_markdown(text: str) -> list[str]:
    """按空行分段聚合为 ~CHUNK_TARGET 字的块；超长段硬切。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text or "") if b.strip()]
    chunks: list[str] = []
    buffer = ""
    for block in blocks:
        if len(block) > CHUNK_MAX:
            while block:
                piece, block = block[:CHUNK_MAX], block[CHUNK_MAX:]
                piece = piece.rstrip()
                if piece:
                    if buffer:
                        chunks.append(buffer)
                        buffer = ""
                    chunks.append(piece)
            continue
        if buffer and len(buffer) + len(block) + 1 > CHUNK_TARGET:
            chunks.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n{block}" if buffer else block
        if len(chunks) >= CHUNK_LIMIT:
            break
    if buffer and len(chunks) < CHUNK_LIMIT:
        chunks.append(buffer)
    return chunks[:CHUNK_LIMIT]


def _entity_key(name: str) -> str:
    return re.sub(r"\s+", "", name or "").lower()


def _digest_of(values: list) -> str:
    """来源签名（块 id / 成员列表）：未变化则跳过重生成。"""
    raw = ",".join(str(v) for v in values).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _compact(text: str) -> str:
    """注入用压缩：折叠空白为单空格。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def _parse_json_object(content: str, label: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍代码围栏与前后说明文字，兼容网页版输出）。"""
    text = _FENCE_RE.sub("", (content or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    candidates = [text]
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data
        last_error = ValueError("输出不是 json 对象")
    raise ValueError(f"{label}输出不是合法 json：{last_error}")


def parse_classification(content: str) -> dict:
    """解析入库审核输出（容忍代码围栏与前后说明文字），返回 {kind,title,entities,relations}。"""
    data = _parse_json_object(content, "入库审核")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("knowledge", "news"):
        raise ValueError(f"未知资料类型：{kind!r}")
    entities = [e.strip() for e in (data.get("entities") or []) if isinstance(e, str) and e.strip()]
    relations = [
        [str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip() if len(r) > 2 else ""]
        for r in (data.get("relations") or [])
        if isinstance(r, list) and len(r) >= 2
    ]
    return {
        "kind": kind,
        "title": str(data.get("title") or "").strip()[:60],
        "entities": entities[:ENTITY_LIMIT],
        "relations": relations[:RELATION_LIMIT],
    }


def build_classify_prompt(title: str, content: str) -> str:
    body = content.strip()[:CLASSIFY_MAX_CHARS]
    return CLASSIFY_PROMPT.replace("{doc}", f"标题：{title}\n\n{body}")


ENTITY_SUMMARY_PROMPT = """你在为知识库整理「实体档案」。实体：{name}
下面是知识库中与该实体相关的片段（可能来自多份资料，允许有重复或矛盾）：
{excerpts}

请只输出一个 json 对象，不要解释：
{"summary": "3~5 句综合概述：合并重复信息、保留关键事实；相互矛盾之处保留并写“存在分歧”", "facts": ["要点1", "要点2"]}
要求：只依据给定片段；facts 最多 8 条、每条不超 40 字。"""

CLUSTER_SUMMARY_PROMPT = """你在为知识库整理「主题综述」。以下是同一主题板块下的实体与相关片段：
实体：{members}
片段：
{excerpts}

请只输出一个 json 对象，不要解释：
{"title": "板块名（不超 12 字）", "summary": "3~5 句综述：这个板块整体在讲什么、共识与分歧各是什么", "facts": ["要点1"]}
要求：只依据给定片段；facts 最多 6 条。"""

SUMMARY_SOURCE_MAX_CHARS = 3600


def build_entity_summary_prompt(name: str, excerpts: list[str]) -> str:
    return ENTITY_SUMMARY_PROMPT.replace("{name}", name).replace("{excerpts}", _join_excerpts(excerpts))


def build_cluster_summary_prompt(members: list[str], excerpts: list[str]) -> str:
    return (
        CLUSTER_SUMMARY_PROMPT.replace("{members}", "、".join(members))
        .replace("{excerpts}", _join_excerpts(excerpts))
    )


def _join_excerpts(excerpts: list[str]) -> str:
    joined = ""
    for text in excerpts:
        piece = _compact(text)
        if not piece:
            continue
        joined = f"{joined}\n- {piece}" if joined else f"- {piece}"
        if len(joined) >= SUMMARY_SOURCE_MAX_CHARS:
            break
    return joined[:SUMMARY_SOURCE_MAX_CHARS] or "（暂无片段）"


def parse_summary(content: str, with_title: bool = False) -> dict:
    """解析档案/综述输出（容忍代码围栏与前后说明文字），返回 {summary, facts[, title]}。"""
    data = _parse_json_object(content, "摘要")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise ValueError("摘要输出缺少 summary")
    facts = [str(item).strip() for item in (data.get("facts") or []) if str(item).strip()]
    parsed = {"summary": summary, "facts": facts[:8]}
    if with_title:
        parsed["title"] = str(data.get("title") or "").strip()[:20]
    return parsed


def render_summary_text(parsed: dict) -> str:
    """档案/综述的注入文本：概述 + 要点列表。"""
    bullets = "\n".join(f"- {item}" for item in parsed.get("facts") or [])
    return parsed["summary"] + (f"\n{bullets}" if bullets else "")


# ---------- 知识库 ----------

class KnowledgeBase:
    """知识库读写与检索（线程安全：所有数据库访问在 lock 下串行）。"""

    def __init__(self, conn: sqlite3.Connection, config=None, lock: threading.RLock | None = None):
        self.conn = conn
        self.config = config
        self.lock = lock or threading.RLock()
        self._index: "_Index | None" = None

    # ---------- 配置读取（兼容缺省） ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @property
    def auto_ingest(self) -> bool:
        return bool(getattr(self.config, "auto_ingest", True))

    @property
    def top_k(self) -> int:
        return max(int(getattr(self.config, "top_k", 2) or 2), 1)

    @property
    def max_chars(self) -> int:
        return max(int(getattr(self.config, "max_chars", 600) or 600), 100)

    @property
    def min_matches(self) -> int:
        value = getattr(self.config, "min_matches", 2)
        return max(int(2 if value is None else value), 0)

    @property
    def graph_hops(self) -> int:
        value = getattr(self.config, "graph_hops", 1)
        return max(int(1 if value is None else value), 0)

    @property
    def inject_summaries(self) -> bool:
        return bool(getattr(self.config, "inject_summaries", True))

    @property
    def classify_backend(self) -> str:
        """入库审核后端：web = 网页版（零 API token）；api = DeepSeek API。"""
        return str(getattr(self.config, "classify_backend", "api") or "api").strip().lower()

    # ---------- 入库 ----------

    def ingest_document(
        self,
        *,
        title: str,
        source: str = "",
        content: str,
        created_at: str | None = None,
        classify=None,
        force_kind: str | None = None,
    ) -> dict:
        """入库一份资料；返回 {status: ingested|duplicate|skipped|failed|empty, ...}。

        classify 为可选的可调用对象（prompt -> 模型输出）：用于 知识/新闻 分类与实体抽取；
        未提供或 force_kind 给定则直接按 force_kind（默认 knowledge）入库。
        """
        content = (content or "").strip()
        if not content:
            return {"status": "empty"}
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.lock:
            row = self.conn.execute(
                "SELECT doc_id FROM knowledge_docs WHERE content_hash = ?", (digest,)
            ).fetchone()
        if row:
            return {"status": "duplicate", "doc_id": int(row["doc_id"])}

        meta: dict = {}
        kind = force_kind or "knowledge"
        if force_kind is None and classify is not None:
            last_error: Exception | None = None
            for attempt in (1, 2):  # 模型偶发坏 JSON：重试一次再放弃
                try:
                    meta = parse_classification(classify(build_classify_prompt(title, content)))
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning("知识入库审核失败（第 %d 次）：%s", attempt, exc)
            if last_error is not None:
                logger.warning("知识入库审核两次均失败（按不入库处理）")
                return {"status": "failed", "reason": str(last_error)}
            kind = meta["kind"]
        if kind != "knowledge":
            return {"status": "skipped", "kind": kind}

        final_title = (meta.get("title") or title or "未命名资料").strip()[:80]
        stored_at = created_at or now_iso()
        chunks = _chunk_markdown(content)
        if not chunks:
            return {"status": "empty"}
        entities = meta.get("entities") or []
        relations = meta.get("relations") or []

        try:
            with self.lock, transaction(self.conn) as c:
                cursor = c.execute(
                    "INSERT INTO knowledge_docs (title, source, kind, content, content_hash, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (final_title, (source or "")[:400], kind, content, digest, stored_at),
                )
                doc_id = int(cursor.lastrowid)
                chunk_ids: list[int] = []
                for seq, text in enumerate(chunks):
                    tokens = _tokenize(text)
                    cursor = c.execute(
                        "INSERT INTO knowledge_chunks (doc_id, seq, text, tokens) VALUES (?, ?, ?, ?)",
                        (doc_id, seq, text, json.dumps(tokens, ensure_ascii=False)),
                    )
                    chunk_ids.append(int(cursor.lastrowid))
                entity_ids = self._write_graph(c, doc_id, entities, relations)
                for chunk_id, text in zip(chunk_ids, chunks):
                    lowered = text.lower()
                    for key, entity_id in entity_ids.items():
                        if key and key in lowered:
                            c.execute(
                                "INSERT OR IGNORE INTO knowledge_chunk_entities (chunk_id, entity_id)"
                                " VALUES (?, ?)",
                                (chunk_id, entity_id),
                            )
        except sqlite3.IntegrityError:
            return {"status": "duplicate"}
        self._index = None
        logger.info("知识入库：%s（%d 块，%d 实体）", final_title, len(chunks), len(entity_ids))
        return {
            "status": "ingested",
            "doc_id": doc_id,
            "title": final_title,
            "kind": kind,
            "chunks": len(chunks),
            "entities": len(entity_ids),
        }

    def _write_graph(self, conn, doc_id: int, entities: list, relations: list) -> dict:
        """写实体与关系，返回 {name_key: entity_id}。"""
        entity_ids: dict[str, int] = {}

        def ensure(name: str) -> int | None:
            key = _entity_key(name)
            if len(key) < 2:
                return None
            if key in entity_ids:
                return entity_ids[key]
            row = conn.execute(
                "SELECT entity_id FROM knowledge_entities WHERE name_key = ?", (key,)
            ).fetchone()
            if row:
                entity_id = int(row["entity_id"])
            else:
                entity_id = int(
                    conn.execute(
                        "INSERT INTO knowledge_entities (name, name_key) VALUES (?, ?)",
                        (name.strip()[:60], key),
                    ).lastrowid
                )
            entity_ids[key] = entity_id
            return entity_id

        for name in entities:
            if isinstance(name, str):
                ensure(name)
        for relation in relations:
            src_id = ensure(relation[0])
            dst_id = ensure(relation[1])
            if src_id is None or dst_id is None or src_id == dst_id:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_edges (src_id, dst_id, relation, doc_id)"
                " VALUES (?, ?, ?, ?)",
                (src_id, dst_id, (relation[2] if len(relation) > 2 else "")[:80], doc_id),
            )
        return entity_ids

    # ---------- 整合（实体档案 / 主题综述） ----------

    def consolidate(
        self,
        generate,
        *,
        scope: str = "all",
        min_chunks: int = 2,
        limit: int = 50,
        max_members: int = 30,
        force: bool = False,
        dry_run: bool = False,
        on_step=None,
    ) -> dict:
        """批量生成实体档案与主题综述；来源未变化则跳过（force=True 强制重生成）；过大板块不生成。"""
        report = {
            "entity_done": 0,
            "entity_skipped": 0,
            "entity_failed": 0,
            "cluster_done": 0,
            "cluster_skipped": 0,
            "cluster_failed": 0,
        }
        notify = on_step or (lambda _message: None)
        if scope in ("all", "entity"):
            self._consolidate_entities(generate, min_chunks, limit, force, dry_run, notify, report)
        if scope in ("all", "cluster"):
            self._consolidate_clusters(generate, limit, max_members, force, dry_run, notify, report)
        return report

    def _consolidate_entities(self, generate, min_chunks, limit, force, dry_run, notify, report) -> None:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT e.name, e.name_key, GROUP_CONCAT(ce.chunk_id) AS chunk_ids
                FROM knowledge_entities e
                JOIN knowledge_chunk_entities ce ON ce.entity_id = e.entity_id
                GROUP BY e.entity_id
                HAVING COUNT(ce.chunk_id) >= ?
                ORDER BY COUNT(ce.chunk_id) DESC
                LIMIT ?
                """,
                (min_chunks, limit),
            ).fetchall()
            existing = {
                row["target_key"]: row["digest"]
                for row in self.conn.execute(
                    "SELECT target_key, digest FROM knowledge_summaries WHERE scope = 'entity'"
                )
            }
        for row in rows:
            chunk_ids = sorted(int(x) for x in str(row["chunk_ids"]).split(",") if x.strip())
            digest = _digest_of(chunk_ids)
            if not force and existing.get(row["name_key"]) == digest:
                report["entity_skipped"] += 1
                continue
            if dry_run:
                notify(f"[dry-run] 实体档案：{row['name']}（来源 {len(chunk_ids)} 块）")
                report["entity_done"] += 1
                continue
            excerpts = self._chunk_texts(chunk_ids)
            try:
                parsed = parse_summary(generate(build_entity_summary_prompt(row["name"], excerpts)))
            except Exception as exc:
                notify(f"实体档案失败：{row['name']}（{exc}）")
                report["entity_failed"] += 1
                continue
            self._upsert_summary("entity", row["name_key"], row["name"], render_summary_text(parsed), [], digest)
            notify(f"实体档案完成：{row['name']}（来源 {len(chunk_ids)} 块）")
            report["entity_done"] += 1

    def _consolidate_clusters(self, generate, limit, max_members, force, dry_run, notify, report) -> None:
        components = self._components()
        with self.lock:
            existing = {
                row["target_key"]: row["digest"]
                for row in self.conn.execute(
                    "SELECT target_key, digest FROM knowledge_summaries WHERE scope = 'cluster'"
                )
            }
        processed = 0
        for members in components:
            if len(members) < 2:
                continue
            if len(members) > max_members:
                notify(f"[跳过] 板块过大（{len(members)} 个实体，阈值 {max_members}）：{'、'.join(members[:6])}…")
                report["cluster_skipped"] += 1
                continue
            if processed >= limit:
                break
            digest = _digest_of(members)
            if not force and existing.get(digest) == digest:
                report["cluster_skipped"] += 1
                continue
            if dry_run:
                notify(f"[dry-run] 主题综述：{'、'.join(members[:6])}（{len(members)} 个实体）")
                report["cluster_done"] += 1
                processed += 1
                continue
            excerpts = self._sample_excerpts(members)
            try:
                parsed = parse_summary(
                    generate(build_cluster_summary_prompt(members[:12], excerpts)), with_title=True
                )
            except Exception as exc:
                notify(f"主题综述失败：{'、'.join(members[:3])}…（{exc}）")
                report["cluster_failed"] += 1
                processed += 1
                continue
            title = parsed.get("title") or f"{members[0]} 等相关主题"
            self._upsert_summary("cluster", digest, title, render_summary_text(parsed), members, digest)
            notify(f"主题综述完成：{title}（{len(members)} 个实体）")
            report["cluster_done"] += 1
            processed += 1

    def _chunk_texts(self, chunk_ids: list[int]) -> list[str]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"SELECT text FROM knowledge_chunks WHERE chunk_id IN ({placeholders}) ORDER BY chunk_id",
            chunk_ids,
        ).fetchall()
        return [row["text"] for row in rows]

    def _sample_excerpts(self, member_keys: list[str], per_entity: int = 2) -> list[str]:
        """社区综述素材：每个成员实体取少量关联块。"""
        excerpts: list[str] = []
        for key in member_keys:
            rows = self.conn.execute(
                """
                SELECT kc.text FROM knowledge_chunks kc
                JOIN knowledge_chunk_entities kce ON kce.chunk_id = kc.chunk_id
                JOIN knowledge_entities ke ON ke.entity_id = kce.entity_id
                WHERE ke.name_key = ?
                ORDER BY kc.chunk_id
                LIMIT ?
                """,
                (key, per_entity),
            ).fetchall()
            excerpts += [row["text"] for row in rows]
        return excerpts

    def _components(self) -> list[list[str]]:
        """实体关系图的连通分量（成员为 name_key，按成员数降序）。"""
        names: dict[int, str] = {
            int(row["entity_id"]): row["name_key"]
            for row in self.conn.execute("SELECT entity_id, name_key FROM knowledge_entities")
        }
        adjacency: dict[int, set[int]] = defaultdict(set)
        for row in self.conn.execute("SELECT src_id, dst_id FROM knowledge_edges"):
            src, dst = int(row["src_id"]), int(row["dst_id"])
            adjacency[src].add(dst)
            adjacency[dst].add(src)
        seen: set[int] = set()
        components: list[list[str]] = []
        for entity_id in names:
            if entity_id in seen:
                continue
            stack = [entity_id]
            members: list[int] = []
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                members.append(node)
                stack.extend(adjacency.get(node, ()))
            components.append(sorted(names[node] for node in members))
        components.sort(key=len, reverse=True)
        return components

    def _upsert_summary(self, scope: str, target_key: str, title: str, text: str, members: list, digest: str) -> None:
        with self.lock, transaction(self.conn) as c:
            c.execute(
                """
                INSERT INTO knowledge_summaries (scope, target_key, title, text, members, digest, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (scope, target_key) DO UPDATE SET
                    title = excluded.title,
                    text = excluded.text,
                    members = excluded.members,
                    digest = excluded.digest,
                    updated_at = excluded.updated_at
                """,
                (
                    scope,
                    target_key,
                    title[:60],
                    text,
                    json.dumps(members, ensure_ascii=False),
                    digest,
                    now_iso(),
                ),
            )

    # ---------- 检索 ----------

    def search(self, query: str, *, limit: int | None = None, max_chars: int | None = None,
               min_matches: int | None = None) -> list[dict]:
        """检索与 query 相关的知识块；返回 [{title, date, text, score}]（不命中返回空列表）。"""
        query = (query or "").strip()
        if not query:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        limit = limit or self.top_k
        budget = max_chars or self.max_chars
        required = self.min_matches if min_matches is None else min_matches

        with self.lock:
            index = self._ensure_index()
            scores: dict[int, float] = defaultdict(float)
            matched: dict[int, set[str]] = defaultdict(set)
            for token in set(tokens):
                df = index.df.get(token, 0)
                if not df:
                    continue
                idf = math.log(1 + (index.total - df + 0.5) / (df + 0.5))
                for chunk_id, tf in index.postings.get(token, ()):
                    length = index.chunk_length.get(chunk_id, 1)
                    denom = tf + K1 * (1 - B + B * length / max(index.avg_length, 1.0))
                    scores[chunk_id] += idf * tf * (K1 + 1) / denom
                    matched[chunk_id].add(token)
            boosts, matched_keys = self._graph_boosts(index, query)

        ranked = []
        for chunk_id in set(scores) | set(boosts):
            boost = boosts.get(chunk_id, 0.0)
            hits = len(matched.get(chunk_id, ()))
            if hits < required and boost <= 0:
                continue
            ranked.append((scores.get(chunk_id, 0.0) + boost, chunk_id))
        ranked.sort(reverse=True)
        top_score = ranked[0][0] if ranked else 0.0
        with self.lock:
            summary_items = self._summary_excerpts(matched_keys, top_score + 1.0)

        items: list[tuple[float, dict]] = [(item["score"], item) for item in summary_items]
        for score, chunk_id in ranked:
            meta = index.chunks.get(chunk_id)
            if meta:
                items.append((score, meta))
        items.sort(key=lambda pair: pair[0], reverse=True)

        excerpts: list[dict] = []
        used = 0
        for score, item in items:
            if len(excerpts) >= limit or used >= budget:
                break
            text = _compact(item["text"])
            if len(text) > budget - used:
                text = text[: max(budget - used - 1, 79)].rstrip() + "…"
            used += len(text)
            if "title" in item:  # 档案 / 综述成稿
                excerpts.append(
                    {
                        "title": item["title"],
                        "date": str(item.get("date", ""))[:10],
                        "text": text,
                        "score": round(score, 3),
                    }
                )
                continue
            doc = index.docs.get(item["doc_id"], {})
            excerpts.append(
                {
                    "title": doc.get("title", ""),
                    "date": str(doc.get("created_at", ""))[:10],
                    "text": text,
                    "score": round(score, 3),
                }
            )
        return excerpts

    def _graph_boosts(self, index: "_Index", query: str) -> tuple[dict[int, float], list[str]]:
        """查询命中实体后沿关系图展开：直连块与（1..graph_hops 跳）邻居块加权；同时返回命中实体名。"""
        query_key = _entity_key(query)
        if not query_key:
            return {}, []
        boosts: dict[int, float] = {}
        matched_keys: list[str] = []
        for name_key, entity_id in index.entities.items():
            if len(name_key) < 2 or name_key not in query_key:
                continue
            matched_keys.append(name_key)
            frontier = {entity_id}
            for hop in range(self.graph_hops + 1):
                weight = _DIRECT_BOOST if hop == 0 else _NEIGHBOR_BOOST / hop
                for node in frontier:
                    for chunk_id in index.entity_chunks.get(node, ()):
                        if weight > boosts.get(chunk_id, 0.0):
                            boosts[chunk_id] = weight
                next_frontier: set[int] = set()
                for node in frontier:
                    next_frontier |= index.entity_neighbors.get(node, set())
                frontier = next_frontier - {entity_id}
                if not frontier:
                    break
        return boosts, matched_keys

    def _summary_excerpts(self, matched_keys: list[str], ceiling: float) -> list[dict]:
        """命中的实体档案（最多 2 份）+ 同板块主题综述（≥2 个成员命中时）；成稿分数高于原始块。"""
        if not matched_keys or not self.inject_summaries:
            return []
        items: list[dict] = []
        placeholders = ",".join("?" for _ in matched_keys)
        rows = self.conn.execute(
            f"SELECT * FROM knowledge_summaries WHERE scope = 'entity' AND target_key IN ({placeholders})",
            matched_keys,
        ).fetchall()
        score = ceiling
        for row in rows[:2]:
            items.append(
                {
                    "title": f"{row['title']}（知识档案）",
                    "date": str(row["updated_at"])[:10],
                    "text": row["text"],
                    "score": score,
                }
            )
            score -= 0.1
        best_overlap = 0
        best_row = None
        for row in self.conn.execute("SELECT * FROM knowledge_summaries WHERE scope = 'cluster'"):
            members = set(json.loads(row["members"] or "[]"))
            overlap = len(members & set(matched_keys))
            if overlap >= 2 and overlap > best_overlap:
                best_overlap, best_row = overlap, row
        if best_row is not None:
            items.append(
                {
                    "title": f"主题综述：{best_row['title']}",
                    "date": str(best_row["updated_at"])[:10],
                    "text": best_row["text"],
                    "score": ceiling - 0.5,
                }
            )
        return items

    # ---------- 索引 ----------

    def _signature(self) -> tuple:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(chunk_id), 0) FROM knowledge_chunks"
        ).fetchone()
        docs = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(doc_id), 0) FROM knowledge_docs"
        ).fetchone()
        return (int(row[0]), int(row[1]), int(docs[0]), int(docs[1]))

    def _ensure_index(self) -> "_Index":
        signature = self._signature()
        if self._index is not None and self._index.signature == signature:
            return self._index
        index = _Index()
        index.signature = signature
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for row in self.conn.execute("SELECT chunk_id, doc_id, text, tokens FROM knowledge_chunks"):
            chunk_id = int(row["chunk_id"])
            tokens = json.loads(row["tokens"] or "[]")
            index.chunks[chunk_id] = {"doc_id": int(row["doc_id"]), "text": row["text"]}
            index.chunk_length[chunk_id] = max(len(tokens), 1)
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            for token, tf in counts.items():
                postings[token].append((chunk_id, tf))
                index.df[token] = index.df.get(token, 0) + 1
        index.postings = dict(postings)
        index.total = max(len(index.chunks), 1)
        index.avg_length = (
            sum(index.chunk_length.values()) / len(index.chunk_length) if index.chunk_length else 1.0
        )
        for row in self.conn.execute("SELECT doc_id, title, created_at FROM knowledge_docs"):
            index.docs[int(row["doc_id"])] = {"title": row["title"], "created_at": row["created_at"]}
        for row in self.conn.execute("SELECT entity_id, name_key FROM knowledge_entities"):
            index.entities[row["name_key"]] = int(row["entity_id"])
        for row in self.conn.execute("SELECT chunk_id, entity_id FROM knowledge_chunk_entities"):
            index.entity_chunks.setdefault(int(row["entity_id"]), set()).add(int(row["chunk_id"]))
        for row in self.conn.execute("SELECT src_id, dst_id FROM knowledge_edges"):
            src, dst = int(row["src_id"]), int(row["dst_id"])
            index.entity_neighbors.setdefault(src, set()).add(dst)
            index.entity_neighbors.setdefault(dst, set()).add(src)
        self._index = index
        return index

    # ---------- 快照导出 ----------

    def export_snapshots(self, directory: Path | str) -> list[Path]:
        """导出整合结果 Markdown 快照：实体档案.md / 主题综述.md（内容有变化才写）。"""
        target = Path(directory)
        entity_rows = self.conn.execute(
            "SELECT title, text, updated_at FROM knowledge_summaries WHERE scope = 'entity' ORDER BY title"
        ).fetchall()
        cluster_rows = self.conn.execute(
            "SELECT title, text, members, updated_at FROM knowledge_summaries"
            " WHERE scope = 'cluster' ORDER BY updated_at DESC"
        ).fetchall()
        if not entity_rows and not cluster_rows:
            return []
        target.mkdir(parents=True, exist_ok=True)
        updated: list[Path] = []
        documents = (
            (
                "实体档案.md",
                "知识库整合结果：每个实体的跨文档综合档案（manage knowledge consolidate 生成）",
                entity_rows,
                False,
            ),
            (
                "主题综述.md",
                "知识库整合结果：实体关系图划分的主题板块综述（manage knowledge consolidate 生成）",
                cluster_rows,
                True,
            ),
        )
        for name, intro, rows, with_members in documents:
            content = _render_snapshot(intro, rows, with_members)
            if not rows:
                continue
            path = target / name
            if path.exists() and path.read_text(encoding="utf-8") == content:
                continue
            path.write_text(content, encoding="utf-8")
            updated.append(path)
        if updated:
            logger.info("知识整合快照导出：%d 个文件更新（%s）", len(updated), target)
        return updated

    # ---------- 统计 ----------

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS docs, COALESCE(SUM(LENGTH(content)), 0) AS chars FROM knowledge_docs"
        ).fetchone()
        chunks = int(self.conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0])
        entities = int(self.conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0])
        edges = int(self.conn.execute("SELECT COUNT(*) FROM knowledge_edges").fetchone()[0])
        summaries = int(self.conn.execute("SELECT COUNT(*) FROM knowledge_summaries").fetchone()[0])
        return {
            "docs": int(row["docs"]),
            "chars": int(row["chars"]),
            "chunks": chunks,
            "entities": entities,
            "edges": edges,
            "summaries": summaries,
        }


def _render_snapshot(intro: str, rows: list, with_members: bool) -> str:
    """渲染整合结果快照：标题 +（可选成员列表）+ 正文。"""
    blocks = [
        "# 知识整合结果",
        "",
        f"> {intro}，导出时间：{now_iso()[:10]}",
        "",
        "（回顾性快照，仅供阅读；权威数据在 runtime/ohana.sqlite3 的 knowledge_summaries 表）",
        "",
    ]
    for row in rows:
        blocks.append(f"## {row['title']}")
        blocks.append("")
        blocks.append(row["text"])
        if with_members:
            members = json.loads(row["members"] or "[]")
            suffix = "…" if len(members) > 12 else ""
            blocks.append("")
            blocks.append(f"> 板块成员：{'、'.join(members[:12])}{suffix}")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


class _Index:
    """进程内检索索引（文档、分词倒排、实体图）。"""

    def __init__(self):
        self.signature: tuple = ()
        self.chunks: dict[int, dict] = {}
        self.chunk_length: dict[int, int] = {}
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.df: dict[str, int] = {}
        self.total = 1
        self.avg_length = 1.0
        self.docs: dict[int, dict] = {}
        self.entities: dict[str, int] = {}
        self.entity_chunks: dict[int, set[int]] = {}
        self.entity_neighbors: dict[int, set[int]] = {}
