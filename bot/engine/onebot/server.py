"""OneBot 11 事件服务：HTTP POST 接收 + HMAC-SHA1 签名校验 + GET /healthz。

设计依据 docs/engine-spec.md §1：
- 事件鉴权：X-Signature（HMAC-SHA1）为主，兼容 Authorization: Bearer Token；
- 请求体支持 Content-Length 与 chunked 两种（NapCat 的 HTTP 客户端使用分块传输）；
- 请求体上限 1 MiB；持久化成功后立即返回 2xx；鉴权失败 401/403；过大 413；无法落库 503。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


def verify_signature(signature_header: str, body: bytes, secret: str) -> bool:
    """校验 OneBot X-Signature：sha1=<hex(hmac_sha1(secret, body))>（容许不带前缀）。"""
    if not signature_header:
        return False
    value = signature_header.strip()
    if value.startswith("sha1="):
        value = value[5:]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(value.lower(), expected.lower())


def _read_chunked_body(rfile, max_bytes: int) -> bytes:
    """读取 Transfer-Encoding: chunked 请求体（NapCat HTTP 客户端使用分块传输）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        size_line = rfile.readline(65536).split(b";", 1)[0].strip()
        try:
            size = int(size_line, 16)
        except ValueError as exc:
            raise ValueError(f"非法 chunk 长度：{size_line[:32]!r}") from exc
        if size == 0:
            while True:  # 跳过 trailer
                trailer = rfile.readline(65536)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        total += size
        if total > max_bytes:
            raise OverflowError("chunked 请求体超过上限")
        chunks.append(rfile.read(size))
        rfile.read(2)  # 每个 chunk 后的 CRLF
    return b"".join(chunks)


class _Handler(BaseHTTPRequestHandler):
    server_version = "OhanaEventServer/0.1"

    def do_POST(self):  # noqa: N802 - http.server 接口命名
        event_server = self.server
        if self.path.rstrip("/") != str(event_server.event_path).rstrip("/"):
            self.send_error(404)
            return
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            try:
                body = _read_chunked_body(self.rfile, MAX_BODY_BYTES)
            except OverflowError:
                self.send_error(413)
                return
            except ValueError:
                self.send_error(400)
                return
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.send_error(400)
                return
            if length > MAX_BODY_BYTES:
                self.send_error(413)
                return
            body = self.rfile.read(length)

        if event_server.secret:
            signature = self.headers.get("X-Signature", "")
            authorization = self.headers.get("Authorization", "")
            bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            # 兼容两种鉴权：OneBot 11 的 X-Signature（HMAC-SHA1），或 Bearer Token（NapCat HTTP 客户端）
            if not (
                verify_signature(signature, body, event_server.secret)
                or hmac.compare_digest(bearer, event_server.secret)
            ):
                logger.warning(
                    "事件鉴权失败（来源 %s；X-Signature=%s；Authorization=%s）",
                    self.client_address[0],
                    (signature[:12] + "…") if signature else "无",
                    "Bearer" if bearer else "无",
                )
                self.send_error(401)
                return

        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload 不是 JSON 对象")
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning("事件解析失败：%s", exc)
            self.send_error(400)
            return

        try:
            event_server.callback(payload)
        except Exception:
            logger.exception("事件处理失败")
            self.send_error(503)
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") != "/healthz":
            self.send_error(404)
            return
        payload = {"status": "ok"}
        health_check = getattr(self.server, "health_check", None)
        if health_check is not None:
            try:
                payload["storage"] = health_check()
            except Exception:
                payload["status"] = "degraded"
                payload["storage"] = "error"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 交给 logging 统一处理，屏蔽默认 stderr 输出
        logger.debug("%s - %s", self.client_address[0], fmt % args)


class EventServer:
    """包裹 ThreadingHTTPServer；callback(payload) 抛异常时返回 503。"""

    def __init__(
        self,
        host: str,
        port: int,
        event_path: str,
        callback,
        secret: str = "",
        health_check=None,
    ):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.event_path = event_path
        self._httpd.callback = callback
        self._httpd.secret = secret
        self._httpd.health_check = health_check

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="event-server")
        thread.start()
        return thread

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
