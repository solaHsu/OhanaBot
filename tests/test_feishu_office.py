import json
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from bot.apps.feishu.api import FeishuClient, ServiceError
from bot.apps.feishu.assistant import OFFICE, OfficeAssistant
from bot.core import ChatCore, SQLiteStore


class FeishuClientTests(unittest.TestCase):
    @patch("bot.apps.feishu.api.request_json")
    def test_token_pagination_and_send(self, request):
        request.side_effect = [
            {"code": 0, "access_token": "test-token", "expires_in": 3600},
            {"code": 0, "data": {"items": [{"message_id": "om_1"}], "has_more": True, "page_token": "next"}},
            {"code": 0, "data": {"items": [{"message_id": "om_2"}], "has_more": False}},
            {"code": 0, "data": {"message_id": "om_sent"}},
        ]
        client = FeishuClient("app", "secret")
        self.assertEqual([item["message_id"] for item in client.history("oc_test", 1, 9)], ["om_1", "om_2"])
        self.assertEqual(client.send_text("oc_test", "开会", "request-id"), "om_sent")

        token_call, first, second, send = request.call_args_list
        form = parse_qs(token_call.kwargs["body"].decode())
        self.assertEqual(form, {"grant_type": ["client_credentials"], "client_id": ["app"], "client_secret": ["secret"]})
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(parse_qs(urlparse(second.args[0]).query)["page_token"], ["next"])
        body = json.loads(send.kwargs["body"])
        self.assertEqual(body["uuid"], "request-id")
        self.assertEqual(json.loads(body["content"]), {"text": "开会"})


class OfficeAssistantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        store = SQLiteStore()
        self.addCleanup(store.close)
        self.core = ChatCore(store)
        self.client = Mock(spec=FeishuClient)
        self.prompts = []
        self.clock = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)

        async def summarize(prompt):
            self.prompts.append(prompt)
            return "后续：准备材料（om_1）"

        OfficeAssistant(self.core, self.client, {"项目": "oc_a", "运营": "oc_b"}, summarize, lambda: self.clock)
        self.conversation = self.core.open(["operator"])

    async def ask(self, text):
        return (await self.core.submit(self.conversation.id, "operator", OFFICE, text)).text

    async def test_daily_and_pending_use_today_and_report_unavailable_groups(self):
        def history(chat, start, end):
            china = timezone(timedelta(hours=8))
            self.assertEqual(datetime.fromtimestamp(start, china).hour, 0)
            self.assertEqual(end, int(self.clock.timestamp()))
            if chat == "oc_b":
                raise ServiceError("permission denied")
            return [{"msg_type": "text", "message_id": "om_1", "body": {"content": '{"text":"准备材料"}'}}]

        self.client.history.side_effect = history
        summary = await self.ask("帮我总结一下今天的消息")
        self.assertIn("今日群聊总结 · 2026-11-24", summary)
        self.assertIn("后续：准备材料（om_1）", summary)
        self.assertIn("permission denied", summary)
        self.assertIn("关键进展", self.prompts[0])
        pending = await self.ask("今天有哪些工作要跟进")
        self.assertIn("今日待办和跟进", pending)
        self.assertIn("截止时间", self.prompts[1])
        self.client.send_text.assert_not_called()

    async def test_group_send_is_explicit_and_reports_partial_results(self):
        def send(chat, text, request_id):
            if chat == "oc_b":
                raise ServiceError("timed out")
            return "om_sent"

        self.client.send_text.side_effect = send
        await self.ask("群发 项目,未知 | 请查收")
        await self.ask("群发 项目")
        self.client.send_text.assert_not_called()
        result = await self.ask("群发 项目,项目,运营 | 请查收")
        self.assertIn("项目：已发送（om_sent）", result)
        self.assertIn("运营：未确认发送（timed out）", result)
        calls = self.client.send_text.call_args_list
        self.assertEqual({(call.args[0], call.args[1]) for call in calls}, {("oc_a", "请查收"), ("oc_b", "请查收")})
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].args[2], calls[1].args[2])
        self.assertEqual(self.prompts, [])

    @patch("bot.apps.feishu.assistant._CHUNK_CHARS", 1)
    async def test_pending_uses_mentions_and_reconciles_multiple_batches(self):
        self.client.history.side_effect = lambda chat, start, end: [
            {
                "msg_type": "text", "message_id": "om_1",
                "body": {"content": '{"text":"@_user_1 准备材料"}'},
                "mentions": [{"key": "@_user_1", "name": "小林"}],
            },
            {"msg_type": "text", "message_id": "om_2", "body": {"content": '{"text":"材料已交"}'}},
        ] if chat == "oc_a" else []
        await self.ask("今天有哪些待办")
        first, second, combined = self.prompts
        self.assertIn('"@_user_1": "小林"', first)
        self.assertIn("材料已交", second)
        self.assertIn("单列已完成、取消或改期的状态更新", second)
        self.assertIn("已完成或取消的事项从待办移除", combined)
