import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maxactions  # noqa: E402


def _user(uid, *, name=None, first=None, last=None):
    return SimpleNamespace(
        id=uid,
        names=[SimpleNamespace(name=name, first_name=first, last_name=last)])


def _chat(cid, title):
    return SimpleNamespace(id=cid, title=title)


def _me(uid):
    return SimpleNamespace(contact=SimpleNamespace(id=uid))


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
        self.assertEqual(maxactions._norm_link("https://max.ru/news?ref=join/x"),
                         "https://max.ru/news")


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_uses_join_channel(self):
        client = Mock()
        client.join_channel = AsyncMock(return_value=_chat(-123, "Канал Х"))
        client.join_group = AsyncMock()
        res = await maxactions.join(client, "@kanalx")
        self.assertIn("вступили", res.text.lower())
        self.assertIn("Канал Х", res.text)
        client.join_channel.assert_awaited_once_with("https://max.ru/kanalx")
        client.join_group.assert_not_called()

    async def test_group_invite_uses_join_group(self):
        client = Mock()
        client.join_group = AsyncMock(return_value=_chat(555, "Группа"))
        client.join_channel = AsyncMock()
        res = await maxactions.join(client, "https://max.ru/join/Ab9_xZ")
        self.assertIn("Группа", res.text)
        client.join_group.assert_awaited_once_with("join/Ab9_xZ")
        client.join_channel.assert_not_called()

    async def test_bad_link(self):
        client = Mock()
        client.join_channel = AsyncMock()
        client.join_group = AsyncMock()
        res = await maxactions.join(client, "не ссылка")
        self.assertIn("ссылк", res.text.lower())
        client.join_channel.assert_not_called()
        client.join_group.assert_not_called()

    async def test_reports_error(self):
        client = Mock()
        client.join_channel = AsyncMock(side_effect=RuntimeError("not.found"))
        res = await maxactions.join(client, "@nope")
        self.assertIn("не удалось вступить", res.text.lower())


class FindTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_search(self):
        client = Mock()
        client.search_by_phone = AsyncMock(return_value=_user(777, name="Пётр"))
        res = await maxactions.find(client, "+7 999 123-45-67")
        self.assertIn("Пётр", res.text)
        self.assertIn("777", res.text)
        client.search_by_phone.assert_awaited_once_with("+79991234567")

    async def test_phone_bare_8_normalized_to_plus7(self):
        client = Mock()
        client.search_by_phone = AsyncMock(return_value=_user(5, name="X"))
        await maxactions.find(client, "89991234567")
        client.search_by_phone.assert_awaited_once_with("+79991234567")

    async def test_phone_not_found(self):
        client = Mock()
        client.search_by_phone = AsyncMock(side_effect=RuntimeError("not found"))
        res = await maxactions.find(client, "+79991234567")
        self.assertIn("никто не найден", res.text.lower())

    async def test_numeric_id_resolves(self):
        client = Mock()
        client.get_user = AsyncMock(
            return_value=_user(24720322, first="Ольга", last="Лебедева"))
        res = await maxactions.find(client, "24720322")
        self.assertIn("Ольга Лебедева", res.text)
        client.get_user.assert_awaited_once_with(24720322)

    async def test_numeric_id_not_found(self):
        client = Mock()
        client.get_user = AsyncMock(return_value=None)
        res = await maxactions.find(client, "24720322")
        self.assertIn("не найден", res.text.lower())

    async def test_group_invite_link_resolves(self):
        client = Mock()
        client.resolve_group_by_link = AsyncMock(return_value=_chat(555, "Группа"))
        res = await maxactions.find(client, "https://max.ru/join/AbCdEf")
        self.assertIn("Группа", res.text)
        self.assertIn("555", res.text)
        client.resolve_group_by_link.assert_awaited_once_with("join/AbCdEf")

    async def test_username_link_points_to_join(self):
        client = Mock()
        client.resolve_group_by_link = AsyncMock()
        res = await maxactions.find(client, "@channel")
        self.assertIn("/join", res.text)
        client.resolve_group_by_link.assert_not_called()

    async def test_freetext_name_not_wired(self):
        client = Mock()
        res = await maxactions.find(client, "департамент культуры Липецк")
        self.assertIn("названи", res.text.lower())

    async def test_overlong_query_rejected(self):
        client = Mock()
        res = await maxactions.find(client, "1" * 100)
        self.assertIn("длинн", res.text.lower())


class StartDmTests(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        client = Mock()
        client.me = _me(100)
        client.get_chat_id = Mock(return_value=7268926)
        client.send_message = AsyncMock(
            return_value=SimpleNamespace(id="dm-msg-1"),
        )
        return client

    async def test_sends_via_get_chat_id_and_send_message(self):
        client = self._client()
        res = await maxactions.start_dm(client, "21243808", "привет")
        self.assertIn("Отправлено", res.text)
        client.get_chat_id.assert_called_once_with(100, 21243808)
        client.send_message.assert_awaited_once_with(7268926, "привет")
        self.assertEqual(res.outbound_chat_id, 7268926)
        self.assertEqual(res.outbound_message_id, "dm-msg-1")

    async def test_rejects_non_numeric_id(self):
        client = self._client()
        res = await maxactions.start_dm(client, "не-число", "привет")
        self.assertIn("числовой id", res.text)
        client.send_message.assert_not_called()

    async def test_rejects_empty_text(self):
        client = self._client()
        res = await maxactions.start_dm(client, "5", "   ")
        self.assertIn("Пустое", res.text)
        client.send_message.assert_not_called()

    async def test_not_logged_in_yet(self):
        client = self._client()
        client.me = None
        res = await maxactions.start_dm(client, "5", "привет")
        self.assertIn("подключается", res.text.lower())
        client.send_message.assert_not_called()

    async def test_surfaces_send_error(self):
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        res = await maxactions.start_dm(client, "5", "привет")
        self.assertIn("не удалось отправить", res.text.lower())


if __name__ == "__main__":
    unittest.main()
