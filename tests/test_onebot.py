"""OneBot 接入层测试：消息解析、签名校验、事件服务、入库管道。

运行：python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from bot.engine.config import EngineConfig, OneBotConfig
from bot.engine.ingest import MessageIngestor
from bot.engine.onebot.parser import parse_message_event
from bot.engine.onebot.server import EventServer, verify_signature
from bot.engine.storage import db
from bot.engine.storage.memory_store import MemoryStore

SELF_ID = "424242"
GROUP_ID = 888888


def sample_group_payload(**overrides) -> dict:
    payload = {
        "time": 1789000000,
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 70001,
        "group_id": GROUP_ID,
        "user_id": 10001,
        "raw_message": "hi @Ohana 看看这个",
        "sender": {"user_id": 10001, "nickname": "阿崖", "card": "崖神"},
        "message": [
            {"type": "text", "data": {"text": "hi "}},
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": " 看看这个"}},
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "reply", "data": {"id": "69999"}},
        ],
    }
    payload.update(overrides)
    return payload


def http_post(url: str, body: bytes, headers: dict | None = None) -> int:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def http_post_chunked(host: str, port: int, path: str, body: bytes, headers: dict | None = None) -> int:
    """用 Transfer-Encoding: chunked 发送（NapCat HTTP 客户端的行为）。"""
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest("POST", path)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        middle = len(body) // 2
        for chunk in (body[:middle], body[middle:], b""):
            connection.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


class ParserTests(unittest.TestCase):
    def test_group_message_with_segments(self):
        message = parse_message_event(sample_group_payload(), SELF_ID)
        self.assertIsNotNone(message)
        self.assertEqual(message.conversation_id, f"group:{GROUP_ID}")
        self.assertEqual(message.user_id, "10001")
        self.assertEqual(message.display_name, "崖神")
        self.assertEqual(message.message_id, "70001")
        self.assertEqual(message.mentions, (SELF_ID,))
        self.assertTrue(message.mentions_self)
        self.assertEqual(message.replies_to, "69999")
        self.assertIn("@424242", message.text)
        self.assertIn("看看这个", message.text)
        self.assertIn("[图片]", message.text)
        self.assertEqual(len(message.images), 1)
        self.assertEqual(message.images[0].file, "x.jpg")
        self.assertTrue(message.sent_at.endswith("Z"))

    def test_non_message_and_private_ignored(self):
        self.assertIsNone(parse_message_event({"post_type": "meta_event"}, SELF_ID))
        payload = sample_group_payload(message_type="private", group_id=None)
        self.assertIsNone(parse_message_event(payload, SELF_ID))

    def test_mention_not_self(self):
        payload = sample_group_payload(message=[{"type": "at", "data": {"qq": "99999"}}])
        message = parse_message_event(payload, SELF_ID)
        self.assertFalse(message.mentions_self)
        self.assertEqual(message.mentions, ("99999",))

    def test_cq_string_compat(self):
        payload = sample_group_payload(
            message="你好[CQ:at,qq=424242] [CQ:image,file=x.jpg][CQ:reply,id=666]结束"
        )
        message = parse_message_event(payload, SELF_ID)
        self.assertEqual(message.mentions, (SELF_ID,))
        self.assertTrue(message.mentions_self)
        self.assertEqual(message.replies_to, "666")
        self.assertIn("[图片]", message.text)
        self.assertEqual(len(message.images), 1)
        self.assertEqual(message.images[0].file, "x.jpg")
        self.assertIn("你好", message.text)
        self.assertIn("结束", message.text)


    def test_json_card_extracted(self):
        card = {
            "app": "com.tencent.structmsg",
            "view": "news",
            "prompt": "[分享] 测试文章",
            "meta": {
                "news": {
                    "title": "测试文章",
                    "desc": "这是一段摘要",
                    "jumpUrl": "https://example.com/a",
                }
            },
        }
        payload = sample_group_payload(message=[{"type": "json", "data": {"data": json.dumps(card)}}])
        message = parse_message_event(payload, SELF_ID)
        self.assertIn("测试文章", message.text)
        self.assertIn("这是一段摘要", message.text)
        self.assertNotIn("jumpUrl", message.text)
        self.assertNotIn("com.tencent", message.text)
        self.assertEqual(message.links, ("https://example.com/a",))

    def test_xml_card_extracted(self):
        card = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<msg serviceID='1' templateID='1' brief='[分享] 好文' url='https://x'>"
            "<item><title><![CDATA[好文标题]]></title><summary>好文摘要</summary></item>"
            "<source name='某来源'/></msg>"
        )
        payload = sample_group_payload(message=[{"type": "xml", "data": {"data": card}}])
        message = parse_message_event(payload, SELF_ID)
        self.assertIn("好文标题", message.text)
        self.assertIn("好文摘要", message.text)
        self.assertIn("某来源", message.text)
        self.assertEqual(message.links, ("https://x",))

    def test_broken_card_falls_back(self):
        payload = sample_group_payload(
            message=[
                {"type": "json", "data": {"data": "not json"}},
                {"type": "xml", "data": {"data": "<msg"}},
            ]
        )
        message = parse_message_event(payload, SELF_ID)
        self.assertIn("[卡片]", message.text)


class SignatureTests(unittest.TestCase):
    def test_verify(self):
        import hashlib
        import hmac

        body = b'{"a": 1}'
        signature = hmac.new(b"secret", body, hashlib.sha1).hexdigest()
        self.assertTrue(verify_signature(f"sha1={signature}", body, "secret"))
        self.assertTrue(verify_signature(signature, body, "secret"))
        self.assertFalse(verify_signature("sha1=deadbeef", body, "secret"))
        self.assertFalse(verify_signature("", body, "secret"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.server = EventServer("127.0.0.1", 0, "/", self.calls.append, secret="topsecret")
        self.server.start_background()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()

    def sign(self, body: bytes) -> str:
        import hashlib
        import hmac

        return "sha1=" + hmac.new(b"topsecret", body, hashlib.sha1).hexdigest()

    def test_valid_event_returns_204(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        status = http_post(self.base + "/", body, {"X-Signature": self.sign(body)})
        self.assertEqual(status, 204)
        self.assertEqual(len(self.calls), 1)

    def test_bad_signature_returns_401(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        self.assertEqual(http_post(self.base + "/", body, {"X-Signature": "sha1=bad"}), 401)
        self.assertEqual(len(self.calls), 0)

    def test_bearer_token_accepted(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        status = http_post(self.base + "/", body, {"Authorization": "Bearer topsecret"})
        self.assertEqual(status, 204)
        self.assertEqual(len(self.calls), 1)

    def test_chunked_body_with_signature(self):
        # NapCat 的 HTTP 客户端用 chunked 发送；此前因读不到 body 导致签名校验永远失败
        body = json.dumps(sample_group_payload(), ensure_ascii=False).encode("utf-8")
        status = http_post_chunked(
            "127.0.0.1", self.server.port, "/", body, {"X-Signature": self.sign(body)}
        )
        self.assertEqual(status, 204)
        self.assertEqual(len(self.calls), 1)

    def test_chunked_body_bad_signature(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        status = http_post_chunked(
            "127.0.0.1", self.server.port, "/", body, {"X-Signature": "sha1=bad"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(len(self.calls), 0)

    def test_wrong_bearer_rejected(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        status = http_post(self.base + "/", body, {"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(len(self.calls), 0)

    def test_wrong_path_returns_404(self):
        self.assertEqual(http_post(self.base + "/nope", b"{}"), 404)

    def test_bad_json_returns_400(self):
        body = b"not json"
        self.assertEqual(http_post(self.base + "/", body, {"X-Signature": self.sign(body)}), 400)

    def test_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "ok")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.sqlite3"
        self.conn = db.connect(db_path)
        db.migrate(self.conn)
        self.store = MemoryStore(self.conn)
        config = EngineConfig(
            mode="dry_run",
            database=db_path,
            send_enabled=False,
            onebot=OneBotConfig(expected_self_id=SELF_ID, allowed_groups=[str(GROUP_ID)]),
        )
        self.forwarded = []
        self.ingestor = MessageIngestor(config, self.store, on_message=self.forwarded.append)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def count_messages(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def test_group_message_stored_and_forwarded(self):
        self.ingestor.handle(sample_group_payload())
        self.assertEqual(self.count_messages(), 1)
        self.assertEqual(len(self.forwarded), 1)
        row = self.conn.execute("SELECT user_id, display_name, mentions FROM messages").fetchone()
        self.assertEqual(row["user_id"], "10001")
        self.assertEqual(row["display_name"], "崖神")
        self.assertIn(SELF_ID, row["mentions"])
        # 群昵称历史已记录
        names = self.store.list_names("qq", f"group:{GROUP_ID}", "10001")
        self.assertEqual([n["text"] for n in names], ["崖神"])

    def test_duplicate_callback_ignored(self):
        self.ingestor.handle(sample_group_payload())
        self.ingestor.handle(sample_group_payload())
        self.assertEqual(self.count_messages(), 1)
        self.assertEqual(len(self.forwarded), 1)
        self.assertEqual(self.ingestor.duplicated, 1)

    def test_non_whitelist_group_rejected(self):
        self.ingestor.handle(sample_group_payload(group_id=999999))
        self.assertEqual(self.count_messages(), 0)
        self.assertEqual(self.ingestor.rejected, 1)

    def test_private_message_ignored(self):
        self.ingestor.handle(sample_group_payload(message_type="private", group_id=None))
        self.assertEqual(self.count_messages(), 0)

    def test_self_message_stored(self):
        payload = sample_group_payload(
            user_id=int(SELF_ID),
            sender={"user_id": int(SELF_ID), "nickname": "Ohana", "card": "Ohana"},
        )
        self.ingestor.handle(payload)
        self.assertEqual(self.count_messages(), 1)
        names = self.store.list_names("qq", f"group:{GROUP_ID}", SELF_ID)
        self.assertEqual([n["text"] for n in names], ["Ohana"])


class IngestOverHttpTests(unittest.TestCase):
    """经真实 HTTP 服务线程的端到端（曾暴露跨线程连接问题，防回归）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "e2e.sqlite3"
        self.conn = db.connect(db_path)
        db.migrate(self.conn)
        store = MemoryStore(self.conn)
        config = EngineConfig(
            mode="dry_run",
            database=db_path,
            send_enabled=False,
            onebot=OneBotConfig(expected_self_id=SELF_ID, allowed_groups=[str(GROUP_ID)]),
        )
        ingestor = MessageIngestor(config, store)
        self.server = EventServer("127.0.0.1", 0, "/", ingestor.handle)
        self.server.start_background()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.conn.close()
        self._tmpdir.cleanup()

    def test_event_over_http_persists(self):
        body = json.dumps(sample_group_payload()).encode("utf-8")
        self.assertEqual(http_post(self.base + "/", body), 204)
        count = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 1)
        names = self.conn.execute("SELECT text FROM names WHERE user_id = '10001'").fetchall()
        self.assertEqual([row["text"] for row in names], ["崖神"])


class ConfigResolutionTests(unittest.TestCase):
    def test_direct_value_wins(self):
        config = OneBotConfig(access_token="direct-token", access_token_env="OHANA_TEST_NONEXISTENT_ENV")
        self.assertEqual(config.resolve_access_token(), "direct-token")

    def test_env_fallback(self):
        os.environ["OHANA_TEST_TOKEN_ENV"] = "env-token"
        try:
            config = OneBotConfig(access_token_env="OHANA_TEST_TOKEN_ENV")
            self.assertEqual(config.resolve_access_token(), "env-token")
        finally:
            del os.environ["OHANA_TEST_TOKEN_ENV"]


if __name__ == "__main__":
    unittest.main()
