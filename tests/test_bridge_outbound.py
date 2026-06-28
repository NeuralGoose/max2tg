"""Stage 4: Telegram -> MAX outbound text/reply via client.send_message."""
import unittest
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
        client.send_message = AsyncMock(return_value=None)
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


if __name__ == "__main__":
    unittest.main()
