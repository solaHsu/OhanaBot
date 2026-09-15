#!/usr/bin/env python3
"""Ohana 资料管线第一步：聊天导出与博客的归一化、过滤、脱敏、统计。

用法：
    python scripts/01_preprocess.py --input content --output analysis --config config/analysis.local.yaml

设计依据 docs/data-pipeline.md：
    - 原始文件只读，枚举并计算 SHA-256；
    - 解析 QQChatExporter 结构，坏记录隔离，严重结构错误使本轮失败；
    - 按会话/时间稳定排序，去重仅按（会话, 消息ID），正常复读保留；
    - 排除系统、撤回、机器人消息；纯媒体保留类型计数，不进入语言语料；
    - 回复引用不计入作者风格；人名替换为稳定匿名别名，链接/路径/已知号码脱敏；
    - 产物写入临时目录后切换，重复运行结果稳定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import jieba

    jieba.setLogLevel(50)  # 关闭 jieba 构建日志
    JIEBA_VERSION = getattr(jieba, "__version__", "unknown")
except Exception:  # pragma: no cover - 保底分词
    jieba = None
    JIEBA_VERSION = None

SCHEMA_VERSION = 1
SCRIPT_VERSION = "0.1.0"
STOPWORDS_VERSION = "builtin-v1"
NAME_RULES_VERSION = "v1"

# 内置停用词（高频虚词/语气词），仅用于高频词统计，不影响口头禅候选的原文。
STOPWORDS = frozenset(
    "的 了 是 我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些 什么 怎么 为什么 因为 所以 但是 而且 然后 "
    "就是 还是 也是 不是 没有 一个 一下 可以 自己 现在 时候 已经 应该 可能 觉得 知道 感觉 真的 太 都 也 又 还 就 才 "
    "很 挺 比较 非常 特别 有点 一些 啊 吧 呢 吗 呀 哦 噢 嗯 哈 嘿 诶 哎 唉 额 呃 哇 啦 嘛 喔 唉 之 其 与 及 或 等 "
    "被 把 让 给 对 从 到 在 有 没 不 会 能 想 去 来 说 做 个 只 和 跟 于 而 则 且 并 但 却 若 如果 于是 由于 对于 "
    "关于 通过 按照 为了 以及 或者 不过 其实 反正 确实 估计 大概 差不多 一下 一样 一点 一直 一定 不要 不会 不用 "
    "怎么 这么 那么 多少 几个 第一 第二 最后 之前 之后 以后 以前 今天 明天 昨天 早上 中午 晚上 哈哈 哈哈哈 the a an is are".split()
)

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None)

URL_RE = re.compile(r"(?:https?|ftp|mqqapi|tencent)://[^\s]+|www\.[^\s]+", re.IGNORECASE)
# 裸域名链接（无协议头，如 pan.baidu.com/s/xxx）：需带路径以降低误报
BARE_DOMAIN_RE = re.compile(
    r"(?<![A-Za-z0-9._-])(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org|tv|me|io|cc|top|xyz|app|dev|ac|edu)/[^\s，。！？、；：\"'）】]*",
    re.IGNORECASE,
)
# 中国大陆手机号（11 位，1[3-9] 开头）
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WINPATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s，。！？、；：\"'）】]*")
UNIXPATH_RE = re.compile(r"(?<![\w])/(?:home|root|Users|mnt|media|tmp|opt|var|etc|usr)/[^\s，。！？、；：\"'）】]*")
TOKEN_RE = re.compile(r"(?<![\w-])[A-Za-z0-9_+/=-]{32,}(?![\w-])")
SPACE_RE = re.compile(r"\s+")
RUN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
CJK_ONLY_RE = re.compile(r"^[\u4e00-\u9fff]+$")
WORD_RE = re.compile(r"^(?:[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#._-]{1,15})$")
PUNCT_CHARS = set("，。！？、；：＂＇（）【】《》〈〉「」『』…—～··,.!?;:\"'()[]<>~-+=*&^%$#\\/|`")
SPECIAL_PATTERNS = {
    "laugh_ha2": re.compile(r"哈{2,}"),
    "six_6x2": re.compile(r"6{2,}"),
    "yyds_233": re.compile(r"233+"),
    "grass": re.compile(r"草"),
    "smile_face": re.compile(r"\[微笑\]|\[呲牙\]|\[憨笑\]"),
    "end_q": re.compile(r"[?？]$"),
    "end_excl": re.compile(r"[!！]$"),
    "pure_punct": re.compile(r"^[\s\W_]+$"),
}

LANG_ELEMENT_TYPES = {"text", "at", "face", "market_face"}
PLACEHOLDERS = ("[链接]", "[路径]", "[邮箱]", "[令牌]", "[号码]", "[私隐]", "[图片]", "[表情]", "[文件]", "[视频]", "[卡片]", "[转发]", "[语音]")

# 备注中不应被当作人名的专业/组织类词（避免把“21-软件-刘涛”中的“软件”当人名）
NON_NAME_WORDS = frozenset({
    "软件", "计科", "工科", "工商", "工管", "管理", "学院", "大学", "学校", "专业", "实验", "创新", "国际",
    "信息", "电科", "通信", "自动化", "土木", "机械", "数学", "统计", "英语", "会计", "金融", "经济", "法学",
    "医学", "艺术", "设计", "师范", "体育", "传媒", "新闻", "网络", "智能", "计算机", "数媒", "物联网",
    "大数据", "人工智能", "软件工程", "工程", "科学", "技术", "方向", "班级", "年级",
})


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


def norm_text(s: str) -> str:
    """NFKC 归一化并移除零宽字符。"""
    return unicodedata.normalize("NFKC", s.translate(ZERO_WIDTH))


def name_key(s: str) -> str:
    """名称匹配键：NFKC + casefold + 去除空白与零宽。"""
    return SPACE_RE.sub("", norm_text(s).casefold())


def mask_uin(uin: str | None) -> str:
    if not uin:
        return "-"
    if len(uin) <= 4:
        return "****"
    return "****" + uin[-4:]


def fmt_local(iso_utc: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")


def as_utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit(f"配置无法解析：{path}")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"配置 schema_version 不匹配：{cfg.get('schema_version')}")
    for uin in cfg.get("candidates") or {}:
        if not isinstance(uin, str) or not uin.isdigit():
            raise SystemExit(f"candidates 的键必须是字符串形式的 QQ 号：{uin!r}（禁止浮点转换）")
    for bot in cfg.get("bots") or []:
        for k in ("uin", "uid"):
            if bot.get(k) is not None and not isinstance(bot[k], str):
                raise SystemExit(f"bots 的 {k} 必须是字符串：{bot!r}")
    return cfg


class IdentityBook:
    """本地身份别名表：QQ号/UID → 稳定匿名别名，持久化供重复运行复用。"""

    def __init__(self, path: Path, candidates: dict, bots: list):
        self.path = path
        self.candidates = candidates or {}
        self.bots = bots or []
        self.bot_uins = {b["uin"] for b in self.bots if b.get("uin")}
        self.bot_uids = {b["uid"] for b in self.bots if b.get("uid")}
        self.bot_name_by_key = {}
        for b in self.bots:
            key = self._key(b.get("uin"), b.get("uid"))
            self.bot_name_by_key[key] = b.get("name") or ""
        self.persons = {}       # key -> record
        self.external = {}      # key -> record
        self.conversations = {}  # peer -> record
        self.loaded = False

    @staticmethod
    def _key(uin, uid):
        if uin and str(uin) not in ("", "0"):
            return f"uin:{uin}"
        if uid:
            return f"uid:{uid}"
        return None

    def is_bot(self, uin, uid) -> bool:
        return (uin and uin in self.bot_uins) or (uid and uid in self.bot_uids)

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != SCHEMA_VERSION:
                raise SystemExit(f"身份映射 schema_version 不匹配：{self.path}")
            self.persons = data.get("persons", {})
            for rec in self.persons.values():
                rec["variants"] = defaultdict(set, {k: set(v) for k, v in (rec.get("variants") or {}).items()})
                rec["conversations"] = set(rec.get("conversations") or [])
            self.external = data.get("external_targets", {})
            for rec in self.external.values():
                rec["variants"] = defaultdict(set, {k: set(v) for k, v in (rec.get("variants") or {}).items()})
            self.conversations = data.get("conversations", {})
            self.loaded = True

    def collect_person(self, uid, uin, names: dict) -> None:
        """names: {"name": set(), "nickname": set(), "card": set(), "remark": set()}"""
        key = self._key(uin, uid)
        if key is None:
            return
        rec = self.persons.setdefault(
            key,
            {"alias": None, "uin": uin or None, "uid": uid or None, "variants": defaultdict(set), "conversations": set()},
        )
        if uin and not rec.get("uin"):
            rec["uin"] = uin
        if uid and not rec.get("uid"):
            rec["uid"] = uid
        for tag, vals in names.items():
            for v in vals:
                if v:
                    rec["variants"][tag].add(v.strip())

    def collect_external(self, uid, uin, name, source_tag="mention") -> None:
        key = self._key(uin, uid)
        if key is None:
            return
        rec = self.external.setdefault(key, {"alias": None, "uin": uin or None, "uid": uid or None, "variants": defaultdict(set), "mention_count": 0})
        if name:
            rec["variants"][source_tag].add(name.strip())

    def collect_conversation(self, peer_id, name, kind="group") -> None:
        rec = self.conversations.setdefault(str(peer_id), {"alias": None, "name": name, "kind": kind})
        if name:
            rec["name"] = name

    def finalize(self) -> None:
        """为未分配的对象分配别名：候选成员优先，其余按 QQ 号数值排序，保证首次运行可复现。"""
        # 候选人标记（保存、索引与报告共用，首轮运行也能正确生效）
        for rec in self.persons.values():
            rec["candidate"] = bool(rec.get("uin") and rec["uin"] in self.candidates)

        # 机器人永不应出现在成员或外部目标中（名单可能后补）
        for key in list(self.persons):
            rec = self.persons[key]
            if self.is_bot(rec.get("uin"), rec.get("uid")):
                del self.persons[key]
        for key in list(self.external):
            rec = self.external[key]
            if self.is_bot(rec.get("uin"), rec.get("uid")):
                del self.external[key]

        # 合并同一账号被拆成多个键的外部目标（uid/uin 交叉对应）
        uid_index: dict = {}
        uin_index: dict = {}
        for key in sorted(self.external):
            rec = self.external[key]
            uid, uin = rec.get("uid"), rec.get("uin")
            target = None
            if uid and uid in uid_index:
                target = uid_index[uid]
            elif uin and uin in uin_index:
                target = uin_index[uin]
            if target is None:
                if uid:
                    uid_index[uid] = key
                if uin:
                    uin_index[uin] = key
                continue
            base = self.external[target]
            for tag, vals in rec["variants"].items():
                base["variants"][tag] |= set(vals)
            if not base.get("uid") and uid:
                base["uid"] = uid
                uid_index.setdefault(uid, target)
            if not base.get("uin") and uin:
                base["uin"] = uin
                uin_index.setdefault(uin, target)
            del self.external[key]

        # 提及目标若可解析为成员，不建立独立身份（去重）。
        # 注意：此时别名尚未分配，不能用 alias 作过滤条件。
        uid_set = {r.get("uid") for r in self.persons.values() if r.get("uid")}
        uin_set = {r.get("uin") for r in self.persons.values() if r.get("uin")}
        self._name_index_cache = None
        idx = self._name_index()
        for key in list(self.external.keys()):
            if key in self.persons:
                del self.external[key]
                continue
            rec = self.external[key]
            if rec.get("uid") in uid_set or rec.get("uin") in uin_set:
                del self.external[key]
                continue
            names = [v for vals in rec["variants"].values() for v in vals]
            if any(name_key(v) in idx for v in names):
                del self.external[key]

        used = {rec["alias"] for rec in self.persons.values() if rec.get("alias")}
        used |= {rec["alias"] for rec in self.external.values() if rec.get("alias")}
        next_idx = len(used) + 1

        def assign(records, prefix, order):
            nonlocal next_idx
            for key in order:
                rec = records.get(key)
                if rec is None or rec.get("alias"):
                    continue
                while f"{prefix}{next_idx:02d}" in used:
                    next_idx += 1
                rec["alias"] = f"{prefix}{next_idx:02d}"
                used.add(rec["alias"])
                next_idx += 1

        def sort_key(key):
            uin = key.split(":", 1)[1] if key.startswith("uin:") else None
            if uin and (key.startswith("uin:") and key.split(":", 1)[1].isdigit()):
                return (0, int(uin), key)
            return (1, 0, key)

        candidate_keys = [k for k, rec in self.persons.items() if rec.get("uin") and rec["uin"] in self.candidates and not rec.get("alias")]
        candidate_keys.sort(key=lambda k: list(self.candidates.keys()).index(self.persons[k]["uin"]))
        assign(self.persons, "m", candidate_keys + sorted([k for k in self.persons if k not in candidate_keys], key=sort_key))
        assign(self.external, "x", sorted(self.external, key=sort_key))
        conv_order = sorted(self.conversations, key=lambda p: (0, int(p)) if p.isdigit() else (1, 0))
        next_conv = 1
        used_conv = {rec["alias"] for rec in self.conversations.values() if rec.get("alias")}
        for peer in conv_order:
            rec = self.conversations[peer]
            if rec.get("alias"):
                continue
            while f"g{next_conv:02d}" in used_conv:
                next_conv += 1
            rec["alias"] = f"g{next_conv:02d}"
            used_conv.add(rec["alias"])
            next_conv += 1

    def save(self) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "persons": {
                k: {
                    "alias": rec["alias"],
                    "uin": rec.get("uin"),
                    "uid": rec.get("uid"),
                    "variants": {tag: sorted(vals) for tag, vals in rec["variants"].items()},
                    "conversations": sorted(rec.get("conversations", [])),
                    "candidate": bool(rec.get("uin") and rec["uin"] in self.candidates),
                }
                for k, rec in sorted(self.persons.items())
            },
            "external_targets": {
                k: {
                    "alias": rec["alias"],
                    "uin": rec.get("uin"),
                    "uid": rec.get("uid"),
                    "variants": {tag: sorted(vals) for tag, vals in rec["variants"].items()},
                    "mention_count": rec.get("mention_count", 0),
                }
                for k, rec in sorted(self.external.items())
            },
            "conversations": {
                k: {"alias": rec["alias"], "name": rec.get("name"), "kind": rec.get("kind")}
                for k, rec in sorted(self.conversations.items())
            },
            "bots": [
                {"uin": b.get("uin"), "uid": b.get("uid"), "name": b.get("name"), "excluded": b.get("excluded", 0), "at_count": b.get("at_count", 0)}
                for b in sorted(self.bots, key=lambda b: b.get("uin") or "")
            ],
        }
        write_json_atomic(self.path, data)

    # ---- 查询 ----
    def resolve(self, uid=None, uin=None, name=None) -> tuple[str | None, str | None]:
        """解析到成员别名。返回 (alias, method)。"""
        if uin and str(uin) not in ("", "0"):
            rec = self.persons.get(f"uin:{uin}")
            if rec and rec.get("alias"):
                return rec["alias"], "uin"
        if uid:
            rec = self.persons.get(f"uid:{uid}")
            if rec and rec.get("alias"):
                return rec["alias"], "uid"
        if name:
            rec = self.persons.get(self._name_index().get(name_key(name)) or "")
            if rec and rec.get("alias"):
                return rec["alias"], "name"
        return None, None

    def _name_index(self) -> dict:
        """名称键 → 成员键（不含别名；按排序迭代保证首见顺序确定）。"""
        if getattr(self, "_name_index_cache", None) is None:
            idx = {}
            for key in sorted(self.persons):
                rec = self.persons[key]
                for tag in sorted(rec["variants"]):
                    for v in sorted(rec["variants"][tag]):
                        idx.setdefault(name_key(v), key)
            self._name_index_cache = idx
        return self._name_index_cache

    def resolve_external(self, uid=None, uin=None, name=None) -> tuple[str | None, str | None]:
        if uin and str(uin) not in ("", "0"):
            rec = self.external.get(f"uin:{uin}")
            if rec and rec.get("alias"):
                return rec["alias"], "uin"
        if uid:
            rec = self.external.get(f"uid:{uid}")
            if rec and rec.get("alias"):
                return rec["alias"], "uid"
        if name:
            if getattr(self, "_ext_name_cache", None) is None:
                idx = {}
                for key in sorted(self.external):
                    rec = self.external[key]
                    for tag in sorted(rec["variants"]):
                        for v in sorted(rec["variants"][tag]):
                            idx.setdefault(name_key(v), rec["alias"])
                self._ext_name_cache = idx
            hit = self._ext_name_cache.get(name_key(name))
            if hit:
                return hit, "name"
        return None, None

    def note_external_mention(self, alias: str) -> None:
        if getattr(self, "_ext_by_alias", None) is None:
            self._ext_by_alias = {rec["alias"]: rec for rec in self.external.values() if rec.get("alias")}
        rec = self._ext_by_alias.get(alias)
        if rec is not None:
            rec["mention_count"] = rec.get("mention_count", 0) + 1


def build_name_rules(idb: IdentityBook):
    """生成自由文本中的姓名替换规则。

    规则（NAME_RULES_VERSION=v1）：
    - 备注中提取的真实姓名：一律替换；
    - 含非中日韩字符的昵称/群名片：替换（辨识度高）；
    - 纯中文昵称/群名片：长度 >= 5 才替换（避免误伤常用短语，残余风险在统计报告中说明）；
    - 机器人名称 → bot；群名称 → 群别名。
    返回 [(variant_norm, replacement, tag), ...]，按长度降序。
    """
    def add(variant, replacement, tag, always=False):
        if not variant:
            return
        v = norm_text(variant).strip()
        if len(v) < 2 or v.isdigit():
            return
        if not always:
            if CJK_ONLY_RE.match(v) and len(v) < 5:
                return
        entries.append((v, replacement, tag))

    def remark_name(remark: str) -> str | None:
        """从备注中提取真实姓名：取首个非组织类的中文短词（如“21-软件-刘涛” → “刘涛”）。"""
        for t in re.split(r"[\s,，、/|·・()（）\[\]【】]+|-+", remark):
            t = t.strip()
            if not t or t in NON_NAME_WORDS:
                continue
            m = re.match(r"^([\u4e00-\u9fff]{2,4})(?:同学|老师|会长|队长|学长|学姐|老板|师傅)$", t)
            if m:
                return t
            if 2 <= len(t) <= 5 and CJK_ONLY_RE.match(t) and t not in NON_NAME_WORDS:
                return t
        return None

    entries: list = []
    for _key, rec in sorted(idb.persons.items()):
        alias = rec.get("alias")
        if not alias:
            continue
        variants = rec["variants"]
        combined = set(variants.get("name") or []) | set(variants.get("nickname") or []) | set(variants.get("card") or [])
        for v in sorted(combined):
            add(v, alias, "display")
        for r in sorted(variants.get("remark") or []):
            add(r, alias, "remark")
            name = remark_name(r)
            if name:
                add(name, alias, "real_name", always=True)
                stripped = re.match(r"^([\u4e00-\u9fff]{2,4})(?:同学|老师|会长|队长|学长|学姐|老板|师傅)$", name)
                if stripped:
                    add(stripped.group(1), alias, "real_name", always=True)
    for b in sorted(idb.bots, key=lambda x: (x.get("uin") or "", x.get("name") or "")):
        add(b.get("name") or "", "bot", "bot", always=True)
    for _peer, rec in sorted(idb.conversations.items()):
        add(rec.get("name") or "", rec.get("alias") or "g", "conversation", always=True)

    # 同名变体指向不同对象时删除，避免不确定的替换
    resolved: dict = {}
    ambiguous: set = set()
    for v, replacement, tag in entries:
        key = v.casefold()
        prev = resolved.get(key)
        if prev is None:
            resolved[key] = (v, replacement, tag)
        elif prev[1] != replacement:
            ambiguous.add(key)
    for key in ambiguous:
        resolved.pop(key, None)
    rules = sorted(resolved.values(), key=lambda item: (-len(item[0]), item[0], item[1]))
    return rules


class Redactor:
    """文本脱敏：链接、路径、令牌、已知号码、隐私词表、人名别名替换。"""

    def __init__(self, lexicon_entries, name_rules, known_numbers):
        self.hits = Counter()
        self.lexicon_pattern = None
        if lexicon_entries:
            pattern = "|".join(re.escape(norm_text(e)) for e in sorted(set(lexicon_entries), key=len, reverse=True))
            self.lexicon_pattern = re.compile(pattern, re.IGNORECASE)
        self.name_pattern = None
        self.name_ascii_pattern = None
        self.name_map = {}
        if name_rules:
            # 纯 ASCII 变体（如短昵称 nc）加词边界，避免误伤普通英文单词（sentence/openclaw）
            ascii_rules = [(v, r, t) for v, r, t in name_rules if v.isascii()]
            other_rules = [(v, r, t) for v, r, t in name_rules if not v.isascii()]
            if other_rules:
                self.name_pattern = re.compile("|".join(re.escape(v) for v, _, _ in other_rules), re.IGNORECASE)
            if ascii_rules:
                alts = "|".join(re.escape(v) for v, _, _ in ascii_rules)
                self.name_ascii_pattern = re.compile(r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % alts, re.IGNORECASE)
            self.name_map = {v.casefold(): (r, t) for v, r, t in name_rules}
        nums = sorted({n for n in known_numbers if n and n.isdigit()}, key=len, reverse=True)
        self.number_pattern = re.compile(r"(?<!\d)(?:%s)(?!\d)" % "|".join(re.escape(n) for n in nums)) if nums else None

    @staticmethod
    def _trim_url(match: re.Match) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip("，。！？、；：）】}>」』\"'")
        return "[链接]" + raw[len(trimmed):]

    def redact(self, text: str, keep_urls: bool = False) -> tuple[str, list[str], Counter]:
        """返回 (脱敏后文本, 记录 flags, 本轮命中计数)。keep_urls 用于博客公开内容。"""
        local_hits = Counter()
        flags = []
        t = norm_text(text)
        if not t:
            return t, flags, local_hits

        def sub_count(pattern, repl, key):
            nonlocal t
            t, n = pattern.subn(repl, t)
            if n:
                local_hits[key] += n
            return n

        if not keep_urls:
            sub_count(URL_RE, self._trim_url, "url")
            sub_count(BARE_DOMAIN_RE, "[链接]", "url")
        sub_count(EMAIL_RE, "[邮箱]", "email")
        sub_count(WINPATH_RE, "[路径]", "path")
        sub_count(UNIXPATH_RE, "[路径]", "path")
        sub_count(TOKEN_RE, "[令牌]", "token")
        if self.number_pattern:
            n = sub_count(self.number_pattern, "[号码]", "known_number")
            if n:
                flags.append("known_number_redacted")
        sub_count(PHONE_RE, "[电话]", "phone")
        if self.lexicon_pattern:
            n = sub_count(self.lexicon_pattern, "[私隐]", "lexicon")
            if n:
                flags.append("privacy_lexicon")
        if self.name_pattern or self.name_ascii_pattern:
            def name_repl(m):
                key = m.group(0).casefold()
                repl, tag = self.name_map.get(key, ("[人]", "name"))
                self.hits[f"name_{tag}"] += 1
                return repl
            if self.name_pattern:
                t, n = self.name_pattern.subn(name_repl, t)
                if n:
                    local_hits["name"] += n
            if self.name_ascii_pattern:
                t, n = self.name_ascii_pattern.subn(name_repl, t)
                if n:
                    local_hits["name"] += n
        for k, v in local_hits.items():
            self.hits[k] += v
        return t, flags, local_hits

    def residual_risk(self, text: str) -> str | None:
        """脱敏后的残余风险扫描；返回命中的类别名或 None。"""
        if URL_RE.search(text) or BARE_DOMAIN_RE.search(text) or WINPATH_RE.search(text) or EMAIL_RE.search(text):
            return "url_or_path"
        if PHONE_RE.search(text):
            return "phone"
        if self.number_pattern and self.number_pattern.search(text):
            return "known_number"
        if TOKEN_RE.search(text):
            return "token"
        return None


def load_lexicon(path: Path) -> list[str]:
    """隐私词表：每行一个字面项，井号开头为注释，反斜杠加井号表示字面井号。"""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("\\#"):
            s = s[1:]
        entries.append(s)
    return entries


# ---------------------------------------------------------------------------
# 聊天导出：解析、扫描（Pass A）与处理（Pass B）
# ---------------------------------------------------------------------------

MEDIA_LABEL = {
    "image": "图片",
    "sticker_image": "表情图",
    "video": "视频",
    "audio": "语音",
    "audio_call": "语音通话",
    "file": "文件",
    "json": "卡片/小程序",
    "forward": "转发",
    "wallet": "红包",
    "markdown": "Markdown消息",
}


def scan_chat_source(path: Path, source_id: str, idb: IdentityBook) -> dict:
    """Pass A：收集成员、名称变体、会话与提及目标，并校验结构。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("metadata") or {}
    if not str(meta.get("name", "")).startswith("QQChatExporter"):
        raise SystemExit(f"[{source_id}] 未识别的导出器：{meta.get('name')!r}（要求 QQChatExporter）")
    chat = data.get("chatInfo") or {}
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        raise SystemExit(f"[{source_id}] 严重结构错误：messages 不是数组：{path}")
    if (chat.get("type") or "") != "group":
        raise SystemExit(f"[{source_id}] 首版仅支持群聊导出（当前 type={chat.get('type')!r}）")
    peer = str(chat.get("peerUid") or "").strip()
    if not peer:
        raise SystemExit(f"[{source_id}] 缺少 chatInfo.peerUid")
    idb.collect_conversation(peer, chat.get("name") or "")

    for m in msgs:
        if not isinstance(m, dict):
            continue
        s = m.get("sender") or {}
        uid, uin = s.get("uid"), s.get("uin")
        if uid == "未知":
            uid = None
        if idb.is_bot(uin, uid):
            continue
        if not uin and not uid:
            continue  # 无身份的系统消息（如“系统消息”）
        idb.collect_person(
            uid,
            uin,
            {
                "name": {s.get("name") or ""},
                "nickname": {s.get("nickname") or ""},
                "card": {s.get("groupCard") or ""},
                "remark": {s.get("remark") or ""},
            },
        )
        content = m.get("content") or {}
        for el in content.get("elements") or []:
            if isinstance(el, dict) and el.get("type") == "at":
                d = el.get("data") or {}
                if (d.get("name") or "") == "全体成员" or idb.is_bot(d.get("uin"), d.get("uid")):
                    continue
                idb.collect_external(d.get("uid"), d.get("uin"), d.get("name") or "")
        for men in content.get("mentions") or []:
            if not isinstance(men, dict):
                continue
            if (men.get("name") or "") == "全体成员" or idb.is_bot(men.get("uin"), men.get("uid")):
                continue
            idb.collect_external(men.get("uid"), men.get("uin"), men.get("name") or "")

    stats_block = data.get("statistics") or {}
    return {
        "source_id": source_id,
        "kind": "chat_export",
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "exporter": str(meta.get("name") or ""),
        "exporter_version": str(meta.get("version") or ""),
        "conversation": idb.conversations[peer]["alias"],
        "messages_raw": len(msgs),
        "declared_total": stats_block.get("totalMessages"),
        "declared_range": (stats_block.get("timeRange") or {}),
    }


def extract_elements(m: dict):
    """解析一条消息的元素，返回（segments, media, ats, faces, stickers, reply_data, has_non_lang）。"""
    content = m.get("content") or {}
    elements = [el for el in (content.get("elements") or []) if isinstance(el, dict)]
    segments: list[dict] = []
    media = Counter()
    ats: list[dict] = []
    faces: list[str] = []
    stickers: list[str] = []
    reply_data = None
    has_non_lang = False
    for el in elements:
        t = el.get("type")
        d = el.get("data") or {}
        if t == "text":
            txt = d.get("text") or ""
            if txt:
                segments.append({"type": "text", "text": txt})
        elif t == "at":
            ats.append(d)
        elif t == "face":
            name = d.get("name") or "/表情"
            faces.append(name)
            segments.append({"type": "face", "name": name})
        elif t == "market_face":
            name = d.get("name") or "[表情包]"
            stickers.append(name)
            segments.append({"type": "sticker", "name": name})
        elif t == "reply":
            reply_data = d
        elif t in ("image", "video", "file", "audio", "json", "forward", "wallet", "markdown", "av_record", "system"):
            key = "audio_call" if t == "av_record" else t
            if t == "image" and (d.get("subType") or "") == "sticker":
                key = "sticker_image"
            media[key] += 1
            has_non_lang = True
        else:
            media["other"] += 1
            has_non_lang = True
    return segments, media, ats, faces, stickers, reply_data, has_non_lang


def render_segments(segments: list[dict]) -> str:
    parts = []
    for s in segments:
        t = s.get("type")
        if t == "text":
            parts.append(s.get("text", ""))
        elif t == "at":
            if s.get("person"):
                parts.append("@" + s["person"])
            elif s.get("bot"):
                parts.append("@bot")
            elif s.get("group") == "all":
                parts.append("@全体")
            else:
                parts.append("@" + (s.get("label") or "未知"))
        elif t in ("face", "sticker"):
            parts.append(s.get("name", ""))
    return "".join(parts)


def stats_text(text: str) -> str:
    """高频词/口头禅统计前的清洗：去除提及、表情代码与占位符。"""
    t = text
    t = re.sub(r"@(?:m\d+|x\d+|bot|全体)", " ", t)
    t = re.sub(r"@[^\s@]{1,12}", " ", t)
    t = re.sub(r"/[\u4e00-\u9fffA-Za-z0-9_]{1,16}", " ", t)
    for ph in PLACEHOLDERS:
        t = t.replace(ph, " ")
    t = re.sub(r"【[^】]{1,8}】|\[[^\]\n]{1,10}\]", " ", t)
    return t.strip()


def tokenize(text: str) -> list[str]:
    if jieba is not None:
        raw = jieba.lcut(text)
    else:
        raw = RUN_RE.findall(text)
    out = []
    for w in raw:
        w = w.strip()
        if not WORD_RE.match(w):
            continue
        if w.casefold() in STOPWORDS:
            continue
        out.append(w.casefold() if w.isascii() else w)
    return out


def phrase_counts(text: str) -> Counter:
    """按标点/非文字符号分段后统计 2-4 元组，每条消息内去重。"""
    out = Counter()
    for run in RUN_RE.findall(text):
        if len(run) < 2:
            continue
        run = run[:30]
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                g = run[i : i + n]
                if g in seen:
                    continue
                seen.add(g)
                out[g] += 1
    return out


class MemberStats:
    __slots__ = ("raw", "dup", "system", "recalled", "media_only", "empty", "quarantined", "valid", "style_eligible",
                 "chars", "hour", "weekday",
                 "month", "days", "conv", "punct", "end_char", "special", "face", "sticker", "media", "words", "phrases",
                 "reply_out", "reply_in", "at_out", "at_in", "at_bot", "replies_sent", "flag_counts", "first_ts", "last_ts")

    def __init__(self):
        self.raw = 0
        self.dup = 0
        self.system = 0
        self.recalled = 0
        self.media_only = 0
        self.empty = 0
        self.quarantined = 0
        self.valid = 0
        self.style_eligible = 0
        self.chars: list[int] = []
        self.hour = Counter()
        self.weekday = Counter()
        self.month = Counter()
        self.days: set[str] = set()
        self.conv = Counter()
        self.punct = Counter()
        self.end_char = Counter()
        self.special = Counter()
        self.face = Counter()
        self.sticker = Counter()
        self.media = Counter()
        self.words = Counter()
        self.phrases = Counter()
        self.reply_out = Counter()
        self.reply_in = Counter()
        self.at_out = Counter()
        self.at_in = Counter()
        self.at_bot = Counter()
        self.replies_sent = 0
        self.flag_counts = Counter()
        self.first_ts = None
        self.last_ts = None


def resolve_at_target(d: dict, idb: IdentityBook, mention_uid_by_name: dict) -> tuple[str, str | None]:
    """解析 at 元素：返回 (kind, value)。kind ∈ person/bot/all/external/unresolved。"""
    uin, uid = d.get("uin"), d.get("uid")
    name = d.get("name") or ""
    if name == "全体成员":
        return "all", None
    if not uid and name:
        uid = mention_uid_by_name.get(name_key(name))
    if idb.is_bot(uin, uid):
        return "bot", idb.bot_name_by_key.get(IdentityBook._key(uin, uid)) or name or "机器人"
    alias, _ = idb.resolve(uid=uid, uin=uin, name=name or None)
    if alias:
        return "person", alias
    alias, _ = idb.resolve_external(uid=uid, uin=uin, name=name or None)
    if alias:
        return "external", alias
    return "unresolved", name or "未知"


def process_chat_source(path: Path, source_id: str, idb: IdentityBook, redactor: Redactor, tz: ZoneInfo,
                        stats: dict, counters: Counter, errors: list, quarantine: list,
                        corpus_rows: dict, seen: set, bot_stats: dict, unresolved_labels: Counter,
                        reply_methods: Counter, cfg_stats: dict):
    """Pass B：过滤、脱敏、统计并产出语料记录。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    chat = data.get("chatInfo") or {}
    peer = str(chat.get("peerUid"))
    conv_alias = idb.conversations[peer]["alias"]
    msgs = data.get("messages") or []

    for pos, m in enumerate(msgs, start=1):
        counters["raw"] += 1
        rid = f"{source_id}:msg-{pos:06d}"
        if not isinstance(m, dict):
            counters["parse_error"] += 1
            errors.append({"record": rid, "reason": "not_object"})
            continue
        mid = m.get("id")
        ts = m.get("timestamp")
        if mid is None or not isinstance(ts, (int, float)):
            counters["parse_error"] += 1
            errors.append({"record": rid, "reason": "missing_id_or_timestamp"})
            continue
        mid = str(mid)
        iso = as_utc_iso(int(ts))
        t_field = m.get("time")
        if isinstance(t_field, str) and t_field:
            try:
                ta = datetime.fromisoformat(t_field.replace("Z", "+00:00"))
                if abs((ta - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()) > 1:
                    counters["time_conflict"] += 1
                    errors.append({"record": rid, "reason": "time_conflict", "timestamp": ts, "time": t_field})
                    continue
            except ValueError:
                counters["time_conflict"] += 1
                errors.append({"record": rid, "reason": "time_unparsable", "timestamp": ts, "time": t_field})
                continue

        sender = m.get("sender") or {}
        uid, uin = sender.get("uid"), sender.get("uin")
        if uid == "未知":
            uid = None
        alias, _method = idb.resolve(uid=uid, uin=uin)
        is_systemish = bool(m.get("system")) or m.get("type") == "system" or any(
            isinstance(el, dict) and el.get("type") == "system" for el in ((m.get("content") or {}).get("elements") or [])
        )
        ms = stats.setdefault(alias, MemberStats()) if alias else None
        if ms is not None:
            ms.raw += 1
            if ms.first_ts is None or iso < ms.first_ts:
                ms.first_ts = iso
            if ms.last_ts is None or iso > ms.last_ts:
                ms.last_ts = iso

        # 流水线互斥分类：重复 → 无身份（机器人/撤回/系统）→ 撤回 → 系统 → 无有效文本 → 隐私隔离 → 有效
        if (conv_alias, mid) in seen:
            counters["duplicate"] += 1
            if ms is not None:
                ms.dup += 1
            continue
        seen.add((conv_alias, mid))
        if alias is None:
            if idb.is_bot(uin, uid):
                counters["bot_excluded"] += 1
                bkey = idb.bot_name_by_key.get(IdentityBook._key(uin, uid)) or "机器人"
                bot_stats.setdefault(bkey, {"excluded": 0, "at_count": 0})["excluded"] += 1
            elif m.get("recalled"):
                counters["recalled"] += 1
            elif is_systemish:
                counters["system"] += 1
            else:
                counters["unknown_sender"] += 1
                errors.append({"record": rid, "reason": "unknown_sender"})
            continue
        if m.get("recalled"):
            counters["recalled"] += 1
            ms.recalled += 1
            continue
        elements = [el for el in ((m.get("content") or {}).get("elements") or []) if isinstance(el, dict)]
        if is_systemish:
            counters["system"] += 1
            ms.system += 1
            continue

        segments, media, ats, faces, stickers, reply_data, has_non_lang = extract_elements(m)
        is_reply = m.get("type") == "reply" or reply_data is not None
        if not segments and not ats:
            if media:
                counters["media_only"] += 1
                ms.media_only += 1
                ms.media.update(media)
            else:
                counters["empty"] += 1
                ms.empty += 1
            continue

        # 脱敏文本元素
        redacted_segments: list[dict] = []
        flags: list[str] = []
        quarantined = False
        residual = None
        for seg in segments:
            if seg["type"] == "text":
                new_text, seg_flags, _hits = redactor.redact(seg["text"])
                flags.extend(seg_flags)
                r = redactor.residual_risk(new_text)
                if r:
                    residual = r
                    break
                redacted_segments.append({"type": "text", "text": new_text})
            elif seg["type"] == "face":
                redacted_segments.append(seg)
            elif seg["type"] == "sticker":
                redacted_segments.append(seg)
        if residual:
            counters["privacy_quarantined"] += 1
            ms.quarantined += 1
            quarantine.append({"record": rid, "reason": residual, "conversation": f"group:{conv_alias}", "person": alias, "timestamp": iso})
            continue

        # 提及解析
        mention_uid_by_name = {}
        for men in (m.get("content") or {}).get("mentions") or []:
            if isinstance(men, dict) and men.get("name"):
                mention_uid_by_name.setdefault(name_key(men["name"]), men.get("uid"))
        at_targets = []
        for d in ats:
            kind, value = resolve_at_target(d, idb, mention_uid_by_name)
            at_targets.append((kind, value))
            if kind == "person":
                redacted_segments.append({"type": "at", "person": value})
            elif kind == "bot":
                redacted_segments.append({"type": "at", "bot": True, "label": value})
            elif kind == "all":
                redacted_segments.append({"type": "at", "group": "all"})
            elif kind == "external":
                redacted_segments.append({"type": "at", "person": value})
            else:
                label_r, _f, _h = redactor.redact(value)
                redacted_segments.append({"type": "at", "unresolved": True, "label": label_r})

        # 回复目标
        reply_alias = None
        reply_method = None
        if is_reply and reply_data:
            su = str(reply_data.get("senderUin") or "").strip()
            if su and su not in ("0",):
                reply_alias, _ = idb.resolve(uin=su)
                if reply_alias:
                    reply_method = "uin"
            if not reply_alias:
                for kind, value in at_targets:
                    if kind == "person" and value != alias:
                        reply_alias, reply_method = value, "at"
                        break
            if not reply_alias:
                sn = reply_data.get("senderName") or ""
                if sn:
                    reply_alias, _ = idb.resolve(name=sn)
                    if reply_alias:
                        reply_method = "name"

        text = render_segments(redacted_segments)
        if not text.strip():
            if media:
                counters["media_only"] += 1
                ms.media_only += 1
                ms.media.update(media)
            else:
                counters["empty"] += 1
                ms.empty += 1
            continue

        # —— 有效消息 ——
        counters["valid"] += 1
        ms.valid += 1
        if has_non_lang and media:
            ms.media.update(media)
            flags.append("has_media")
        if is_reply:
            ms.replies_sent += 1

        dt_local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        ms.hour[dt_local.hour] += 1
        ms.weekday[dt_local.weekday()] += 1
        ms.month[dt_local.strftime("%Y-%m")] += 1
        ms.days.add(dt_local.strftime("%Y-%m-%d"))
        ms.conv[conv_alias] += 1
        ms.chars.append(len(text))
        if text:
            ms.end_char[text[-1]] += 1
            ms.punct.update(ch for ch in text if ch in PUNCT_CHARS)
        for sname, pat in SPECIAL_PATTERNS.items():
            if pat.search(text):
                ms.special[sname] += 1
        for name in faces:
            ms.face[name] += 1
        for name in stickers:
            ms.sticker[name] += 1
        st = stats_text(text)
        if st:
            ms.words.update(tokenize(st))
            ms.phrases.update(phrase_counts(st))

        # 互动统计（仅有效消息）
        skipped_reply_at = False
        for kind, value in at_targets:
            if kind == "person":
                if is_reply and not skipped_reply_at and value == reply_alias:
                    skipped_reply_at = True
                    continue
                ms.at_out[value] += 1
                stats.setdefault(value, MemberStats()).at_in[alias] += 1
            elif kind == "bot":
                ms.at_bot[value] += 1
                bot_stats.setdefault(value, {"excluded": 0, "at_count": 0})["at_count"] += 1
            elif kind == "external":
                idb.note_external_mention(value)
            elif kind == "all":
                counters["at_all"] += 1
            elif kind == "unresolved":
                counters["unresolved_mentions"] += 1
                unresolved_labels[redactor.redact(value)[0]] += 1
        if is_reply:
            if reply_alias:
                counters["reply_resolved"] += 1
                reply_methods[reply_method or "unknown"] += 1
                if reply_alias != alias:
                    ms.reply_out[reply_alias] += 1
                    stats.setdefault(reply_alias, MemberStats()).reply_in[alias] += 1
            else:
                counters["reply_unresolved"] += 1
                flags.append("reply_unresolved")
                sn = (reply_data or {}).get("senderName") or ""
                if sn:
                    unresolved_labels[redactor.redact(sn)[0]] += 1
        for f in sorted(set(flags)):
            ms.flag_counts[f] += 1

        row = {
            "schema_version": SCHEMA_VERSION,
            "record_id": rid,
            "source_id": source_id,
            "source_kind": "chat_export",
            "message_id": mid,
            "conversation_id": f"group:{conv_alias}",
            "person_id": alias,
            "timestamp": iso,
            "text": text,
            "segments": redacted_segments,
            "reply_to": reply_alias,
            "flags": sorted(set(flags)),
            "eligible_for_style": True,
            "visibility": "source_only",
        }
        corpus_rows.setdefault(alias, []).append(row)


# ---------------------------------------------------------------------------
# 博客文章（Markdown）处理
# ---------------------------------------------------------------------------

IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
TODO_RE = re.compile(r"^(?:TODO|待补|待补充|待写|占位|WIP|\.\.\.|…)[\u3002.!！]?$", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def parse_front_matter(text: str):
    """返回 (front_matter, body, had_fm)。YAML 解析失败时 front_matter 为 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, False
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                fm = yaml.safe_load(fm_text) or {}
                if not isinstance(fm, dict):
                    fm = {}
                return fm, body, True
            except yaml.YAMLError:
                return None, text, True
    return None, text, True


def post_date_to_utc(value, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        from datetime import date
        if isinstance(value, date):
            dt = datetime(value.year, value.month, value.day)
        else:
            s = str(value).strip()
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                try:
                    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_posts_source(path: Path, source_id: str, redactor: Redactor, tz: ZoneInfo,
                         author_alias: str | None, rows: list, pstats: Counter, errors: list) -> dict:
    files = sorted(p for p in path.glob("*.md") if p.is_file())
    total_bytes = 0
    for doc_idx, md in enumerate(files, start=1):
        total_bytes += md.stat().st_size
        raw = md.read_text(encoding="utf-8")
        fm, body, had_fm = parse_front_matter(raw)
        if fm is None:
            pstats["front_matter_error"] += 1
            errors.append({"record": f"{source_id}:post-{doc_idx:03d}", "reason": "front_matter_error", "file": md.name})
            continue
        pstats["documents"] += 1
        title = str(fm.get("title") or md.stem)
        draft = bool(fm.get("draft"))
        if draft:
            pstats["draft"] += 1
        published_at = post_date_to_utc(fm.get("date"), tz)
        if published_at is None and fm.get("date") is not None:
            pstats["date_error"] += 1

        seq = 0
        section_stack: list[tuple[int, str]] = []
        in_code = False
        pending: list[str] = []

        def emit(content_kind: str, text: str, extra_flags: list[str] | None = None) -> None:
            nonlocal seq
            text = text.strip("\n")
            if not text.strip():
                return
            if content_kind != "code":
                if TODO_RE.match(text.strip()):
                    pstats["excluded_todo"] += 1
                    return
                text_r, _flags, _hits = redactor.redact(text, keep_urls=True)
            else:
                text_r, _flags, _hits = redactor.redact(text, keep_urls=True)
            seq += 1
            flags = list(extra_flags or [])
            if draft:
                flags.append("draft")
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "record_id": f"{source_id}:post-{doc_idx:03d}:{seq:04d}",
                "source_id": source_id,
                "source_kind": "blog_post",
                "title": title,
                "published_at": published_at,
                "draft": draft,
                "section_path": " / ".join(t for _lvl, t in section_stack),
                "content_kind": content_kind,
                "text": text_r,
                "flags": flags,
                "eligible_for_style": (content_kind in ("paragraph", "quote", "list")) and not draft,
                "visibility": "source_only",
                "author_id": author_alias,
            })
            if content_kind == "code":
                pstats["code_blocks"] += 1
            elif content_kind == "quote":
                pstats["quotes"] += 1
            elif content_kind == "list":
                pstats["lists"] += 1
            elif content_kind == "image_ref":
                pstats["image_refs"] += 1
            else:
                pstats["paragraphs"] += 1

        def flush_pending() -> None:
            if not pending:
                return
            block = "\n".join(pending)
            pending.clear()
            stripped = block.strip()
            if not stripped:
                return
            lines_b = stripped.splitlines()
            img_refs = IMG_RE.findall(block)
            cleaned = IMG_RE.sub("", block).strip()
            if lines_b and all(ln.lstrip().startswith(">") for ln in lines_b):
                body_text = "\n".join(ln.lstrip()[1:].strip() for ln in lines_b)
                emit("quote", body_text)
            elif lines_b and all(LIST_RE.match(ln) for ln in lines_b):
                emit("list", cleaned)
            else:
                if cleaned.strip():
                    emit("paragraph", cleaned)
                for alt, _url in img_refs:
                    emit("image_ref", alt or "", ["missing_alt"] if not alt else None)

        code_buf: list[str] = []
        for ln in body.splitlines():
            if ln.strip().startswith("```"):
                if not in_code:
                    flush_pending()
                    in_code = True
                    code_buf = []
                else:
                    emit("code", "\n".join(code_buf))
                    in_code = False
                    code_buf = []
                continue
            if in_code:
                code_buf.append(ln)
                continue
            m_head = HEADING_RE.match(ln)
            if m_head:
                flush_pending()
                level = len(m_head.group(1))
                while section_stack and section_stack[-1][0] >= level:
                    section_stack.pop()
                section_stack.append((level, m_head.group(2).strip()))
                continue
            if not ln.strip():
                flush_pending()
                continue
            pending.append(ln)
        flush_pending()
        if in_code:  # 未闭合代码块：按代码保存
            emit("code", "\n".join(code_buf))
        if seq == 0:
            pstats["empty_documents"] += 1

    return {
        "source_id": source_id,
        "kind": "blog_posts",
        "path": str(path),
        "files": len(files),
        "bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# 产物：统计报告与 manifest
# ---------------------------------------------------------------------------

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def top_str(counter: Counter, k: int = 5, total: bool = False) -> str:
    items = counter.most_common(k)
    parts = [f"{name}×{value}" for name, value in items]
    if total:
        parts.append(f"共{sum(counter.values())}")
    return "，".join(parts) if parts else "—"


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def quantile(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def build_stats_report(cfg, idb: IdentityBook, stats: dict, counters: Counter, source_metas: list,
                       posts_stats: Counter, posts_meta: dict, bot_stats: dict, unresolved_labels: Counter,
                       reply_methods: Counter, redactor: Redactor, tz: ZoneInfo,
                       corpus_manifest: list, cfg_digest: str) -> str:
    lines: list[str] = []
    ap = lines.append
    ap("# 统计报告（自动生成）")
    ap("")
    ap(f"工具：`scripts/01_preprocess.py` v{SCRIPT_VERSION}；schema_version={SCHEMA_VERSION}；配置摘要：`{cfg_digest[:12]}`。")
    ap("报告为本地分析产物：机器人以显式名单排除；QQ 号仅展现尾号；成员使用匿名别名（原始映射在身份表中，不进入公开文档）。")
    ap("")
    ap("## 1. 输入")
    ap("")
    ap("| source_id | 类型 | 文件摘要 | 消息数 | 声明数 | 时间范围(UTC) |")
    ap("| --- | --- | --- | --- | --- | --- |")
    for meta in source_metas:
        rng = meta.get("declared_range") or {}
        rng_s = f"{rng.get('start', '?')} ~ {rng.get('end', '?')}" if rng else "—"
        ap(f"| {meta['source_id']} | {meta['kind']} | {meta['file'][:12]}… sha256:{meta['sha256'][:10]} | {meta['messages_raw']:,} | {meta.get('declared_total') or '—'} | {rng_s} |")
    ap("")
    raw_total = sum(m["messages_raw"] for m in source_metas)
    decl_total = sum(m["declared_total"] or 0 for m in source_metas)
    ap(f"聊天消息合计：{raw_total:,} 条（导出声明合计 {decl_total:,}）。")
    ap("")
    ap("## 2. 流水线计数（排除类别按顺序互斥）")
    ap("")
    ap("| 类别 | 数量 | 说明 |")
    ap("| --- | --- | --- |")
    rows = [
        ("原始消息", counters["raw"], "两个群聊导出中的全部条目"),
        ("重复", counters["duplicate"], "同一会话内消息 ID 重复（跨导出）"),
        ("机器人消息", counters["bot_excluded"], "发送者在显式机器人名单中"),
        ("撤回/系统", counters["recalled"] + counters["system"], f"撤回标记 {counters['recalled']:,} + 系统类 {counters['system']:,}"),
        ("无有效文本", counters["media_only"] + counters["empty"], f"纯媒体 {counters['media_only']:,} + 空内容 {counters['empty']:,}"),
        ("隐私隔离", counters["privacy_quarantined"], "脱敏后仍有残留风险的内容"),
        ("有效语料", counters["valid"], "进入 corpus/members/"),
        ("解析失败/时间冲突", counters["parse_error"] + counters["time_conflict"], "隔离至 errors.local.jsonl"),
        ("未知发送者", counters["unknown_sender"], "无法建立身份的消息"),
    ]
    for name, num, note in rows:
        ap(f"| {name} | {num:,} | {note} |")
    ap("")
    ap("> 复读未按文本去重，保留用于互动统计；撤回列表以标记为准；媒体消息不计入语言样本。")
    ap("")
    ap("## 3. 机器人排除（显式名单）")
    ap("")
    ap("| 名称 | QQ 尾号 | 发送消息 | 被 @ 次数 | 说明 |")
    ap("| --- | --- | --- | --- | --- |")
    for b in sorted(idb.bots, key=lambda x: x.get("uin") or ""):
        name = b.get("name") or "机器人"
        msg_n = bot_stats.get(name, {}).get("excluded", 0)
        at_n = bot_stats.get(name, {}).get("at_count", 0)
        ap(f"| {name} | {mask_uin(b.get('uin'))} | {msg_n:,} | {at_n:,} | 已从语料、成员统计与人类互动中排除 |")
    ap("")
    ap("## 4. 成员总览")
    ap("")
    ap("| 别名 | 群 | 有效消息 | 平均长度 | 中位 | ≤5字 | 活跃天 | 峰值时段 | 被回复 | 被@ | 候选 |")
    ap("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for alias in sorted(stats):
        ms = stats[alias]
        rec = next((r for r in idb.persons.values() if r.get("alias") == alias), None)
        convs = "、".join(sorted(ms.conv)) if ms.conv else "—"
        chars = sorted(ms.chars)
        avg = sum(chars) / len(chars) if chars else 0
        med = quantile(chars, 0.5)
        short = sum(1 for c in chars if c <= 5)
        active_days = len(ms.days)
        peak = ms.hour.most_common(1)
        peak_s = f"{peak[0][0]:02d}时×{peak[0][1]:,}" if peak else "—"
        cand = ""
        if rec and rec.get("candidate"):
            role = (cfg.get("candidates", {}).get(rec.get("uin") or "", {}) or {}).get("role", "reference")
            cand = "✓ " + ("主要参考" if role == "primary_reference" else role)
        ap(f"| {alias} | {convs} | {ms.valid:,} | {avg:.1f} | {med} | {pct(short, len(chars))} | {active_days or '—'} | {peak_s} | {sum(ms.reply_in.values()):,} | {sum(ms.at_in.values()):,} | {cand} |")
    ap("")
    ap("## 5. 逐成员统计")
    ap("")
    for alias in sorted(stats):
        ms = stats[alias]
        rec = next((r for r in idb.persons.values() if r.get("alias") == alias), None)
        cand_note = ""
        if rec and rec.get("candidate"):
            c = cfg.get("candidates", {}).get(rec.get("uin") or "", {}) or {}
            cand_note = f"（候选：{c.get('role', 'reference')}；{c.get('note', '')}）"
        ap(f"### {alias} {cand_note}")
        ap("")
        ap(f"- 时间范围：{(ms.first_ts or '?')[:16]} ~ {(ms.last_ts or '?')[:16]}（UTC）")
        ap(f"- 计数：原始 {ms.raw:,}；重复 {ms.dup}；系统 {ms.system}；撤回 {ms.recalled}；纯媒体 {ms.media_only}；空 {ms.empty}；隔离 {ms.quarantined}；有效 {ms.valid:,}；风格可用 {ms.style_eligible:,}")
        chars = sorted(ms.chars)
        if chars:
            ap(f"- 长度：平均 {sum(chars) / len(chars):.1f}，中位 {quantile(chars, 0.5)}，p90 {quantile(chars, 0.9)}，最长 {chars[-1]}；≤5字 {pct(sum(1 for c in chars if c <= 5), len(chars))}；≤10字 {pct(sum(1 for c in chars if c <= 10), len(chars))}")
        hours = "、".join(f"{h:02d}时×{n:,}" for h, n in ms.hour.most_common(6))
        ap(f"- 时段 Top6：{hours or '—'}；活跃天 {len(ms.days):,}；活跃月份数 {len(ms.month)}；群分布：{'、'.join(f'{k}×{v:,}' for k, v in sorted(ms.conv.items())) or '—'}")
        punct = "、".join(f"{ch}×{n:,}" for ch, n in ms.punct.most_common(8))
        ap(f"- 标点 Top8：{punct or '—'}")
        sp = "、".join(f"{_special_name(k)}×{v:,}" for k, v in ms.special.most_common(8))
        ap(f"- 语气特征：{sp or '—'}")
        ap(f"- 表情：QQ表情 Top6：{top_str(ms.face, 6)}；表情包 Top5：{top_str(ms.sticker, 5)}；表情图 {ms.media.get('sticker_image', 0):,}；图片 {ms.media.get('image', 0):,}；视频 {ms.media.get('video', 0):,}；语音 {ms.media.get('audio', 0):,}；卡片 {ms.media.get('json', 0):,}；转发 {ms.media.get('forward', 0):,}")
        ap(f"- 高频词 Top15：{top_str(ms.words, 15)}（关键词统计不含贴图与占位符）")
        min_ph = (cfg.get("stats") or {}).get("min_phrase_messages", 5)
        ph = Counter({k: v for k, v in ms.phrases.items() if v >= min_ph}) or ms.phrases
        ap(f"- 口头禅候选 Top10：{top_str(ph, 10)}（按出现消息数，阈值≥{min_ph}）")
        rep = f"发出回复 {ms.replies_sent:,}"
        if ms.reply_out:
            rep += f"；最常回复：{top_str(ms.reply_out, 3)}"
        if ms.reply_in:
            rep += f"；被回复：{top_str(ms.reply_in, 3)}"
        ap(f"- 互动：{rep}")
        ats = []
        if ms.at_out:
            ats.append(f"@发出 Top3：{top_str(ms.at_out, 3)}")
        if ms.at_in:
            ats.append(f"@收到 Top3：{top_str(ms.at_in, 3)}")
        if ms.at_bot:
            ats.append(f"提及机器人 {sum(ms.at_bot.values()):,}（{top_str(ms.at_bot, 3)}）")
        ap(f"- 提及：{'；'.join(ats) if ats else '—'}")
        if ms.flag_counts:
            ap(f"- 标记：{'、'.join(f'{k}×{v}' for k, v in ms.flag_counts.most_common())}")
        ap("")
    ap("## 6. 成员互动关系（回复+提及）")
    ap("")
    ap("| 发起 | 对象 | 回复 | 提及 |")
    ap("| --- | --- | --- | --- |")
    pairs = plural_pair_counts(stats)
    for (a, b), (r, m) in pairs[:30]:
        ap(f"| {a} | {b} | {r:,} | {m:,} |")
    ap("")
    ap(f"回复解析方式：{'、'.join(f'{k}×{v:,}' for k, v in reply_methods.most_common()) or '—'}；未解析回复 {counters['reply_unresolved']:,}；未解析提及 {counters['unresolved_mentions']:,}。")
    if unresolved_labels:
        ap(f"未解析标签样例：{top_str(unresolved_labels, 10)}")
    ap("")
    ap("## 7. 提及但未发言账号")
    ap("")
    ap("| 别名 | QQ 尾号 | 名称（本地） | 被提及 | 备注 |")
    ap("| --- | --- | --- | --- | --- |")
    for rec in sorted(idb.external.values(), key=lambda r: -(r.get("mention_count") or 0)):
        names = sorted({v for vals in rec["variants"].values() for v in vals}, key=len, reverse=True)
        name = names[0] if names else "?"
        note = ""
        if re.search(r"bot", name, re.IGNORECASE):
            note = "名称包含 bot，疑似机器人（待确认）"
        ap(f"| {rec['alias']} | {mask_uin(rec.get('uin'))} | {name} | {rec.get('mention_count', 0):,} | {note} |")
    ap("")
    ap("## 8. 数据质量与偏差说明")
    ap("")
    ap(f"- 解析失败：{counters['parse_error']:,}；时间冲突：{counters['time_conflict']:,}；未知发送者：{counters['unknown_sender']:,}。")
    ap(f"- 脱敏命中：{', '.join(f'{k}×{v:,}' for k, v in sorted(redactor.hits.items())) or '—'}。")
    ap(f"- 风格存疑（保留在语料中但 eligible_for_style=false）：{counters.get('style_flagged', 0):,} 条；"
       f"分类（可重叠）：跨成员转发 {counters.get('flag_likely_forwarded', 0):,}、自我模板 {counters.get('flag_repeated_template', 0):,}、"
       f"跨群转发 {counters.get('flag_cross_posted', 0):,}、AI 摘要 {counters.get('flag_ai_digest', 0):,}。")
    ap(f"- 姓名替换规则 {NAME_RULES_VERSION}：备注真实姓名与含非中文的昵称一律替换；纯中文短昵称（<5 字）可能仍在自由文本中出现，如需覆盖请加入本地词表。")
    ap("- 互动关系基于显式回复与 @ 提及；时间相邻仅作弱关联，本报告不将其计入亲密关系。")
    ap("- 机器人仅按显式名单排除；第 7 节的疑似项需人工确认后加入本地配置。")
    ap("")
    ap("## 9. 博客语料")
    ap("")
    ap(f"- 文章 {posts_stats.get('documents', 0)} 篇（草稿 {posts_stats.get('draft', 0)} 篇，默认不进入模型上下文）。")
    ap(f"- 记录：段落 {posts_stats.get('paragraphs', 0):,}，代码块 {posts_stats.get('code_blocks', 0):,}，引用 {posts_stats.get('quotes', 0):,}，列表 {posts_stats.get('lists', 0):,}，图片引用 {posts_stats.get('image_refs', 0):,}。")
    ap(f"- 排除：TODO/占位段落 {posts_stats.get('excluded_todo', 0)}；front matter 解析失败 {posts_stats.get('front_matter_error', 0)}；空文档 {posts_stats.get('empty_documents', 0)}。")
    ap(f"- 博客作者：{'已按候选配置归属' + '（' + str(posts_meta.get('author_alias')) + '）' if posts_meta.get('author_alias') else '未配置'}；代码与公式作为知识材料，不混入聊天风格统计。")
    ap("")
    ap("## 10. 可复现性")
    ap("")
    ap("- 记录集合由输入 SHA-256、配置与身份表共同决定；语料文件摘要：")
    ap("")
    ap("| 文件 | sha256 | 行数 |")
    ap("| --- | --- | --- |")
    for item in corpus_manifest:
        line_s = f"{item['lines']:,}" if item.get("lines") is not None else "—"
        ap(f"| {item['path']} | {item['sha256'][:12]} | {line_s} |")
    ap("")
    return "\n".join(lines) + "\n"


def _special_name(key: str) -> str:
    return {
        "laugh_ha2": "哈≥2",
        "six_6x2": "6连",
        "yyds_233": "233",
        "grass": "草",
        "smile_face": "微笑类表情",
        "end_q": "问号结尾",
        "end_excl": "感叹结尾",
        "pure_punct": "纯标点",
    }.get(key, key)


def plural_pair_counts(stats: dict):
    out = defaultdict(lambda: [0, 0])
    for a, ms in stats.items():
        for b, n in ms.reply_out.items():
            out[(a, b)][0] += n
        for b, n in ms.at_out.items():
            out[(a, b)][1] += n
    pairs = sorted(out.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
    return [(k, v) for k, v in pairs]


# ---------------------------------------------------------------------------
# 写出与主流程
# ---------------------------------------------------------------------------

def rel_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def build_manifest(idb: IdentityBook, idb_sha: str, cfg_path: Path, cfg_digest: str, source_metas: list,
                   counters: Counter, bot_stats: dict, posts_stats: Counter, posts_meta: dict,
                   outputs: list, corpus_digest: str, root: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "script": {"name": "01_preprocess.py", "version": SCRIPT_VERSION},
        "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": {
            "python": sys.version.split()[0],
            "jieba": JIEBA_VERSION,
            "pyyaml": getattr(yaml, "__version__", None),
            "stopwords": STOPWORDS_VERSION,
            "name_rules": NAME_RULES_VERSION,
        },
        "config": {"path": rel_str(cfg_path, root), "sha256": cfg_digest},
        "identity_map": {
            "path": rel_str(idb.path, root),
            "sha256": idb_sha,
            "persons": sum(1 for r in idb.persons.values() if r.get("alias")),
            "external_targets": sum(1 for r in idb.external.values() if r.get("alias")),
            "conversations": len(idb.conversations),
        },
        "inputs": source_metas,
        "counts": dict(counters),
        "bots": [
            {"name": b.get("name"), "uin": b.get("uin"), "excluded": b.get("excluded", 0), "at_count": b.get("at_count", 0)}
            for b in sorted(idb.bots, key=lambda x: x.get("uin") or "")
        ],
        "posts": dict(posts_stats),
        "posts_source": posts_meta,
        "outputs": outputs,
        "corpus_digest": corpus_digest,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ohana 资料管线：归一化、过滤、脱敏、统计（本地离线）")
    parser.add_argument("--input", default="content", help="输入根目录（sources 未配置时用于扫描）")
    parser.add_argument("--output", default="analysis", help="输出根目录")
    parser.add_argument("--config", default="config/analysis.local.yaml", help="本地分析配置")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]

    def resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (root / pp)

    in_dir = resolve(args.input)
    out_dir = resolve(args.output)
    cfg_path = resolve(args.config)
    if not cfg_path.exists():
        raise SystemExit(f"缺少本地配置：{cfg_path}\n请从 config/analysis.example.yaml 复制为 config/analysis.local.yaml 后填写。")
    cfg = load_config(cfg_path)
    tz = ZoneInfo(cfg.get("timezone") or "Asia/Shanghai")
    log(f"[01] 项目根：{root}")
    log(f"[01] 配置：{rel_str(cfg_path, root)}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 来源解析
    sources = list(cfg.get("sources") or [])
    if not sources:
        chat_files = sorted(in_dir.glob("*.json"))
        sources = [{"source_id": f"src-{chr(ord('a') + i)}", "kind": "chat_export", "path": str(p)} for i, p in enumerate(chat_files)]
        posts_dir = in_dir / "posts"
        if posts_dir.exists():
            sources.append({"source_id": f"src-{chr(ord('a') + len(chat_files))}", "kind": "blog_posts", "path": str(posts_dir)})
        log(f"[01] 配置未列出 sources，自动扫描得到 {len(sources)} 个来源")
    chat_sources, posts_sources = [], []
    for s in sources:
        sp = resolve(s["path"])
        if not sp.exists():
            raise SystemExit(f"来源不存在：{sp}（source_id={s.get('source_id')}）")
        item = dict(s)
        item["_path"] = sp
        if s.get("kind") == "chat_export":
            chat_sources.append(item)
        elif s.get("kind") == "blog_posts":
            posts_sources.append(item)
        else:
            raise SystemExit(f"未知来源类型：{s.get('kind')}")
    if not chat_sources:
        raise SystemExit("没有聊天导出来源，无法生成语料。")

    # 身份表与 Pass A
    idb = IdentityBook(resolve(cfg.get("identity_map_path") or "runtime/identity_map.local.json"),
                       cfg.get("candidates") or {}, [dict(b) for b in (cfg.get("bots") or [])])
    idb.load()
    source_metas = []
    for s in chat_sources:
        log(f"[01] 扫描来源 {s['source_id']}：{s['_path'].name}")
        source_metas.append(scan_chat_source(s["_path"], s["source_id"], idb))
    idb.finalize()
    log(f"[01] 身份：成员 {sum(1 for r in idb.persons.values() if r.get('alias'))} 人，"
        f"外部提及目标 {sum(1 for r in idb.external.values() if r.get('alias'))} 个，会话 {len(idb.conversations)} 个")

    bot_stats = {b.get("name") or "机器人": {"excluded": 0, "at_count": 0} for b in idb.bots}

    # 脱敏器
    privacy_cfg = cfg.get("privacy") or {}
    lexicon = load_lexicon(resolve(privacy_cfg.get("public_lexicon") or "docs/lexicon-privacy.txt"))
    local_lexicon_path = resolve(privacy_cfg.get("local_lexicon") or "runtime/privacy/lexicon.local.txt")
    local_lexicon = load_lexicon(local_lexicon_path)
    if local_lexicon:
        log(f"[01] 本地词表：{len(local_lexicon)} 条（{rel_str(local_lexicon_path, root)}）")
    name_rules = build_name_rules(idb)
    known_numbers = {r.get("uin") for r in idb.persons.values()} | {r.get("uin") for r in idb.external.values()} | set(idb.conversations) | set(idb.bot_uins)
    redactor = Redactor(lexicon + local_lexicon, name_rules, known_numbers)

    # Pass B
    stats: dict[str, MemberStats] = {}
    counters: Counter = Counter()
    errors: list = []
    quarantine: list = []
    corpus_rows: dict[str, list] = {}
    seen: set = set()
    unresolved_labels: Counter = Counter()
    reply_methods: Counter = Counter()
    cfg_stats = cfg.get("stats") or {}
    for s in chat_sources:
        log(f"[01] 处理来源 {s['source_id']}：{s['_path'].name}")
        process_chat_source(s["_path"], s["source_id"], idb, redactor, tz, stats, counters, errors, quarantine,
                            corpus_rows, seen, bot_stats, unresolved_labels, reply_methods, cfg_stats)

    # —— 转发/模板识别：跨成员完全重复（≥40字）→ likely_forwarded；自我重复≥3次 → repeated_template；
    #    同人多群转发（≥60字）→ cross_posted；AI 新闻摘要 → ai_digest。命中则 eligible_for_style=False，
    #    保留在语料中仅作兴趣参考。 ——
    FP_MIN_LEN = 40
    digest_re = re.compile(r"\[第\s*\d+\s*/\s*\d+\s*批次\]|AI\s*分析数|核心热点态势|增量分析")
    fp_authors: dict[str, set] = defaultdict(set)
    fp_self: dict[str, Counter] = defaultdict(Counter)
    fp_groups: dict[str, set] = defaultdict(set)
    row_fps: dict[str, list] = defaultdict(list)
    for alias in sorted(corpus_rows):
        for row in corpus_rows[alias]:
            fp = re.sub(r"\W+", "", row["text"].casefold())
            row_fps[alias].append((row, fp))
            if len(fp) >= FP_MIN_LEN:
                fp_authors[fp].add(alias)
                fp_self[fp][alias] += 1
                fp_groups[fp].add(row["conversation_id"])
    ineligible = ("likely_forwarded", "repeated_template", "cross_posted", "ai_digest")
    for alias in sorted(corpus_rows):
        ms = stats.get(alias)
        for row, fp in row_fps[alias]:
            flags = set(row.get("flags") or [])
            if len(fp) >= FP_MIN_LEN and len(fp_authors[fp]) >= 2:
                flags.add("likely_forwarded")
            elif len(fp) >= FP_MIN_LEN and fp_self[fp][alias] >= 3:
                flags.add("repeated_template")
            elif len(fp) >= 60 and len(fp_groups[fp]) >= 2 and len(fp_authors[fp]) == 1:
                flags.add("cross_posted")
            if digest_re.search(row["text"]):
                flags.add("ai_digest")
            row["flags"] = sorted(flags)
            hit = [f for f in ineligible if f in flags]
            if hit:
                row["eligible_for_style"] = False
                counters["style_flagged"] += 1
                for f in hit:
                    counters[f"flag_{f}"] += 1
                    if ms is not None:
                        ms.flag_counts[f] += 1
            elif ms is not None:
                ms.style_eligible += 1

    # 短语剪枝（至少出现在 2 条消息中），控制内存与噪音
    for ms in stats.values():
        ms.words = Counter({k: v for k, v in ms.words.items() if v >= 2})
        ms.phrases = Counter({k: v for k, v in ms.phrases.items() if v >= 2})

    # 博客
    posts_stats: Counter = Counter()
    posts_rows: list = []
    posts_meta: dict = {"author_alias": None, "sources": []}
    author_alias = None
    for uin, c in (cfg.get("candidates") or {}).items():
        if c.get("is_blog_author"):
            rec = idb.persons.get(f"uin:{uin}")
            author_alias = rec.get("alias") if rec else None
    posts_meta["author_alias"] = author_alias
    for s in posts_sources:
        log(f"[01] 处理博客来源 {s['source_id']}：{rel_str(s['_path'], root)}")
        posts_meta["sources"].append(process_posts_source(s["_path"], s["source_id"], redactor, tz, author_alias, posts_rows, posts_stats, errors))

    # 语料写出（先写暂存目录，再整体替换）
    corpus_dir = out_dir / "corpus"
    members_dir = corpus_dir / "members"
    members_staging = out_dir / ".staging-members"
    if members_staging.exists():
        shutil.rmtree(members_staging)
    members_staging.mkdir(parents=True)
    corpus_manifest = []
    corpus_index = {"schema_version": SCHEMA_VERSION, "members": []}
    for alias in sorted(corpus_rows):
        rows = sorted(corpus_rows[alias], key=lambda r: (r["timestamp"], r["record_id"]))
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        (members_staging / f"{alias}.jsonl").write_text(content, encoding="utf-8")
        corpus_manifest.append({"path": f"corpus/members/{alias}.jsonl", "sha256": sha256_bytes(content.encode("utf-8")), "lines": len(rows)})
        ms = stats.get(alias)
        rec = next((r for r in idb.persons.values() if r.get("alias") == alias), None)
        corpus_index["members"].append({
            "alias": alias,
            "candidate": bool(rec and rec.get("candidate")),
            "conversations": sorted(ms.conv) if ms else [],
            "valid": len(rows),
        })
    members_dir.mkdir(parents=True, exist_ok=True)
    for f in members_dir.glob("*.jsonl"):
        f.unlink()
    for f in members_staging.glob("*.jsonl"):
        f.replace(members_dir / f.name)
    shutil.rmtree(members_staging)
    write_json_atomic(corpus_dir / "index.json", corpus_index)

    posts_rows_sorted = sorted(posts_rows, key=lambda r: (r["source_id"], r["record_id"]))
    posts_path = corpus_dir / "posts.jsonl"
    write_text_atomic(posts_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in posts_rows_sorted))
    corpus_manifest.append({"path": "corpus/posts.jsonl", "sha256": sha256_file(posts_path), "lines": len(posts_rows_sorted)})
    corpus_manifest.append({"path": "corpus/index.json", "sha256": sha256_file(corpus_dir / "index.json"), "lines": None})

    if errors:
        write_text_atomic(out_dir / "errors.local.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in errors))
    if quarantine:
        write_text_atomic(out_dir / "quarantine.local.jsonl", "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in quarantine))

    # 身份表回写（机器人排除计数 + 外部提及计数）
    for b in idb.bots:
        st = bot_stats.get(b.get("name") or "")
        if st:
            b["excluded"] = st.get("excluded", 0)
            b["at_count"] = st.get("at_count", 0)
    idb.save()
    idb_sha = sha256_file(idb.path)

    cfg_digest = sha256_bytes(cfg_path.read_bytes())
    corpus_digest = sha256_bytes("".join(f"{i['path']}:{i['sha256']}\n" for i in sorted(corpus_manifest, key=lambda x: x["path"])).encode("utf-8"))

    report = build_stats_report(cfg, idb, stats, counters, source_metas, posts_stats, posts_meta, bot_stats,
                                unresolved_labels, reply_methods, redactor, tz, corpus_manifest, cfg_digest)
    report_path = out_dir / "stats_report.md"
    write_text_atomic(report_path, report)

    outputs = list(corpus_manifest)
    outputs.append({"path": "stats_report.md", "sha256": sha256_file(report_path), "lines": None})
    if errors:
        outputs.append({"path": "errors.local.jsonl", "sha256": sha256_file(out_dir / "errors.local.jsonl"), "lines": len(errors)})
    if quarantine:
        outputs.append({"path": "quarantine.local.jsonl", "sha256": sha256_file(out_dir / "quarantine.local.jsonl"), "lines": len(quarantine)})

    manifest = build_manifest(idb, idb_sha, cfg_path, cfg_digest, source_metas, counters, bot_stats, posts_stats,
                              posts_meta, outputs, corpus_digest, root)
    write_json_atomic(out_dir / "manifest.json", manifest)

    log("[01] 完成："
        f"原始 {counters['raw']:,}，重复 {counters['duplicate']:,}，机器人 {counters['bot_excluded']:,}，"
        f"系统 {counters['system']:,}，撤回 {counters['recalled']:,}，"
        f"纯媒体 {counters['media_only']:,}，空 {counters['empty']:,}，隔离 {counters['privacy_quarantined']:,}，"
        f"有效 {counters['valid']:,}；语料文件 {len(corpus_rows)} 个；corpus_digest {corpus_digest[:12]}")
    log(f"[01] 报告：{rel_str(report_path, root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
