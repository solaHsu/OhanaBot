#!/usr/bin/env python3
"""Ohana 资料管线第二步：分层抽样，生成供深度阅读分析的材料。

用法：
    python scripts/02_sample.py --input analysis/corpus --output analysis/samples --seed 42

设计依据 docs/data-pipeline.md：
    - 固定随机种子；按成员、月份、长短消息、回复场景分层；
    - 每位成员最多 max_samples_per_member 个中心样本，不足时全取并标记覆盖缺口；
    - 样本携带同会话前后各至多 context_before/context_after 条消息，上下文同样脱敏；
    - 最近 holdout_days 天的中心样本标记为 holdout（评估留出），上下文随中心同属一个集合；
    - manifest 保存种子、记录 ID 清单、配置与脚本版本、输出摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCHEMA_VERSION = 1
SCRIPT_VERSION = "0.1.0"
LENGTH_BANDS = ((0, 10, "短"), (11, 30, "中"), (31, 10**9, "长"))


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_rng(seed: int, *parts: str) -> random.Random:
    key = f"{seed}:" + ":".join(parts)
    return random.Random(int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big"))


def length_band(n: int) -> str:
    for lo, hi, name in LENGTH_BANDS:
        if lo <= n <= hi:
            return name
    return "长"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"配置无法解析或版本不匹配：{path}")
    return cfg


def load_corpus(corpus_dir: Path):
    """读取全部成员语料；返回 (member_rows, streams, meta)。

    streams: {conversation_id: [record, ...]}（按时间排序的完整会话序列）
    """
    member_rows: dict[str, list] = {}
    meta: dict[str, dict] = {}
    index = json.loads((corpus_dir / "index.json").read_text(encoding="utf-8"))
    for m in index.get("members", []):
        meta[m["alias"]] = m
    for f in sorted((corpus_dir / "members").glob("*.jsonl")):
        alias = f.stem
        rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows.sort(key=lambda r: (r["timestamp"], r["record_id"]))
        member_rows[alias] = rows
    streams: dict[str, list] = defaultdict(list)
    for alias in sorted(member_rows):
        for r in member_rows[alias]:
            streams[r["conversation_id"]].append(r)
    for conv in streams:
        streams[conv].sort(key=lambda r: (r["timestamp"], r["record_id"]))
    return member_rows, dict(streams), meta


def select_samples(rows: list, max_n: int, seed: int, alias: str, tz: ZoneInfo):
    """按 (月份, 长度档, 是否回复) 分层，轮转抽样至多 max_n 条。返回 (选中行, 分层计数)。"""
    strata: dict[tuple, list] = defaultdict(list)
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).astimezone(tz)
        band = length_band(len(r.get("text") or ""))
        is_reply = "reply" if (r.get("reply_to") or "reply_unresolved" in (r.get("flags") or [])) else "talk"
        strata[(dt.strftime("%Y-%m"), band, is_reply)].append(r)
    rng = stable_rng(seed, alias)
    keys = sorted(strata)
    for k in keys:
        rng.shuffle(strata[k])
    picked: list = []
    depth = {k: 0 for k in keys}
    while len(picked) < max_n:
        progress = False
        for k in keys:
            if len(picked) >= max_n:
                break
            i = depth[k]
            if i < len(strata[k]):
                picked.append(strata[k][i])
                depth[k] += 1
                progress = True
        if not progress:
            break
    strata_counts = Counter({f"{k[0]}|{k[1]}|{k[2]}": depth[k] for k in keys if depth[k]})
    picked.sort(key=lambda r: (r["timestamp"], r["record_id"]))
    return picked, strata_counts


def build_context(streams: dict, center, before_n: int, after_n: int, pos_index: dict):
    seq = streams[center["conversation_id"]]
    pos = pos_index[center["record_id"]]
    lo = max(0, pos - before_n)
    hi = min(len(seq), pos + after_n + 1)
    ctx = []
    for i in range(lo, hi):
        r = seq[i]
        ctx.append({
            "record_id": r["record_id"],
            "person_id": r["person_id"],
            "timestamp": r["timestamp"],
            "text": r["text"],
            "center": i == pos,
        })
    return ctx


def fmt_local(iso_utc: str, tz: ZoneInfo, with_date: bool = True) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(tz)
    return dt.strftime("%m-%d %H:%M" if with_date else "%H:%M")


def render_md(alias: str, samples: list, coverage: dict, tz: ZoneInfo) -> str:
    style_n = sum(1 for s in samples if s.get("eligible_for_style", True))
    lines = [
        f"# 抽样阅读材料 · {alias}",
        "",
        f"- 中心样本：{coverage['sampled']} 条（该成员有效消息 {coverage['total']} 条；"
        f"{'全量覆盖' if coverage['sampled'] >= coverage['total'] else '已抽样'}）",
        f"- 风格可用样本：{style_n}/{len(samples)}；带 `〔转发/模板〕` 标记的句子为跨成员转发、自我模板、跨群转发或 AI 摘要，仅作兴趣参考。",
        f"- 评估留出：最近 {coverage['holdout_days']} 天内样本标为 holdout",
        "- 标记说明：`>>>` 为中心消息；上下文为同会话相邻消息；人名/链接/号码已脱敏替换为别名。",
        "",
    ]
    for i, s in enumerate(samples, 1):
        head = (
            f"## {i:03d} · {fmt_local(s['timestamp'], tz)} · {s['conversation_id']} · "
            f"{s['strata']['band']}({len(s['text'])}字) · {'回复' if s['strata']['reply'] == 'reply' else '发言'} · {s['split']}"
        )
        lines.append(head)
        for c in s["context"]:
            if c["center"]:
                mark = ">>> " if s.get("eligible_for_style", True) else ">>> 〔转发/模板〕 "
            else:
                mark = "- "
            lines.append(f"{mark}{c['person_id']} [{fmt_local(c['timestamp'], tz)}] {c['text']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_posts_reading(posts_path: Path) -> tuple[str, int, int]:
    """将非草稿博客语料整理为可读材料（供写作观察）；返回 (markdown, 文章数, 记录数)。"""
    if not posts_path.exists():
        return "", 0, 0
    rows = [json.loads(line) for line in posts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    docs: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get("draft"):
            continue
        docs[r["source_id"] + ":" + r["record_id"].split(":post-", 1)[1].split(":", 1)[0]].append(r)
    lines = ["# 博客阅读材料（非草稿）", "", "仅保留正文段落/引用/列表/图片引用；代码块与草稿不进入本材料。", ""]
    n_docs = 0
    n_records = 0
    for doc_key in sorted(docs):
        recs = sorted(docs[doc_key], key=lambda r: r["record_id"])
        first = recs[0]
        n_docs += 1
        lines.append(f"## {first.get('title')}")
        lines.append("")
        if first.get("published_at"):
            lines.append(f"*发布时间：{first['published_at']}*")
            lines.append("")
        last_section = None
        for r in recs:
            if r["content_kind"] not in ("paragraph", "quote", "list", "image_ref"):
                continue
            n_records += 1
            if r.get("section_path") and r["section_path"] != last_section:
                lines.append(f"### {r['section_path']}")
                lines.append("")
                last_section = r["section_path"]
            if r["content_kind"] == "image_ref":
                lines.append(f"（图片引用：{r['text'] or '无描述'}）")
            elif r["content_kind"] == "quote":
                lines.append("> " + r["text"].replace("\n", "\n> "))
            else:
                lines.append(r["text"])
            lines.append("")
    return "\n".join(lines) + "\n", n_docs, n_records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ohana 语料分层抽样（本地离线）")
    parser.add_argument("--input", default="analysis/corpus", help="01 脚本产出的语料目录")
    parser.add_argument("--output", default="analysis/samples", help="抽样输出目录")
    parser.add_argument("--config", default="config/analysis.local.yaml", help="本地分析配置")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（默认取配置 sampling.seed）")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]

    def resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (root / pp)

    corpus_dir = resolve(args.input)
    out_dir = resolve(args.output)
    cfg_path = resolve(args.config)
    if not (corpus_dir / "members").is_dir():
        raise SystemExit(f"找不到语料目录：{corpus_dir}/members（请先运行 01_preprocess.py）")
    cfg = load_config(cfg_path)
    tz = ZoneInfo(cfg.get("timezone") or "Asia/Shanghai")
    sampling = cfg.get("sampling") or {}
    seed = args.seed if args.seed is not None else int(sampling.get("seed", 42))
    max_n = int(sampling.get("max_samples_per_member", 200))
    overrides = {str(k): int(v) for k, v in (sampling.get("member_overrides") or {}).items()}
    before_n = int(sampling.get("context_before", 3))
    after_n = int(sampling.get("context_after", 3))
    holdout_days = int(sampling.get("holdout_days", 30))

    log(f"[02] 语料：{corpus_dir}")
    member_rows, streams, meta = load_corpus(corpus_dir)
    total_rows = sum(len(v) for v in member_rows.values())
    log(f"[02] 成员 {len(member_rows)} 人，记录 {total_rows:,} 条，会话 {len(streams)} 个；seed={seed}，上限 {max_n}/人"
        + (f"，覆盖：{overrides}" if overrides else ""))

    # 全局最近时间 → holdout 边界
    all_ts = [r["timestamp"] for rows in member_rows.values() for r in rows]
    if not all_ts:
        raise SystemExit("语料为空，无法抽样。")
    latest = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in all_ts)
    holdout_from = (latest - timedelta(days=holdout_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    pos_index: dict[str, int] = {}
    for conv, seq in streams.items():
        for i, r in enumerate(seq):
            pos_index[r["record_id"]] = i

    out_dir.mkdir(parents=True, exist_ok=True)
    stale = {f.stem for f in (out_dir.glob("*.jsonl"))} | {f.stem for f in (out_dir.glob("*.md"))}
    manifest_members: dict[str, dict] = {}
    record_lists: dict[str, list] = {}
    outputs: list[dict] = []
    digest_inputs = []

    for alias in sorted(member_rows):
        rows = member_rows[alias]
        if not rows:
            continue
        cap = overrides.get(alias, max_n)
        picked, strata_counts = select_samples(rows, cap, seed, alias, tz)
        samples = []
        holdout_n = 0
        for r in picked:
            split = "holdout" if r["timestamp"] >= holdout_from else "analysis"
            holdout_n += split == "holdout"
            dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).astimezone(tz)
            samples.append({
                "schema_version": SCHEMA_VERSION,
                "center_record_id": r["record_id"],
                "person_id": alias,
                "conversation_id": r["conversation_id"],
                "timestamp": r["timestamp"],
                "text": r["text"],
                "strata": {
                    "month": dt.strftime("%Y-%m"),
                    "band": length_band(len(r.get("text") or "")),
                    "reply": "reply" if (r.get("reply_to") or "reply_unresolved" in (r.get("flags") or [])) else "talk",
                },
                "split": split,
                "flags": r.get("flags") or [],
                "eligible_for_style": bool(r.get("eligible_for_style", True)),
                "context": build_context(streams, r, before_n, after_n, pos_index),
            })
        coverage = {
            "sampled": len(samples),
            "total": len(rows),
            "complete": len(rows) <= cap,
            "holdout_days": holdout_days,
            "holdout": holdout_n,
        }
        jl = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples)
        md = render_md(alias, samples, coverage, tz)
        write_text_atomic(out_dir / f"{alias}.jsonl", jl)
        write_text_atomic(out_dir / f"{alias}.md", md)
        outputs.append({"path": f"{alias}.jsonl", "sha256": sha256_bytes(jl.encode("utf-8")), "samples": len(samples)})
        outputs.append({"path": f"{alias}.md", "sha256": sha256_bytes(md.encode("utf-8")), "samples": len(samples)})
        record_lists[alias] = [s["center_record_id"] for s in samples]
        manifest_members[alias] = {
            "sampled": len(samples),
            "total": len(rows),
            "cap": cap,
            "style_eligible": sum(1 for s in samples if s.get("eligible_for_style", True)),
            "coverage_gap": len(samples) < cap,  # 未达到配额即全取并标记缺口
            "holdout": holdout_n,
            "strata": dict(sorted(strata_counts.items())),
        }
        stale.discard(alias)
        digest_inputs.append(f"{alias}:{sha256_file(corpus_dir / 'members' / (alias + '.jsonl'))}")

    # 成员语料有更新时，清理旧的样本文件
    for name in sorted(stale):
        for ext in (".jsonl", ".md"):
            p = out_dir / (name + ext)
            if p.exists():
                p.unlink()

    # 博客阅读材料（写作风格观察用）
    posts_md, posts_docs, posts_records = render_posts_reading(corpus_dir / "posts.jsonl")
    if posts_md:
        write_text_atomic(out_dir / "posts_reading.md", posts_md)
        outputs.append({"path": "posts_reading.md", "sha256": sha256_bytes(posts_md.encode("utf-8")), "samples": None})

    corpus_digest = sha256_bytes("\n".join(sorted(digest_inputs)).encode("utf-8"))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "script": {"name": "02_sample.py", "version": SCRIPT_VERSION},
        "seed": seed,
        "params": {
            "max_samples_per_member": max_n,
            "member_overrides": overrides,
            "context_before": before_n,
            "context_after": after_n,
            "holdout_days": holdout_days,
            "length_bands": [[lo, hi, name] for lo, hi, name in LENGTH_BANDS],
        },
        "config": {"path": str(cfg_path), "sha256": sha256_file(cfg_path) if cfg_path.exists() else None},
        "corpus_digest": corpus_digest,
        "holdout_from": holdout_from,
        "members": manifest_members,
        "center_record_ids": record_lists,
        "posts_reading": {"documents": posts_docs, "records": posts_records},
        "outputs": outputs,
    }
    write_json_atomic(out_dir / "manifest.json", manifest)

    total_sampled = sum(v["sampled"] for v in manifest_members.values())
    log(f"[02] 完成：{total_sampled:,} 个中心样本 → {out_dir}")
    log(f"[02] 语料摘要 corpus_digest={corpus_digest[:12]}；seed={seed}（重复运行结果稳定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
