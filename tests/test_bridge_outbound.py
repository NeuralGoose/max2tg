"""Stage 4: Telegram -> MAX outbound text/reply via client.send_message."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import MaxToTelegramBridge


class OutboundTextTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, **overrides):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
        }
        config.update(overrides)
        return MaxToTelegramBridge(config)

    def _client(self):
        client = Mock()
        client.send_message = AsyncMock(return_value=SimpleNamespace(id=77))
        client.edit_message = AsyncMock()
        client.add_reaction = AsyncMock()
        client.remove_reaction = AsyncMock()
        return client

    def _target(self, **overrides):
        target = {"chat_id": 555, "message_id": 42, "sender": "Иван",
                  "telegram_chat_id": 111, "message_thread_id": None}
        target.update(overrides)
        return target

    async def test_reply_includes_reply_to(self):
        bridge = self._bridge()
        bridge._client = self._client()
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._send_reply_to_max(self._target(), "привет")
        bridge._client.send_message.assert_awaited_once_with(
            555, "привет", reply_to=42)

    async def test_send_without_message_id_uses_no_reply(self):
        bridge = self._bridge()
        bridge._client = self._client()
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._send_reply_to_max(self._target(message_id=None), "hi")
        bridge._client.send_message.assert_awaited_once_with(
            555, "hi", reply_to=None)

    async def test_client_none_notifies_telegram(self):
        bridge = self._bridge()
        bridge._client = None
        with patch("bridge.tg.send_message") as send:
            await bridge._send_reply_to_max(self._target(), "hi")
        send.assert_called_once()
        self.assertIn("не подключ", send.call_args.args[2].lower())

    async def test_error_falls_back_to_telegram_note(self):
        bridge = self._bridge()
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        bridge._client = client
        with patch("bridge.tg.send_message") as send:
            await bridge._send_reply_to_max(self._target(), "hi")
        send.assert_called_once()
        self.assertIn("Не удалось", send.call_args.args[2])

    async def test_confirm_sent_true_posts_confirmation(self):
        bridge = self._bridge(telegram_confirm_sent=True)
        bridge._client = self._client()
        with patch("bridge.tg.send_message") as send:
            await bridge._send_reply_to_max(self._target(), "hi")
        send.assert_called_once()
        self.assertIn("Отправлено в MAX", send.call_args.args[2])

    async def test_confirm_sent_false_suppresses_confirmation(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        with patch("bridge.tg.send_message") as send:
            await bridge._send_reply_to_max(self._target(), "hi")
        send.assert_not_called()

    async def test_telegram_reply_routes_to_mapped_max_message(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._reply_map[100] = self._target(message_id=42)
        update = {"message": {
            "chat": {"id": 111},
            "reply_to_message": {"message_id": 100},
            "text": "ответ",
        }}
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        bridge._client.send_message.assert_awaited_once_with(
            555, "ответ", reply_to=42)

    async def test_telegram_bold_reply_converts_to_markdown(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._reply_map[100] = self._target(message_id=42)
        update = {"message": {
            "chat": {"id": 111},
            "reply_to_message": {"message_id": 100},
            "text": "bold",
            "entities": [{"type": "bold", "offset": 0, "length": 4}],
        }}
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        bridge._client.send_message.assert_awaited_once_with(
            555, "**bold**", reply_to=42)

    async def test_telegram_multi_entity_reply_converts_to_markdown(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._reply_map[100] = self._target(message_id=42)
        line = "Съешь ещё этих мягких французских булок, да выпей же чаю"
        from formatting import py_index_to_utf16, utf16_len

        def off(substring: str) -> int:
            return py_index_to_utf16(line, line.index(substring))

        update = {"message": {
            "chat": {"id": 111},
            "reply_to_message": {"message_id": 100},
            "text": line,
            "entities": [
                {"type": "underline", "offset": off("ещё"), "length": utf16_len("ещё")},
                {"type": "bold", "offset": off("мягких"), "length": utf16_len("мягких")},
                {
                    "type": "bold",
                    "offset": off("французских"),
                    "length": utf16_len("французских"),
                },
                {
                    "type": "italic",
                    "offset": off("французских"),
                    "length": utf16_len("французских"),
                },
                {
                    "type": "underline",
                    "offset": off("булок"),
                    "length": utf16_len("булок"),
                },
                {
                    "type": "strikethrough",
                    "offset": off("да выпей же"),
                    "length": utf16_len("да выпей же"),
                },
            ],
        }}
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        sent_text = bridge._client.send_message.await_args.args[1]
        self.assertIn("**_французских_**", sent_text)
        self.assertNotIn("булокк", sent_text)
        self.assertNotIn("жее", sent_text)

    async def test_tg_edited_message_updates_max(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._tg_sent_to_max[100] = {
            "max_chat_id": 555,
            "max_message_id": 42,
        }
        update = {"edited_message": {
            "message_id": 100,
            "text": "updated",
        }}
        await bridge._handle_update(update)
        bridge._client.edit_message.assert_awaited_once_with(
            555, 42, "updated",
        )

    async def test_tg_reaction_updates_max(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._tg_sent_to_max[100] = {
            "max_chat_id": 555,
            "max_message_id": 42,
        }
        update = {"message_reaction": {
            "message_id": 100,
            "user": {"id": 111},
            "new_reaction": [{"type": "emoji", "emoji": "👍"}],
        }}
        await bridge._handle_update(update)
        bridge._client.add_reaction.assert_awaited_once_with(
            555, "42", "👍",
        )

    async def test_outbound_reply_records_tg_sent_map(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._send_reply_to_max(
                self._target(), "привет", tg_message_id=100,
            )
        self.assertEqual(bridge._tg_sent_to_max[100]["max_message_id"], 77)

    async def test_telegram_quote_reply_prefixes_max_text(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._reply_map[100] = self._target(message_id=42)
        update = {"message": {
            "chat": {"id": 111},
            "reply_to_message": {
                "message_id": 100,
                "text": "исходное сообщение",
            },
            "quote": {"text": "исходное"},
            "text": "мой ответ",
        }}
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        sent_text = bridge._client.send_message.await_args.args[1]
        self.assertIn("мой ответ", sent_text)
        self.assertIn("исходное", sent_text)
        self.assertIn("ответ на", sent_text.lower())
        bridge._client.send_message.assert_awaited_once_with(
            555, sent_text, reply_to=42)

    async def test_telegram_reply_to_message_snippet_when_no_quote_field(self):
        bridge = self._bridge(telegram_confirm_sent=False)
        bridge._client = self._client()
        bridge._reply_map[100] = self._target(message_id=42)
        update = {"message": {
            "chat": {"id": 111},
            "reply_to_message": {
                "message_id": 100,
                "text": "родительский текст",
            },
            "text": "ответ без quote",
        }}
        with patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        sent_text = bridge._client.send_message.await_args.args[1]
        self.assertIn("родительский текст", sent_text)
        self.assertIn("ответ без quote", sent_text)
        self.assertIn("ответ на", sent_text.lower())


if __name__ == "__main__":
    unittest.main()
