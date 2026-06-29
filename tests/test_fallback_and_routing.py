"""Legacy single-chat (no-topic) mode and Telegram->MAX update routing.

These cover the deprecated-but-live fallback path (all MAX chats into one
Telegram chat) and the _handle_update command/reply routing matrix, which were
previously untested.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import MaxToTelegramBridge
from formatting import FormattedText
from state import BridgeState


def _user(first="Иван", last="Петров"):
    return SimpleNamespace(
        names=[SimpleNamespace(first_name=first, last_name=last, name=None)])


def _chat(title="Семья", type_="CHAT"):
    return SimpleNamespace(title=title, type=type_)


def _message(*, id=1, chat_id=555, sender=7, text="привет"):
    return SimpleNamespace(id=id, chat_id=chat_id, sender=sender, text=text,
                           attaches=[], model_extra={}, stats=None)


def _client():
    client = Mock()
    client.get_user = AsyncMock(return_value=_user())
    client.get_chat = AsyncMock(return_value=_chat())
    return client


class FallbackModeTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, tmp):
        # No forum_chat_id + topics disabled => legacy single-chat mode.
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_topics_enabled": False,
            "max_login_token": "max",
        }
        bridge = MaxToTelegramBridge(config)
        bridge._state = BridgeState(Path(tmp) / "state.json")
        bridge._own_id = 999
        return bridge

    async def test_forward_goes_to_single_chat_with_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = _client()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic") as create, \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(_message(), client)

            create.assert_not_called()  # no topics in fallback mode
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1], 111)
            body = send.call_args.args[2]
            self.assertIn("MAX |", body)
            self.assertIn("(чат 555)", body)
            self.assertIn("привет", body)
            self.assertIsNone(send.call_args.kwargs.get("message_thread_id"))
            self.assertIn(10, bridge._reply_map)

    async def test_dedup_persists_without_topic_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            bridge = self._bridge(tmp)
            bridge._state = BridgeState(state_path)
            client = _client()
            bridge._client = client
            with patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(id=42), client)

            self.assertTrue(bridge._state.is_delivered(555, 42))

            # Simulate a restart: a fresh state from the same file must remember
            # the delivery so the message is not forwarded again.
            reloaded = BridgeState(state_path)
            self.assertTrue(reloaded.is_delivered(555, 42))

            bridge2 = self._bridge(tmp)
            bridge2._state = reloaded
            bridge2._hydrate_delivered_cache()
            bridge2._client = _client()
            with patch("bridge.tg.send_message", return_value=11) as send2:
                await bridge2._on_message(_message(id=42), bridge2._client)
            send2.assert_not_called()


class HandleUpdateRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        }
        bridge = MaxToTelegramBridge(config)
        bridge._client = Mock()
        return bridge

    def _result(self):
        return SimpleNamespace(text="ok", outbound_chat_id=None,
                               outbound_message_id=None)

    async def test_non_owner_command_in_forum_rejected(self):
        bridge = self._bridge()
        update = {"message": {"chat": {"id": -100222}, "from": {"id": 999},
                              "text": "/find @x"}}
        with patch("bridge.maxactions.find",
                   new=AsyncMock(return_value=self._result())) as find, \
                patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        find.assert_not_awaited()

    async def test_owner_command_in_forum_allowed(self):
        bridge = self._bridge()
        update = {"message": {"chat": {"id": -100222}, "from": {"id": 111},
                              "text": "/find @x"}}
        with patch("bridge.maxactions.find",
                   new=AsyncMock(return_value=self._result())) as find, \
                patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        find.assert_awaited_once()

    async def test_reply_routes_to_mapped_target(self):
        bridge = self._bridge()
        bridge._reply_map[50] = {
            "chat_id": 555, "message_id": 42, "sender": "Иван",
            "telegram_chat_id": 111, "message_thread_id": None,
        }
        update = {"message": {"chat": {"id": 111}, "from": {"id": 111},
                              "text": "ответ",
                              "reply_to_message": {"message_id": 50}}}
        with patch.object(bridge, "_send_telegram_update_to_max",
                          new=AsyncMock()) as route:
            await bridge._handle_update(update)
        route.assert_awaited_once()
        self.assertEqual(route.await_args.args[0]["chat_id"], 555)

    async def test_loose_message_gets_hint(self):
        bridge = self._bridge()
        update = {"message": {"chat": {"id": 111}, "from": {"id": 111},
                              "text": "просто какой-то текст"}}
        with patch("bridge.tg.send_message", return_value=1) as send:
            await bridge._handle_update(update)
        send.assert_called_once()
        self.assertIn("Reply", send.call_args.args[2])


class PartialDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_topics_enabled": False,
            "max_login_token": "max",
        }
        return MaxToTelegramBridge(config)

    def _photo(self):
        return SimpleNamespace(kind="photo", url="https://cdn/p.jpg", text="",
                               file_id=None, video_id=None, filename=None,
                               size=None)

    async def test_media_failure_reports_not_fully_delivered(self):
        bridge = self._bridge()
        # msg_id present (a placeholder note went out) but ok=False.
        with patch.object(bridge, "_send_media_item",
                          new=AsyncMock(return_value=(True, 10, False))):
            delivered, _first, fully = await bridge._deliver_to_telegram(
                Mock(), "MAX | A (chat 5)", FormattedText.plain(""),
                [self._photo()], 5, 1, "A",
                111, None, in_topic=False, is_channel=False)
        self.assertTrue(delivered)
        self.assertFalse(fully)

    async def test_media_success_reports_fully_delivered(self):
        bridge = self._bridge()
        with patch.object(bridge, "_send_media_item",
                          new=AsyncMock(return_value=(True, 10, True))):
            delivered, _first, fully = await bridge._deliver_to_telegram(
                Mock(), "MAX | A (chat 5)", FormattedText.plain(""),
                [self._photo()], 5, 1, "A",
                111, None, in_topic=False, is_channel=False)
        self.assertTrue(delivered)
        self.assertTrue(fully)


if __name__ == "__main__":
    unittest.main()
