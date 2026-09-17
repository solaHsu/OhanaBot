"""网页抓取工具：把链接转成可读文本，供生成上下文注入。

- 仅允许 http/https；拒绝内网、回环、链路本地等地址（防 SSRF，重定向同样检查）；
- 超时 10 秒、正文 2MB 上限、重定向最多 3 跳；任何失败返回 None（调用方静默跳过）；
- HTML 清洗为纯文本：去 script/style，保留标题与正文，压缩空白，截断到上限。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 10.0
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 1200
MAX_REDIRECTS = 3
CACHE_TTL_SECONDS = 300.0  # 成功抓取的缓存时长
CACHE_MAX_ENTRIES = 50
USER_AGENT = "Mozilla/5.0 (compatible; Ohana/0.1)"

_CACHE: dict[str, tuple[float, str]] = {}

_SKIP_TAGS = {"script", "style", "noscript", "template"}
_BREAK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "table",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.meta_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            attrs_map = {str(name).lower(): str(value or "") for name, value in attrs}
            key = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            if key in ("description", "og:description", "og:title"):
                self.meta_parts.append(attrs_map.get("content", ""))
        elif tag in _BREAK_TAGS:
            self.body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BREAK_TAGS:
            self.body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.body_parts.append(data)


def extract_text_from_html(markup: str) -> str:
    """HTML → 标题 + 描述 + 正文纯文本（去脚本样式、压空白）。"""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass  # 坏 HTML 用已收集的内容
    title = _normalize("".join(parser.title_parts))
    meta = _normalize("\n".join(part for part in parser.meta_parts if part))
    body = _normalize("".join(parser.body_parts))
    parts = []
    if title:
        parts.append(f"# {title}")
    if meta:
        parts.append(meta)
    if body:
        parts.append(body)
    return "\n".join(parts).strip()


def fetch_page(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    max_bytes: int = MAX_PAGE_BYTES,
    allow_private: bool = False,
) -> str | None:
    """抓取网页并返回可读文本（标题 + 正文，截断）；地址不安全或失败返回 None。"""
    if not allow_private and not _is_safe_url(url):
        logger.info("链接不在允许范围（内网 / 非 http(s)），跳过：%s", url)
        return None
    cached = _cache_get(url)
    if cached is not None:
        logger.debug("链接命中缓存（%d 字）：%s", len(cached), url)
        return cached
    opener = urllib.request.build_opener(
        _SafeRedirectHandler(allow_private=allow_private, base_host=urllib.parse.urlparse(url).hostname or "")
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type() or ""
            if content_type and not content_type.startswith("text/"):
                logger.info("非文本内容（%s），跳过：%s", content_type, url)
                return None
            charset = response.headers.get_content_charset()
            payload = response.read(max_bytes + 1)
            content_encoding = (response.headers.get("Content-Encoding") or "").lower()
    except Exception as exc:
        logger.warning("链接抓取失败（%s）：%s", url, exc)
        return None
    payload = _decompress(payload, content_encoding, max_bytes)
    if payload is None:
        logger.warning("页面压缩编码无法处理（%s），跳过：%s", content_encoding, url)
        return None
    if len(payload) > max_bytes:
        payload = payload[:max_bytes]
        logger.info("页面过大已截断：%s", url)
    extracted = extract_text_from_html(_decode(payload, charset))
    if not extracted:
        return None
    text = extracted[:MAX_TEXT_CHARS]
    _cache_put(url, text)
    return text


def _cache_get(url: str) -> str | None:
    entry = _CACHE.get(url)
    if not entry:
        return None
    stored_at, text = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _CACHE.pop(url, None)
        return None
    return text


def _cache_put(url: str, text: str) -> None:
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest, None)
    _CACHE[url] = (time.monotonic(), text)


def collect_page_texts(urls, limit: int = 2) -> list[tuple[str, str]]:
    """按顺序抓取链接正文（去重、限量）；失败的跳过。返回 [(url, 文本)]。"""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in urls:
        if len(pages) >= limit:
            break
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        text = fetch_page(url)
        if text:
            logger.info("已抓取链接内容（%d 字）：%s", len(text), url)
            pages.append((url, text))
    return pages


# ---------- 内部 ----------

class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向前再次校验目标：跨主机必须通过公网检查；测试模式下同主机可放行。"""

    def __init__(self, allow_private: bool = False, base_host: str = "") -> None:
        super().__init__()
        self._allow_private = allow_private
        self._base_host = base_host
        self._count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._count >= MAX_REDIRECTS:
            raise urllib.error.HTTPError(newurl, code, "too many redirects", headers, None)
        if not _is_safe_url(newurl):
            same_host = urllib.parse.urlparse(newurl).hostname == self._base_host
            if not (self._allow_private and same_host):
                raise urllib.error.HTTPError(newurl, code, "redirect to unsafe address blocked", headers, None)
        self._count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    return _is_public_host(host)


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _decode(payload: bytes, charset: str | None) -> str:
    for encoding in filter(None, (charset, "utf-8", "gb18030")):
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _decompress(payload: bytes, encoding: str, limit: int) -> bytes | None:
    """按 Content-Encoding 解压（带展开上限）；无法处理返回 None。"""
    if not encoding or "identity" in encoding:
        return payload
    if "gzip" in encoding:
        candidates = (16 + zlib.MAX_WBITS, zlib.MAX_WBITS)
    elif "deflate" in encoding:
        candidates = (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    else:
        return None
    for wbits in candidates:
        try:
            return zlib.decompressobj(wbits).decompress(payload, limit + 1)
        except zlib.error:
            continue
    return None


def _normalize(text: str) -> str:
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
