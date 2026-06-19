import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maxactions  # noqa: E402


class NormLinkTests(unittest.TestCase):
    def test_group_invite_hash(self):
        self.assertEqual(maxactions._norm_link("https://max.ru/join/Ab9_xZ"), "join/Ab9_xZ")
        self.assertEqual(maxactions._norm_link("join/Ab9_xZ"), "join/Ab9_xZ")

    def test_channel_or_user_link(self):
        self.assertEqual(maxactions._norm_link("https://max.ru/durov"), "https://max.ru/durov")
        self.assertEqual(maxactions._norm_link("max.ru/durov"), "https://max.ru/durov")

    def test_bare_username(self):
        self.assertEqual(maxactions._norm_link("@durov"), "https://max.ru/durov")
        self.assertEqual(maxactions._norm_link("durov"), "https://max.ru/durov")

    def test_rejects_garbage(self):
        self.assertIsNone(maxactions._norm_link("привет мир"))
        self.assertIsNone(maxactions._norm_link("ab"))

    def test_join_in_query_not_misread_as_invite(self):
        # 'join/' inside a query string must NOT be treated as a group invite.
        self.assertEqual(maxactions._norm_link("https://max.ru/news?ref=join/x"),
                         "https://max.ru/news")


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_subscribes_and_reports_title(self):
        client = AsyncMock()
        client.invoke_method.side_effect = [
            {"payload": {"chat": {"id": -123, "title": "Канал Х"}}},  # opcode 57
            {"payload": {}},  # opcode 75 subscribe
        ]
        res = await maxactions.join(client, "@kanalx")
        self.assertIn("вступили", res.text.lower())
        self.assertIn("Канал Х", res.text)
        self.assertEqual(client.invoke_method.call_args_list[0].kwargs["opcode"], 57)
        sub = client.invoke_method.call_args_list[1].kwargs
        self.assertEqual(sub["opcode"], 75)
        self.assertTrue(sub["payload"]["subscribe"])

    async def test_join_bad_link(self):
        client = AsyncMock()
        res = await maxactions.join(client, "не ссылка")
        self.assertIn("ссылк", res.text.lower())
        client.invoke_method.assert_not_called()

    async def test_join_reports_max_error(self):
        client = AsyncMock(invoke_method=AsyncMock(return_value={"payload": {"error": "not.found"}}))
        res = await maxactions.join(client, "@nope")
        self.assertIn("не дал вступить", res.text)


class FindTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_search_opcode_46(self):
        client = AsyncMock(invoke_method=AsyncMock(return_value={
            "payload": {"contact": {"id": 777, "names": [{"name": "Пётр"}]}}}))
        res = await maxactions.find(client, "+7 999 123-45-67")
        self.assertIn("Пётр", res.text)
        self.assertIn("777", res.text)
        call = client.invoke_method.call_args.kwargs
        self.assertEqual(call["opcode"], 46)
        self.assertEqual(call["payload"]["phone"], "+79991234567")

    async def test_phone_bare_8_normalized_to_plus7(self):
        client = AsyncMock(invoke_method=AsyncMock(return_value={
            "payload": {"contact": {"id": 5, "names": [{"name": "X"}]}}}))
        await maxactions.find(client, "89991234567")
        self.assertEqual(client.invoke_method.call_args.kwargs["payload"]["phone"], "+79991234567")

    async def test_numeric_id_resolves(self):
        client = AsyncMock()
        with patch.object(maxactions, "resolve_users",
                          new=AsyncMock(return_value={"payload": {"contacts": [
                              {"names": [{"firstName": "Ольга", "lastName": "Лебедева"}]}]}})):
            res = await maxactions.find(client, "24720322")
        self.assertIn("Ольга Лебедева", res.text)

    async def test_username_via_opcode_89(self):
        client = AsyncMock(invoke_method=AsyncMock(
            return_value={"payload": {"chat": {"id": 555, "title": "Channel"}}}))
        res = await maxactions.find(client, "@channel")
        self.assertIn("Channel", res.text)
        self.assertEqual(client.invoke_method.call_args.kwargs["opcode"], 89)

    async def test_freetext_name_not_wired(self):
        client = AsyncMock()
        res = await maxactions.find(client, "департамент культуры Липецк")
        self.assertIn("названи", res.text.lower())
        client.invoke_method.assert_not_called()    # must NOT send a guessed opcode

    async def test_overlong_query_rejected(self):
        client = AsyncMock()
        res = await maxactions.find(client, "1" * 100)
        self.assertIn("длинн", res.text.lower())
        client.invoke_method.assert_not_called()


class StartDmTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_is_disabled_and_sends_nothing(self):
        # /dm must NOT contact MAX: dialog chatId != user_id, so sending by id
        # could reach the wrong person. It only returns an explanation.
        client = AsyncMock()
        res = await maxactions.start_dm(client, "999", "привет")
        self.assertIn("отключ", res.text.lower())
        client.invoke_method.assert_not_called()


if __name__ == "__main__":
    unittest.main()
