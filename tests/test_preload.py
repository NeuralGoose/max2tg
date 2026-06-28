"""Preload chat discovery and message depth seeding."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import MaxToTelegramBridge
from config import normalize_config
from state import BridgeState


def _chat(chat_id, *, last_event_time=1000, last_message=None):
    return SimpleNamespace(
        id=chat_id,
        type="CHAT",
        title=f"Chat {chat_id}",
        participants={},
        last_message=last_message,
        last_event_time=last_event_time,
        cid=chat_id,
    )


def _message(mid, *, time=100, text="hi", sender=7):
    return SimpleNamespace(
        id=mid, time=time, text=text, sender=sender, attaches=[], chat_id=100,
    )


class PreloadConfigTests(unittest.TestCase):
    def test_preload_defaults(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
        })
        self.assertEqual(config["telegram_preload_chat_source"], "login")
        self.assertEqual(config["telegram_preload_message_depth"], 1)
        self.assertEqual(config["telegram_preload_fetch_pages"], 20)
        self.assertEqual(config["telegram_preload_chat_delay_seconds"], 0.35)

    def test_invalid_chat_source_falls_back_to_login(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
            "telegram_preload_chat_source": "bogus",
        })
        self.assertEqual(config["telegram_preload_chat_source"], "login")

    def test_message_depth_clamped(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
            "telegram_preload_message_depth": 999,
        })
        self.assertEqual(config["telegram_preload_message_depth"], 50)


class CollectPreloadChatsTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, **overrides):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "telegram_preload_chat_count": 100,
            "telegram_preload_chat_source": "login",
        }
        config.update(overrides)
        return MaxToTelegramBridge(config)

    async def test_login_source_uses_client_chats_only(self):
        bridge = self._bridge(telegram_preload_chat_source="login")
        client = Mock()
        client.chats = [_chat(1), _chat(2)]
        client.fetch_chats = AsyncMock()
        chats, discovered = await bridge._collect_preload_chats(client)
        self.assertEqual(discovered, 2)
        self.assertEqual([c.id for c in chats], [1, 2])
        client.fetch_chats.assert_not_called()

    async def test_fetch_source_merges_and_dedupes(self):
        bridge = self._bridge(
            telegram_preload_chat_source="fetch",
            telegram_preload_chat_count=10,
        )
        client = Mock()
        client.chats = [_chat(100, last_event_time=5000)]
        client.fetch_chats = AsyncMock(side_effect=[
            [_chat(200, last_event_time=3000), _chat(100, last_event_time=4000)],
            [],
        ])
        chats, discovered = await bridge._collect_preload_chats(client)
        self.assertEqual(discovered, 2)
        self.assertEqual([c.id for c in chats], [100, 200])
        client.fetch_chats.assert_awaited()

    async def test_collect_skips_excluded_chat_zero(self):
        bridge = self._bridge(telegram_preload_chat_source="login")
        client = Mock()
        client.chats = [
            _chat(0),
            _chat(100),
        ]
        chats, discovered = await bridge._collect_preload_chats(client)
        self.assertEqual(discovered, 1)
        self.assertEqual([c.id for c in chats], [100])

    async def test_fetch_respects_chat_count_limit(self):
        bridge = self._bridge(
            telegram_preload_chat_source="login",
            telegram_preload_chat_count=1,
        )
        client = Mock()
        client.chats = [_chat(1), _chat(2)]
        chats, discovered = await bridge._collect_preload_chats(client)
        self.assertEqual(discovered, 2)
        self.assertEqual(len(chats), 1)


class SeedChatMessagesTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, **overrides):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "telegram_seed_last_messages": True,
        }
        config.update(overrides)
        bridge = MaxToTelegramBridge(config)
        bridge._own_id = 999
        return bridge

    async def test_depth_zero_skips_sends(self):
        bridge = self._bridge()
        with patch("bridge.tg.send_message", return_value=10) as send:
            count = await bridge._seed_chat_messages(
                Mock(), 100, 41, _chat(100), depth=0,
            )
        self.assertEqual(count, 0)
        send.assert_not_called()

    async def test_depth_one_uses_fetch_history(self):
        bridge = self._bridge()
        last = _message("m1", text="last only")
        chat = _chat(100, last_message=last)
        client = Mock()
        client.fetch_history = AsyncMock(return_value=[last])
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            with patch("bridge.tg.send_message", return_value=10) as send:
                count = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=1,
                )
        client.fetch_history.assert_awaited_once_with(100, backward=1)
        self.assertEqual(count, 1)
        send.assert_called_once()
        self.assertIn("last only", send.call_args.args[2])

    async def test_depth_gt_one_fetches_history_oldest_first(self):
        bridge = self._bridge(telegram_preload_message_depth=3)
        chat = _chat(100)
        client = Mock()
        client.fetch_history = AsyncMock(return_value=[
            _message("m3", time=300, text="third"),
            _message("m1", time=100, text="first"),
            _message("m2", time=200, text="second"),
        ])
        bodies = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")

            def capture_send(*args, **kwargs):
                bodies.append(args[2])
                return 10 + len(bodies)

            with patch("bridge.tg.send_message", side_effect=capture_send), \
                    patch.object(
                        bridge, "_resolve_sender_name",
                        new=AsyncMock(return_value="Ivan"),
                    ):
                count = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=3,
                )

        client.fetch_history.assert_awaited_once_with(100, backward=3)
        self.assertEqual(count, 3)
        self.assertEqual(
            [b for b in bodies if "first" in b or "second" in b or "third" in b],
            [bodies[0], bodies[1], bodies[2]],
        )
        self.assertIn("first", bodies[0])
        self.assertIn("second", bodies[1])
        self.assertIn("third", bodies[2])

    async def test_equal_timestamp_newest_first_api_order(self):
        """Stable-sort tie on time must not preserve newest-first API order."""
        bridge = self._bridge()
        chat = _chat(100)
        new_id = 116742887450236083
        mid_id = 116741887450236083
        old_id = 116739188629507992
        client = Mock()
        client.fetch_history = AsyncMock(return_value=[
            _message(new_id, time=1000, text="newest"),
            _message(mid_id, time=1000, text="middle"),
            _message(old_id, time=1000, text="oldest"),
        ])
        bodies = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")

            def capture_send(*args, **kwargs):
                bodies.append(args[2])
                return 10 + len(bodies)

            with patch("bridge.tg.send_message", side_effect=capture_send), \
                    patch.object(
                        bridge, "_resolve_sender_name",
                        new=AsyncMock(return_value="Ivan"),
                    ):
                count = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=3,
                )

        self.assertEqual(count, 3)
        self.assertIn("oldest", bodies[0])
        self.assertIn("middle", bodies[1])
        self.assertIn("newest", bodies[2])

    async def test_partial_reseed_guard_skips_older_after_newest(self):
        bridge = self._bridge()
        chat = _chat(100)
        new_id = 116742887450236083
        old_id = 116739188629507992
        client = Mock()
        client.fetch_history = AsyncMock(return_value=[
            _message(new_id, time=2000, text="newest"),
            _message(old_id, time=1000, text="oldest"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            bridge._state.mark_seeded_message(
                100, max_message_id=new_id, telegram_message_id=99,
            )
            with patch("bridge.tg.send_message", return_value=10) as send:
                count = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=2,
                )
        self.assertEqual(count, 0)
        send.assert_not_called()

    async def test_forward_waits_while_seed_holds_topic_lock(self):
        bridge = self._bridge()
        chat = _chat(100)
        client = Mock()
        seed_started = asyncio.Event()
        release_seed = asyncio.Event()
        forward_started = asyncio.Event()

        async def slow_seed(*args, **kwargs):
            seed_started.set()
            await release_seed.wait()
            return 0

        live_message = _message(999, text="live")
        live_message.chat_id = 100

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            bridge._client = client

            async def run_seed():
                with patch.object(
                    bridge, "_seed_one_message", side_effect=slow_seed,
                ):
                    client.fetch_history = AsyncMock(return_value=[
                        _message(1, text="seed"),
                    ])
                    return await bridge._seed_chat_messages(
                        client, 100, 41, chat, depth=1,
                    )

            async def run_forward():
                await seed_started.wait()
                forward_started.set()
                with patch.object(
                    bridge, "_deliver_to_telegram",
                    new=AsyncMock(return_value=(True, 50, True)),
                ) as deliver:
                    await bridge._handle_incoming_message(
                        live_message, client, edited=False,
                    )
                    return deliver

            seed_task = asyncio.create_task(run_seed())
            await seed_started.wait()
            forward_task = asyncio.create_task(run_forward())
            await forward_started.wait()
            await asyncio.sleep(0.05)
            self.assertFalse(forward_task.done())
            release_seed.set()
            deliver = await asyncio.gather(seed_task, forward_task)
            deliver[1].assert_awaited()

    async def test_second_preload_pass_skips_delivered(self):
        bridge = self._bridge()
        last = _message("m1", text="once")
        chat = _chat(100, last_message=last)
        client = Mock()
        client.fetch_history = AsyncMock(return_value=[last])
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            with patch("bridge.tg.send_message", return_value=10) as send:
                first = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=1,
                )
                second = await bridge._seed_chat_messages(
                    client, 100, 41, chat, depth=1,
                )
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        send.assert_called_once()


class PreloadTopicsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_preload_with_depth_zero_creates_topics_without_sends(self):
        bridge = MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "telegram_preload_topics": True,
            "telegram_seed_last_messages": True,
            "telegram_preload_message_depth": 0,
            "telegram_preload_chat_delay_seconds": 0,
        })
        client = Mock()
        client.chats = [
            _chat(100, last_message=_message("m1", text="skip me")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", return_value=41), \
                    patch("bridge.tg.send_message", return_value=10) as send, \
                    patch.object(
                        bridge, "_sync_chat_meta",
                        new=AsyncMock(return_value=("T", "chat", "Ivan")),
                    ):
                await bridge._preload_topics(client)
            send.assert_not_called()
            self.assertEqual(bridge._state.get_topic(100)["telegram_thread_id"], 41)


if __name__ == "__main__":
    unittest.main()
