"""Preload backoff and deferred queue on Telegram 429."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tg
from bridge import MaxToTelegramBridge
from state import BridgeState


def _chat(chat_id, **kwargs):
    defaults = {
        "type": "CHAT",
        "title": f"Chat {chat_id}",
        "participants": {},
        "last_message": None,
        "last_event_time": 1000,
        "cid": chat_id,
    }
    defaults.update(kwargs)
    return SimpleNamespace(id=chat_id, **defaults)


class PreloadRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, tmp):
        bridge = MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "telegram_preload_topics": True,
            "telegram_seed_last_messages": False,
            "telegram_preload_chat_delay_seconds": 0,
            "telegram_preload_api_min_interval_seconds": 0,
            "telegram_api_min_interval_seconds": 0,
        })
        bridge._state = BridgeState(Path(tmp) / "state.json")
        return bridge

    async def test_preload_retries_after_rate_limit(self):
        client = Mock()
        client.chats = [_chat(100)]
        attempts = {"n": 0}

        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)

            async def ensure_side_effect(max_chat_id, title, chat_type, sender):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise tg.RateLimitError("limited", 0.01)
                bridge._state.save_topic(
                    max_chat_id,
                    thread_id=41,
                    title=title,
                    chat_type=chat_type,
                    sender=sender,
                )
                return -100222, 41, True

            with patch.object(bridge, "_telegram_target", side_effect=ensure_side_effect), \
                    patch.object(bridge, "_sync_chat_meta",
                                 new=AsyncMock(return_value=("T", "chat", "X"))), \
                    patch("bridge.tg.set_api_min_interval"), \
                    patch("bridge.asyncio.sleep", new_callable=AsyncMock):
                await bridge._preload_topics(client)

            self.assertGreaterEqual(attempts["n"], 2)
            self.assertEqual(bridge._state.get_topic(100)["telegram_thread_id"], 41)

    async def test_failed_preload_defers_chat_for_next_run(self):
        client = Mock()
        client.chats = [_chat(200)]

        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            with patch.object(
                bridge, "_telegram_target",
                side_effect=tg.RateLimitError("limited", 0.01),
            ), patch.object(
                bridge, "_sync_chat_meta",
                new=AsyncMock(return_value=("T", "chat", "X")),
            ), patch("bridge.tg.set_api_min_interval"), \
                    patch("bridge.asyncio.sleep", new_callable=AsyncMock):
                await bridge._preload_topics(client)

            self.assertIn("200", bridge._state.get_pending_preload_chat_ids())

    async def test_prepend_deferred_puts_pending_first(self):
        client = Mock()
        client.chats = [_chat(1), _chat(2)]
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            bridge._state.add_pending_preload_chat(2)
            merged = bridge._prepend_deferred_preload_chats(
                client, [_chat(1), _chat(2)],
            )
            self.assertEqual([c.id for c in merged], [2, 1])


if __name__ == "__main__":
    unittest.main()
