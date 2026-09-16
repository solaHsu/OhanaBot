"""兴趣词表与会话兴趣集（标准 §4）。

- 词表文件：bot/memory/interests.md（人工维护），一行一个类目：`类别名: 成员词1、成员词2、…`；
- 命中判定：消息分词（jieba，大小写不敏感）与成员词求交集；
- 会话兴趣集：容量默认 5；类目优先（md 顺序），动态关键词补位；
- 动态关键词：参与退出时从本段对话分词统计词频，一条消息内每词最多计一次。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    import jieba

    jieba.setLogLevel(50)
except Exception:  # pragma: no cover - 无 jieba 时退化为 ASCII 词 + 汉字 bigram，保证可运行
    jieba = None

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INTERESTS_FILE = REPO_ROOT / "bot" / "memory" / "interests.md"

# 动态关键词用停用词（取 scripts/01_preprocess.py 的常用部分，口径一致）
STOPWORDS = frozenset(
    "的 了 是 我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些 什么 怎么 为什么 因为 所以 但是 而且 然后 "
    "就是 还是 也是 不是 没有 一个 一下 可以 自己 现在 时候 已经 应该 可能 觉得 知道 感觉 真的 太 都 也 又 还 就 才 "
    "很 挺 比较 非常 特别 有点 一些 啊 吧 呢 吗 呀 哦 噢 嗯 哈 嘿 诶 哎 唉 额 呃 哇 啦 嘛 喔 "
    "被 把 让 给 对 从 到 在 有 没 不 会 能 想 去 来 说 做 个 只 和 跟 于 而 则 且 并 但 却 如果 由于 于是 "
    "以及 或者 不过 其实 反正 确实 估计 大概 差不多 一样 一点 一直 一定 不要 不会 不用 "
    "这么 那么 多少 几个 第一 第二 最后 之前 之后 以后 以前 今天 明天 昨天 早上 中午 晚上 the a an is are".split()
)

_ASCII_WORD_RE = re.compile(r"[a-z][a-z0-9_+-]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 数据行：`- 类别: 词、词…`；排除含全角冒号 / 反引号 / 引号 / 星号的表头行
_DATA_LINE_RE = re.compile(r"^-\s*([^：`\"*]{1,20}?)\s*:\s*(.+)$")


def tokenize(text: str) -> list[str]:
    """分词并做大小写归一。"""
    lowered = text.lower()
    if jieba is not None:
        return [token.strip().lower() for token in jieba.lcut(lowered) if token.strip()]
    tokens = [match.group(0) for match in _ASCII_WORD_RE.finditer(lowered)]
    for match in _CJK_RUN_RE.finditer(lowered):
        run = match.group(0)
        tokens.extend(run if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)])
    return tokens


@dataclass(frozen=True)
class InterestCategory:
    """一个兴趣类目；priority 越小优先级越高（= md 中的行顺序）。"""

    name: str
    words: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class InterestItem:
    """会话兴趣集中的一项：兴趣类目或动态关键词。"""

    kind: str  # "category" | "keyword"
    name: str
    words: tuple[str, ...]
    priority: tuple[int, int]  # 类目 (0, md 顺序)；关键词 (1, -词频)

    def hit(self, tokens: set[str]) -> bool:
        return any(word in tokens for word in self.words)


def load_interests(path: Path | str = DEFAULT_INTERESTS_FILE) -> list[InterestCategory]:
    """解析 interests.md（表头行会被跳过）。"""
    categories: list[InterestCategory] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _DATA_LINE_RE.match(line.strip())
        if not match:
            continue
        name = match.group(1).strip()
        words_part = match.group(2)
        if "`" in words_part or "：" in words_part or "**" in words_part:
            continue
        words = tuple(word.strip().lower() for word in re.split(r"[、,，]", words_part) if word.strip())
        if name and words:
            categories.append(InterestCategory(name, words, len(categories)))
    return categories


class InterestSet:
    """每会话一个，容量固定；类目优先（md 顺序），动态关键词补位。"""

    def __init__(self, capacity: int = 5):
        self.capacity = capacity
        self.items: list[InterestItem] = []

    def clear(self) -> None:
        self.items = []

    def hit(self, tokens: set[str]) -> list[str]:
        """返回命中的项名列表（类目名 / 关键词）。"""
        return [item.name for item in self.items if item.hit(tokens)]

    def ensure_category(self, category: InterestCategory) -> None:
        """类目随命中增补；满员时淘汰优先级最低项（先动态词后类目）；优先级更低的新项不加入。"""
        if any(item.kind == "category" and item.name == category.name for item in self.items):
            return
        new_item = InterestItem("category", category.name, category.words, (0, category.priority))
        if len(self.items) >= self.capacity:
            lowest = max(self.items, key=lambda item: item.priority)
            if lowest.priority <= new_item.priority:
                return
            self.items.remove(lowest)
        self.items.append(new_item)

    def rebuild(self, categories: list[InterestCategory], keywords: list[tuple[str, int]]) -> None:
        """退出参与时重建：类目（md 顺序）+ 动态关键词（词频序）补位。"""
        items = [
            InterestItem("category", cat.name, cat.words, (0, cat.priority))
            for cat in sorted(categories, key=lambda cat: cat.priority)
        ]
        for word, freq in keywords:
            if len(items) >= self.capacity:
                break
            items.append(InterestItem("keyword", word, (word,), (1, -freq)))
        self.items = items[: self.capacity]


def extract_keywords(texts: list[str], top: int = 5) -> list[tuple[str, int]]:
    """动态关键词：词频统计（一条消息内每词最多计一次），返回 [(词, 频次)] 降序。"""
    freq: Counter[str] = Counter()
    for text in texts:
        for token in set(tokenize(text)):
            if token in STOPWORDS or len(token) < 2:
                continue
            if not any(ch.isalnum() for ch in token):
                continue
            freq[token] += 1
    return sorted(freq.items(), key=lambda item: (-item[1], item[0]))[:top]
