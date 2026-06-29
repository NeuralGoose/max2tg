"""Excluded MAX chats and locale-key system messages."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import MaxToTelegramBridge
from config import normalize_config
from state import BridgeState


def _message(mid, *, text="hi", chat_id=100, sender=7):
    return SimpleNamespace(
        id=mid, time=100, text=text, sender=sender, attaches=[], chat_id=chat_id,
    )


class ExcludedChatConfigTests(unittest.TestCase):
    def test_default_excludes_chat_zero(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
        })
        self.assertIn(0, config["telegram_exclude_chat_ids"])

    def test_parse_exclude_list_from_string(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
            "telegram_exclude_chat_ids": "0, 999",
        })
        self.assertEqual(config["telegram_exclude_chat_ids"], frozenset({0, 999}))


class ExcludedChatBridgeTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self):
        bridge = MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "telegram_exclude_chat_ids": frozenset({0}),
        })
        bridge._own_id = 999
        bridge._client = Mock()
        return bridge

    async def test_collect_preload_skips_chat_zero(self):
        bridge = self._bridge()
        client = Mock()
        client.chats = [
            SimpleNamespace(id=0, type="DIALOG", title="", participants={}),
            SimpleNamespace(id=100, type="CHAT", title="Real", participants={}),
        ]
        chats, discovered = await bridge._collect_preload_chats(client)
        self.assertEqual(discovered, 1)
        self.assertEqual([c.id for c in chats], [100])

    async def test_locale_system_text_not_forwarded(self):
        bridge = self._bridge()
        message = _message("m1", text="welcome.saved.dialog.message", chat_id=100)
        with patch.object(bridge, "_forward", new=AsyncMock(return_value=True)) as forward:
            await bridge._handle_incoming_message(message, Mock())
        forward.assert_not_called()

    async def test_excluded_chat_live_message_ignored(self):
        bridge = self._bridge()
        message = _message("m1", text="secret note", chat_id=0)
        with patch.object(bridge, "_forward", new=AsyncMock(return_value=True)) as forward:
            await bridge._handle_incoming_message(message, Mock())
        forward.assert_not_called()

    async def test_locale_system_text_not_seeded(self):
        bridge = self._bridge()
        bridge._config["telegram_seed_last_messages"] = True
        message = _message("m1", text="welcome.saved.dialog.message")
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            with patch("bridge.tg.send_message", return_value=10) as send:
                seeded = await bridge._seed_one_message(Mock(), 100, 41, message)
        self.assertFalse(seeded)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
