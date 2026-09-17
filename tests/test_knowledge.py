"""知识库（GraphRAG-lite）的测试：分词/分块、分类入库、去重、检索与图扩展、上下文渲染。

运行：python -m unittest discover -s tests -v
"""

import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

from bot.engine import context, knowledge
from bot.engine.config import EngineConfig, load_config
from bot.engine.knowledge import KnowledgeBase
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore


def make_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn, MemoryStore(conn)


def fake_classify(kind="knowledge", title="测试资料", entities=None, relations=None):
    payload = json.dumps(
        {
            "kind": kind,
            "title": title,
            "entities": entities or [],
            "relations": relations or [],
        },
        ensure_ascii=False,
    )
    return lambda prompt: payload


class TokenTests(unittest.TestCase):
    def test_tokenize_keeps_meaningful_words(self):
        tokens = knowledge._tokenize("肖申克的救赎 IMDb 评分 9.7")
        self.assertIn("imdb", tokens)
        self.assertIn("9.7", tokens)
        self.assertIn("救赎", tokens)

    def test_stopwords_filtered(self):
        tokens = knowledge._tokenize("我们觉得这个是可能的")
        self.assertNotIn("我们", tokens)
        self.assertNotIn("这个", tokens)


class ChunkTests(unittest.TestCase):
    def test_short_doc_single_chunk(self):
        chunks = knowledge._chunk_markdown("第一段。\n\n第二段。")
        self.assertEqual(len(chunks), 1)
        self.assertIn("第一段", chunks[0])

    def test_long_doc_split_within_limits(self):
        doc = "\n\n".join(f"第{i}段：" + "内容" * 60 for i in range(30))
        chunks = knowledge._chunk_markdown(doc)
        self.assertGreater(len(chunks), 3)
        self.assertLessEqual(len(chunks), knowledge.CHUNK_LIMIT)
        self.assertTrue(all(len(c) <= knowledge.CHUNK_MAX for c in chunks))

    def test_oversized_block_hard_split(self):
        chunks = knowledge._chunk_markdown("字" * (knowledge.CHUNK_MAX * 2 + 10))
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= knowledge.CHUNK_MAX for c in chunks))


class ClassificationParseTests(unittest.TestCase):
    def test_valid_with_fence(self):
        raw = '```json\n{"kind": "knowledge", "title": "标题", "entities": ["甲", "乙"], "relations": [["甲", "乙", "关系"]]}\n```'
        meta = knowledge.parse_classification(raw)
        self.assertEqual(meta["kind"], "knowledge")
        self.assertEqual(meta["entities"], ["甲", "乙"])
        self.assertEqual(meta["relations"], [["甲", "乙", "关系"]])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            knowledge.parse_classification("不是 json")
        with self.assertRaises(ValueError):
            knowledge.parse_classification('{"kind": "unknown"}')


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)

    def test_ingest_knowledge_with_graph(self):
        outcome = self.base.ingest_document(
            title="电影资料",
            source="测试",
            content="《肖申克的救赎》由弗兰克·德拉邦特执导。\n\n影片常年位居榜首。",
            classify=fake_classify(
                entities=["肖申克的救赎", "弗兰克·德拉邦特"],
                relations=[["肖申克的救赎", "弗兰克·德拉邦特", "执导"]],
            ),
        )
        self.assertEqual(outcome["status"], "ingested")
        self.assertEqual(outcome["kind"], "knowledge")
        stats = self.base.stats()
        self.assertEqual(stats["docs"], 1)
        self.assertGreaterEqual(stats["chunks"], 1)
        self.assertEqual(stats["entities"], 2)
        self.assertEqual(stats["edges"], 1)

    def test_duplicate_content_skipped(self):
        content = "同一份内容。"
        first = self.base.ingest_document(title="A", content=content, force_kind="knowledge")
        second = self.base.ingest_document(title="B", content=content, force_kind="knowledge")
        self.assertEqual(first["status"], "ingested")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.base.stats()["docs"], 1)

    def test_news_skipped(self):
        outcome = self.base.ingest_document(
            title="某发布会", content="今天发布了新款。", classify=fake_classify(kind="news")
        )
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.base.stats()["docs"], 0)

    def test_classify_failure_skips_ingest(self):
        outcome = self.base.ingest_document(
            title="X", content="内容内容", classify=lambda prompt: "不是 json"
        )
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(self.base.stats()["docs"], 0)

    def test_classify_retry_once(self):
        calls = {"n": 0}

        def classify(prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                return "不是 json"
            return json.dumps({"kind": "knowledge", "title": "重试成功"}, ensure_ascii=False)

        outcome = self.base.ingest_document(title="X", content="重试内容", classify=classify)
        self.assertEqual(outcome["status"], "ingested")
        self.assertEqual(calls["n"], 2)

    def test_empty_content(self):
        self.assertEqual(self.base.ingest_document(title="X", content="  ")["status"], "empty")


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)
        self.base.ingest_document(
            title="肖申克资料",
            content="《肖申克的救赎》由弗兰克·德拉邦特拍摄。\n\n影片讲的是越狱与希望的故事，常年位居榜首。",
            classify=fake_classify(
                title="肖申克资料",
                entities=["肖申克的救赎", "弗兰克·德拉邦特"],
                relations=[["肖申克的救赎", "弗兰克·德拉邦特", "执导"]],
            ),
        )
        self.base.ingest_document(
            title="误杀资料",
            content="《误杀》系列里肖央扮演父亲，翻拍自印度电影。\n\n结局争议很大。",
            classify=fake_classify(title="误杀资料", entities=["误杀", "肖央"]),
        )

    def test_relevant_doc_ranked_first(self):
        results = self.base.search("肖申克的救赎 好看吗")
        self.assertTrue(results)
        self.assertEqual(results[0]["title"], "肖申克资料")
        self.assertIn("救赎", results[0]["text"])

    def test_unrelated_query_returns_empty(self):
        self.assertEqual(self.base.search("我们今天中午吃什么"), [])

    def test_top_k_limit(self):
        results = self.base.search("救赎 误杀 电影", limit=1)
        self.assertEqual(len(results), 1)

    def test_char_budget_respected(self):
        results = self.base.search("救赎 误杀 电影", max_chars=120)
        self.assertLessEqual(sum(len(r["text"]) for r in results), 120)


class GraphExpansionTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)

    def test_neighbor_chunk_injected_without_lexical_match(self):
        content = "电影甲由导演乙拍摄完成。" + "补充说明。" * 80 + "\n\n导演乙的新作话题度很高。"
        outcome = self.base.ingest_document(
            title="电影甲资料",
            content=content,
            classify=fake_classify(
                entities=["电影甲", "导演乙"],
                relations=[["电影甲", "导演乙", "执导"]],
            ),
        )
        self.assertEqual(outcome["status"], "ingested")
        results = self.base.search("电影甲")
        texts = " ".join(r["text"] for r in results)
        # 第二块与查询无词法重叠，仅靠实体图扩展入选
        self.assertIn("话题度", texts)

    def test_graph_hops_zero_disables_expansion(self):
        content = "电影甲由导演乙拍摄完成。" + "补充说明。" * 80 + "\n\n导演乙的新作话题度很高。"
        self.base.ingest_document(
            title="电影甲资料",
            content=content,
            classify=fake_classify(
                entities=["电影甲", "导演乙"],
                relations=[["电影甲", "导演乙", "执导"]],
            ),
        )
        options = EngineConfig().knowledge
        options.graph_hops = 0
        base = KnowledgeBase(self.conn, options)
        results = base.search("电影甲")
        texts = " ".join(r["text"] for r in results)
        self.assertIn("拍摄完成", texts)
        self.assertNotIn("话题度", texts)


class SummaryParseTests(unittest.TestCase):
    def test_valid(self):
        parsed = knowledge.parse_summary('{"summary": "概述", "facts": ["甲", "乙"]}')
        self.assertEqual(parsed["summary"], "概述")
        self.assertEqual(parsed["facts"], ["甲", "乙"])

    def test_fence_and_title(self):
        raw = '```json\n{"title": "电影板块", "summary": "概述"}\n```'
        parsed = knowledge.parse_summary(raw, with_title=True)
        self.assertEqual(parsed["title"], "电影板块")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            knowledge.parse_summary("nope")
        with self.assertRaises(ValueError):
            knowledge.parse_summary('{"facts": []}')


def fake_summary_reply(summary="综合概述", facts=None, title=None):
    payload = {"summary": summary, "facts": facts or []}
    if title:
        payload["title"] = title
    return json.dumps(payload, ensure_ascii=False)


def graph_content():
    """构造：电影甲关联 3 块、导演乙关联 2 块（均满足 min_chunks=2），两者有执导关系。"""
    return (
        "电影甲由导演乙拍摄。\n\n"
        + "电影甲影评要点："
        + "好评如潮。" * 90
        + "\n\n导演乙的其他作品也有讨论度，电影甲续集在筹备。"
    )


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)
        self.base.ingest_document(
            title="电影甲资料",
            content=graph_content(),
            classify=fake_classify(
                title="电影甲资料",
                entities=["电影甲", "导演乙"],
                relations=[["电影甲", "导演乙", "执导"]],
            ),
        )

    def test_entity_consolidate_creates_dossier(self):
        prompts = []

        def generate(prompt):
            prompts.append(prompt)
            return fake_summary_reply("电影甲综合概述", ["要点一", "要点二"])

        report = self.base.consolidate(generate, scope="entity")
        self.assertGreaterEqual(report["entity_done"], 1)
        self.assertTrue(all("实体档案" in p for p in prompts))
        row = self.conn.execute(
            "SELECT * FROM knowledge_summaries WHERE scope = 'entity' LIMIT 1"
        ).fetchone()
        self.assertIn("电影甲综合概述", row["text"])
        self.assertIn("要点一", row["text"])

    def test_second_run_skips_unchanged(self):
        calls = {"n": 0}

        def generate(prompt):
            calls["n"] += 1
            return fake_summary_reply("概述")

        self.base.consolidate(generate, scope="entity")
        first = calls["n"]
        report = self.base.consolidate(generate, scope="entity")
        self.assertEqual(calls["n"], first)  # 来源未变 → 不再调用模型
        self.assertGreaterEqual(report["entity_skipped"], 1)

    def test_dry_run_no_write(self):
        calls = {"n": 0}

        def generate(prompt):
            calls["n"] += 1
            return fake_summary_reply("概述")

        report = self.base.consolidate(generate, scope="all", dry_run=True)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(self.base.stats()["summaries"], 0)
        self.assertGreaterEqual(report["entity_done"], 1)

    def test_cluster_consolidate(self):
        def generate(prompt):
            if "主题综述" in prompt:
                return fake_summary_reply("板块综述", title="电影板块")
            return fake_summary_reply("实体概述")

        report = self.base.consolidate(generate, scope="cluster")
        self.assertEqual(report["cluster_done"], 1)
        row = self.conn.execute(
            "SELECT * FROM knowledge_summaries WHERE scope = 'cluster'"
        ).fetchone()
        self.assertEqual(row["title"], "电影板块")
        members = json.loads(row["members"])
        self.assertIn("电影甲", members)
        self.assertIn("导演乙", members)

    def test_oversized_cluster_skipped(self):
        def generate(prompt):
            return fake_summary_reply("实体概述")

        steps = []
        report = self.base.consolidate(
            generate, scope="cluster", max_members=1, on_step=steps.append
        )
        self.assertEqual(report["cluster_done"], 0)
        self.assertEqual(report["cluster_skipped"], 1)
        self.assertTrue(any("板块过大" in msg for msg in steps))


class SummaryInjectionTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)
        self.base.ingest_document(
            title="电影甲资料",
            content=graph_content(),
            classify=fake_classify(
                title="电影甲资料",
                entities=["电影甲", "导演乙"],
                relations=[["电影甲", "导演乙", "执导"]],
            ),
        )

        def generate(prompt):
            if "主题综述" in prompt:
                return fake_summary_reply("板块综述", title="电影板块")
            return fake_summary_reply("电影甲综合概述", ["要点一"])

        self.base.consolidate(generate)

    def test_dossier_injected_first(self):
        results = self.base.search("电影甲 怎么样", limit=4)
        self.assertTrue(results)
        self.assertIn("知识档案", results[0]["title"])
        self.assertIn("电影甲综合概述", results[0]["text"])

    def test_cluster_injected_on_two_entities(self):
        results = self.base.search("电影甲 导演乙", limit=4)
        titles = " ".join(r["title"] for r in results)
        self.assertIn("主题综述", titles)

    def test_cluster_not_injected_on_single_entity(self):
        results = self.base.search("电影甲 怎么样", limit=4)
        titles = " ".join(r["title"] for r in results)
        self.assertNotIn("主题综述", titles)

    def test_inject_summaries_disabled(self):
        options = EngineConfig().knowledge
        options.inject_summaries = False
        base = KnowledgeBase(self.conn, options)
        results = base.search("电影甲 怎么样", limit=4)
        titles = " ".join(r["title"] for r in results)
        self.assertNotIn("知识档案", titles)


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = make_store()
        self.base = KnowledgeBase(self.conn, EngineConfig().knowledge)
        self.base.ingest_document(
            title="电影甲资料",
            content=graph_content(),
            classify=fake_classify(
                title="电影甲资料",
                entities=["电影甲", "导演乙"],
                relations=[["电影甲", "导演乙", "执导"]],
            ),
        )

        def generate(prompt):
            if "主题综述" in prompt:
                return fake_summary_reply("板块综述", title="电影板块")
            return fake_summary_reply("电影甲综合概述", ["要点一"])

        self.base.consolidate(generate)

    def test_export_writes_snapshots(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        updated = self.base.export_snapshots(Path(tmp.name))
        names = {path.name for path in updated}
        self.assertIn("实体档案.md", names)
        self.assertIn("主题综述.md", names)
        dossier = (Path(tmp.name) / "实体档案.md").read_text(encoding="utf-8")
        self.assertIn("电影甲", dossier)
        self.assertIn("电影甲综合概述", dossier)
        cluster = (Path(tmp.name) / "主题综述.md").read_text(encoding="utf-8")
        self.assertIn("电影板块", cluster)
        self.assertIn("板块成员", cluster)

    def test_second_export_skips_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertTrue(self.base.export_snapshots(Path(tmp.name)))
        self.assertEqual(self.base.export_snapshots(Path(tmp.name)), [])

    def test_empty_knowledge_base_no_files(self):
        conn, _ = make_store()
        base = KnowledgeBase(conn, EngineConfig().knowledge)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(base.export_snapshots(Path(tmp.name)), [])


class ContextRenderTests(unittest.TestCase):
    def test_render_knowledge_empty(self):
        self.assertEqual(context._render_knowledge(None), "")
        self.assertEqual(context._render_knowledge([]), "")

    def test_render_knowledge_block(self):
        rend = context._render_knowledge(
            [{"title": "资料甲", "date": "2026-09-17", "text": "要点内容", "score": 1.0}]
        )
        self.assertIn("【知识库摘录】", rend)
        self.assertIn("资料甲", rend)
        self.assertIn("要点内容", rend)

    def test_build_prompt_injects_knowledge(self):
        conn, store = make_store()
        store.ensure_conversation("qq", "group:1", "测试群")
        batch = [
            types.SimpleNamespace(
                message_id="m1", user_id="u1", display_name="某人", text="聊聊老电影",
                sent_at="2026-09-17T10:00:00Z",
            )
        ]
        excerpt = [{"title": "资料甲", "date": "2026-09-17", "text": "要点内容", "score": 1.0}]
        with_kb = context.build_reply_prompt(
            "S", "Y", store, "qq", "group:1", batch, False, knowledge=excerpt
        )
        without_kb = context.build_reply_prompt("S", "Y", store, "qq", "group:1", batch, False)
        self.assertIn("【知识库摘录】", with_kb)
        self.assertNotIn("【知识库摘录】", without_kb)
        self.assertNotIn("{knowledge}", with_kb)
        self.assertNotIn("{knowledge}", without_kb)


class KnowledgeConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = EngineConfig().knowledge
        self.assertTrue(config.enabled)
        self.assertTrue(config.auto_ingest)
        self.assertEqual(config.top_k, 2)
        self.assertEqual(config.max_chars, 600)
        self.assertEqual(config.min_matches, 2)
        self.assertEqual(config.graph_hops, 1)
        self.assertTrue(config.inject_summaries)

    def test_yaml_parse(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.yaml"
        path.write_text(
            "knowledge:\n  enabled: false\n  auto_ingest: false\n  top_k: 3\n  graph_hops: 0\n  inject_summaries: false\n",
            encoding="utf-8",
        )
        config = load_config(path).knowledge
        self.assertFalse(config.enabled)
        self.assertFalse(config.auto_ingest)
        self.assertEqual(config.top_k, 3)
        self.assertEqual(config.graph_hops, 0)
        self.assertFalse(config.inject_summaries)


if __name__ == "__main__":
    unittest.main()
