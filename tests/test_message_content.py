"""Unit tests for forward resolution and content fingerprints."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from message_content import (
    FORWARD_FETCH_FAILED_FALLBACK,
    UNKNOWN_SENDER,
    message_content_fingerprint,
    resolve_message_content,
)


def _message(**kwargs):
    defaults = dict(
        id=1,
        chat_id=555,
        sender=None,
        text="",
        attaches=[],
        model_extra={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class ResolveMessageContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_link_fetches_original(self):
        wrapper = _message(
            model_extra={
                "link": {
                    "type": "FORWARD",
                    "chatId": 100,
                    "messageId": 200,
                },
            },
        )
        original = _message(
            id=200,
            chat_id=100,
            sender=42,
            text="Новость дня",
            attaches=[],
        )
        client = Mock()
        client.get_message = AsyncMock(return_value=original)

        async def resolve_name(uid):
            return "Иван Петров" if uid == 42 else str(uid)

        resolved = await resolve_message_content(
            wrapper,
            client,
            chat_type="chat",
            chat_title="Группа",
            own_id=999,
            resolve_sender_name=resolve_name,
        )

        client.get_message.assert_awaited_once_with(100, 200)
        self.assertEqual(resolved.text, "Новость дня")
        self.assertTrue(resolved.is_forward)
        self.assertEqual(resolved.author, "Иван Петров")
        self.assertEqual(resolved.attribution, "↪ Иван Петров")
        self.assertTrue(resolved.forward_attempted)

    async def test_forward_link_with_string_message_id(self):
        wrapper = _message(
            model_extra={
                "link": {
                    "type": "FORWARD",
                    "chatId": 100,
                    "messageId": "116742887450236083",
                },
            },
        )
        original = _message(
            id=116742887450236083,
            chat_id=100,
            sender=42,
            text="Длинный id",
            attaches=[],
        )
        client = Mock()
        client.get_message = AsyncMock(return_value=original)

        resolved = await resolve_message_content(
            wrapper,
            client,
            chat_type="chat",
            chat_title="Группа",
            own_id=999,
            resolve_sender_name=AsyncMock(return_value="Автор"),
        )

        client.get_message.assert_awaited_once_with(100, 116742887450236083)
        self.assertEqual(resolved.text, "Длинный id")

    async def test_forward_fetch_failure_sets_fallback(self):
        wrapper = _message(
            sender=7,
            model_extra={
                "link": {
                    "type": "FORWARD",
                    "chatId": 100,
                    "messageId": 200,
                },
            },
        )
        client = Mock()
        client.get_message = AsyncMock(return_value=None)

        resolved = await resolve_message_content(
            wrapper,
            client,
            chat_type="chat",
            chat_title="Группа",
            own_id=999,
            resolve_sender_name=AsyncMock(return_value="Иван Петров"),
        )

        self.assertTrue(resolved.is_forward)
        self.assertTrue(resolved.forward_attempted)
        self.assertEqual(resolved.text, FORWARD_FETCH_FAILED_FALLBACK)
        self.assertIn("↪", resolved.attribution or "")

    async def test_embedded_forward_link_uses_inline_message(self):
        wrapper = _message(
            sender=180016258,
            model_extra={
                "link": {
                    "type": "FORWARD",
                    "chatId": -73194865803385,
                    "message": {
                        "sender": 204749122,
                        "id": "116826476060678268",
                        "text": "Только что нашли погибшего в ДТП",
                        "type": "USER",
                        "attaches": [
                            {
                                "_type": "PHOTO",
                                "baseUrl": "https://i.oneme.ru/i?r=abc",
                            },
                        ],
                    },
                },
            },
        )
        client = Mock()
        client.get_message = AsyncMock()

        resolved = await resolve_message_content(
            wrapper,
            client,
            chat_type="chat",
            chat_title="Группа",
            own_id=999,
            resolve_sender_name=AsyncMock(
                side_effect=lambda uid: "Автор канала" if uid == 204749122 else str(uid)
            ),
        )

        client.get_message.assert_not_called()
        self.assertTrue(resolved.is_forward)
        self.assertIn("погибшего", resolved.text)
        self.assertEqual(len(resolved.attaches), 1)
        self.assertEqual(resolved.author, "Автор канала")
        self.assertIn("↪", resolved.attribution or "")

    async def test_channel_without_sender_uses_chat_title(self):
        message = _message(sender=None, text="Пост канала")
        client = Mock()

        resolved = await resolve_message_content(
            message,
            client,
            chat_type="channel",
            chat_title="Мой канал",
            own_id=999,
            resolve_sender_name=AsyncMock(return_value="x"),
        )

        self.assertEqual(resolved.author, "Мой канал")
        self.assertIsNone(resolved.attribution)

    async def test_empty_wrapper_without_link_degrades_gracefully(self):
        message = _message(sender=None, text="", attaches=[])
        client = Mock()

        resolved = await resolve_message_content(
            message,
            client,
            chat_type="group",
            chat_title="Группа",
            own_id=999,
            resolve_sender_name=AsyncMock(return_value="x"),
        )

        self.assertEqual(resolved.text, "")
        self.assertEqual(resolved.author, UNKNOWN_SENDER)
        self.assertFalse(resolved.is_forward)


class FingerprintTests(unittest.TestCase):
    def test_same_text_and_attaches_same_fingerprint(self):
        photo = SimpleNamespace(type="PHOTO", base_url="https://cdn/p.jpg")
        msg1 = _message(text="hello", attaches=[photo], stats={"comments": 1})
        msg2 = _message(text="hello", attaches=[photo], stats={"comments": 5})
        self.assertEqual(
            message_content_fingerprint(msg1),
            message_content_fingerprint(msg2),
        )

    def test_changed_text_different_fingerprint(self):
        msg1 = _message(text="old")
        msg2 = _message(text="new")
        self.assertNotEqual(
            message_content_fingerprint(msg1),
            message_content_fingerprint(msg2),
        )


if __name__ == "__main__":
    unittest.main()
