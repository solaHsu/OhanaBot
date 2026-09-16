"""网页抓取工具的测试：URL 安全、HTML 清洗、抓取与重定向。

运行：python -m unittest discover -s tests -v
"""

import gzip
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from bot.tools import web


class UrlSafetyTests(unittest.TestCase):
    def test_reject_non_http(self):
        self.assertFalse(web._is_safe_url("ftp://example.com/"))
        self.assertFalse(web._is_safe_url("not a url"))
        self.assertFalse(web._is_safe_url(""))

    def test_reject_private_addresses(self):
        self.assertFalse(web._is_safe_url("http://127.0.0.1/"))
        self.assertFalse(web._is_safe_url("http://192.168.1.10/"))
        self.assertFalse(web._is_safe_url("http://10.0.0.5/"))
        self.assertFalse(web._is_safe_url("http://169.254.169.254/latest/meta-data"))

    def test_public_literal_ip_allowed(self):
        self.assertTrue(web._is_safe_url("http://8.8.8.8/"))


class HtmlExtractionTests(unittest.TestCase):
    def test_title_body_and_skip(self):
        markup = (
            "<html><head><title>我的标题</title>"
            "<script>var x=1;</script><style>.a{color:red}</style></head>"
            "<body><h1>第一节</h1><p>正文&amp;内容</p></body></html>"
        )
        result = web.extract_text_from_html(markup)
        self.assertIn("# 我的标题", result)
        self.assertIn("第一节", result)
        self.assertIn("正文&内容", result)
        self.assertNotIn("var x", result)
        self.assertNotIn(".a{", result)

    def test_broken_html_tolerated(self):
        result = web.extract_text_from_html("<p>断了<div>也能读")
        self.assertIn("断了", result)
        self.assertIn("也能读", result)

    def test_meta_description_extracted(self):
        markup = (
            "<html><head><title>标题</title>"
            '<meta name="description" content="这是页面描述">'
            '<meta property="og:title" content="分享标题"></head>'
            "<body>正文</body></html>"
        )
        result = web.extract_text_from_html(markup)
        self.assertIn("这是页面描述", result)
        self.assertIn("分享标题", result)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = "<html><head><title>本地标题</title></head><body>本地正文内容</body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/gbk":
            body = "<html><head><title>中文标题</title></head><body>中文内容考验</body></html>".encode("gbk")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=gbk")
        elif self.path == "/gzip":
            raw = "<html><head><title>压缩标题</title></head><body>压缩正文</body></html>".encode("utf-8")
            body = gzip.compress(raw)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
        elif self.path == "/redirect":
            body = b""
            self.send_response(302)
            self.send_header("Location", "/ok")
        elif self.path == "/redirect-unsafe":
            body = b""
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest")
        else:
            body = b"missing"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_private_blocked_by_default(self):
        self.assertIsNone(web.fetch_page(self.base + "/ok"))

    def test_fetch_local_page(self):
        text = web.fetch_page(self.base + "/ok", allow_private=True)
        self.assertIn("本地标题", text)
        self.assertIn("本地正文内容", text)

    def test_gbk_page_decoded(self):
        text = web.fetch_page(self.base + "/gbk", allow_private=True)
        self.assertIn("中文内容考验", text)

    def test_gzip_page_decoded(self):
        text = web.fetch_page(self.base + "/gzip", allow_private=True)
        self.assertIn("压缩标题", text)
        self.assertIn("压缩正文", text)

    def test_redirect_followed(self):
        text = web.fetch_page(self.base + "/redirect", allow_private=True)
        self.assertIn("本地标题", text)

    def test_unsafe_redirect_blocked(self):
        self.assertIsNone(web.fetch_page(self.base + "/redirect-unsafe", allow_private=True))

    def test_fetch_cached(self):
        web._CACHE.clear()
        self.addCleanup(web._CACHE.clear)
        url = self.base + "/ok"
        first = web.fetch_page(url, allow_private=True)
        self.assertIn("本地标题", first)
        self.assertIn(url, web._CACHE)
        self.server.shutdown()  # 关掉服务器后再取，只能命中缓存
        second = web.fetch_page(url, allow_private=True)
        self.assertEqual(second, first)

    def test_collect_dedup_and_limit(self):
        original = web.fetch_page

        def fake(url, **kwargs):
            return f"text:{url}"

        web.fetch_page = fake
        self.addCleanup(setattr, web, "fetch_page", original)
        pages = web.collect_page_texts(["u1", "u1", "u2", "u3"], limit=2)
        self.assertEqual(pages, [("u1", "text:u1"), ("u2", "text:u2")])


if __name__ == "__main__":
    unittest.main()
