import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bridge import MaxToTelegramBridge, _contact_display_name
from config import normalize_config
from state import BridgeState, normalize_topic_title


class DotenvTests(unittest.TestCase):
    def test_loads_file_but_does_not_override_real_env(self):
        import os

        import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                'MAX2TG_TEST_A=fromfile\n# comment\nMAX2TG_TEST_B="quoted"\n',
                encoding="utf-8",
            )
            os.environ.pop("MAX2TG_TEST_A", None)
            os.environ["MAX2TG_TEST_B"] = "realenv"
            try:
                config.apply_dotenv(path)
                self.assertEqual(os.environ["MAX2TG_TEST_A"], "fromfile")
                self.assertEqual(os.environ["MAX2TG_TEST_B"], "realenv")
            finally:
                os.environ.pop("MAX2TG_TEST_A", None)
                os.environ.pop("MAX2TG_TEST_B", None)

    def test_handles_export_prefix_and_inline_comment(self):
        import os

        import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                'export MAX2TG_TA=hello\nMAX2TG_TB=100  # a default\n'
                'MAX2TG_TC="q v"\n', encoding="utf-8")
            for k in ("MAX2TG_TA", "MAX2TG_TB", "MAX2TG_TC"):
                os.environ.pop(k, None)
            try:
                config.apply_dotenv(path)
                self.assertEqual(os.environ["MAX2TG_TA"], "hello")
                self.assertEqual(os.environ["MAX2TG_TB"], "100")
                self.assertEqual(os.environ["MAX2TG_TC"], "q v")
            finally:
                for k in ("MAX2TG_TA", "MAX2TG_TB", "MAX2TG_TC"):
                    os.environ.pop(k, None)


class StateSaveTests(unittest.TestCase):
    def test_falls_back_when_atomic_replace_fails(self):
        import json

        # A single-file bind mount in Docker makes tmp.replace() raise
        # EBUSY/EXDEV; save() must still persist via a direct write.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = BridgeState(path)
            with patch("pathlib.Path.replace", side_effect=OSError("EBUSY")):
                state.save_topic(123, thread_id=7, title="X", chat_type="dialog")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["topics"]["123"]["telegram_thread_id"], 7)


class ContactNameTests(unittest.TestCase):
    def test_prefers_full_name_over_first_name_only(self):
        contact = {
            "names": [{"name": "Алина", "firstName": "Алина", "lastName": "Чернова"}],
        }
        self.assertEqual(_contact_display_name(contact), "Алина Чернова")

    def test_single_name_when_no_last_name(self):
        contact = {"names": [{"name": "Кирилл", "firstName": "Кирилл"}]}
        self.assertEqual(_contact_display_name(contact), "Кирилл")

    def test_falls_back_to_contact_level_fields(self):
        contact = {"firstName": "Инна", "lastName": "Кладова"}
        self.assertEqual(_contact_display_name(contact), "Инна Кладова")


class TopicStateTests(unittest.TestCase):
    def test_state_roundtrip_and_find_by_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = BridgeState(path)
            state.save_topic(
                148440672,
                thread_id=77,
                title="Людмила",
                chat_type="dialog",
                sender="Людмила",
            )

            loaded = BridgeState(path)
            self.assertEqual(loaded.get_topic(148440672)["telegram_thread_id"], 77)
            self.assertEqual(loaded.find_by_thread(77)["max_chat_id"], 148440672)

    def test_topic_title_is_normalized_and_limited(self):
        self.assertEqual(normalize_topic_title("  Людмила   Иванова  ", "fallback"), "Людмила Иванова")
        self.assertLessEqual(len(normalize_topic_title("x" * 200, "fallback")), 120)


class ConfigTests(unittest.TestCase):
    def test_optional_topic_config_defaults_to_fallback_chat(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
            "telegram_forum_chat_id": "-100456",
        })

        self.assertEqual(config["telegram_chat_id"], 123)
        self.assertEqual(config["telegram_forum_chat_id"], -100456)
        self.assertEqual(config["telegram_fallback_chat_id"], 123)
        self.assertTrue(config["telegram_topics_enabled"])
        self.assertFalse(config["telegram_preload_topics"])
        self.assertFalse(config["telegram_seed_last_messages"])
        self.assertEqual(config["telegram_preload_chat_count"], 100)

    def test_env_overrides_apply_over_config_json(self):
        import os

        import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                '{"telegram_bot_token": "token", "telegram_chat_id": "123",'
                ' "max_login_token": "max"}',
                encoding="utf-8",
            )
            os.environ["MAX2TG_TELEGRAM_CONFIRM_SENT"] = "false"
            with patch.object(config_module, "CONFIG_PATH", path):
                try:
                    loaded = config_module.load_config()
                finally:
                    os.environ.pop("MAX2TG_TELEGRAM_CONFIRM_SENT", None)

        # Tokens come from config.json, but the env var still wins.
        self.assertEqual(loaded["telegram_bot_token"], "token")
        self.assertFalse(loaded["telegram_confirm_sent"])

    def test_env_tokens_do_not_discard_config_json_optional_settings(self):
        # HIGH-fix: all 3 token env vars set must NOT wipe optional config.json
        # settings (topics, forum id, confirm_sent).
        import os

        import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "telegram_bot_token": "fromfile",
                "telegram_chat_id": 111,
                "max_login_token": "fromfile",
                "telegram_topics_enabled": True,
                "telegram_forum_chat_id": -100123,
                "telegram_confirm_sent": False,
            }), encoding="utf-8")
            keys = ("MAX2TG_TELEGRAM_BOT_TOKEN", "MAX2TG_TELEGRAM_CHAT_ID",
                    "MAX2TG_MAX_TOKEN")
            saved = {k: os.environ.get(k) for k in keys}
            os.environ.update({
                "MAX2TG_TELEGRAM_BOT_TOKEN": "fromenv",
                "MAX2TG_TELEGRAM_CHAT_ID": "222",
                "MAX2TG_MAX_TOKEN": "fromenv",
            })
            try:
                with patch.object(config_module, "CONFIG_PATH", path):
                    loaded = config_module.load_config()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        self.assertEqual(loaded["telegram_bot_token"], "fromenv")  # env wins per-key
        self.assertEqual(loaded["telegram_forum_chat_id"], -100123)  # survives
        self.assertTrue(loaded["telegram_topics_enabled"])
        self.assertFalse(loaded["telegram_confirm_sent"])

    def test_confirm_sent_defaults_to_true(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
        })
        self.assertTrue(config["telegram_confirm_sent"])

    def test_corrupt_config_is_logged(self):
        import config as config_module
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            with patch.object(config_module, "CONFIG_PATH", path):
                with self.assertLogs("config", level="WARNING"):
                    self.assertEqual(config_module.load_partial(), {})


class BridgeTopicTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self):
        return MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        })

    async def test_creates_topic_for_new_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", return_value=42):
                chat_id, thread_id, in_topic = await bridge._telegram_target(
                    555, "Людмила", "dialog", "Людмила"
                )

            self.assertEqual(chat_id, -100222)
            self.assertEqual(thread_id, 42)
            self.assertTrue(in_topic)
            self.assertEqual(bridge._state.get_topic(555)["title"], "Людмила")

    async def test_concurrent_new_chat_creates_one_topic(self):
        # HIGH-fix: two concurrent packets from the same brand-new chat must
        # create exactly ONE Telegram topic, not duplicate it.
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            created = []

            async def slow_create(func, *args):
                # asyncio.to_thread passes the target callable first; yield so
                # the second coroutine reaches the lock while we're "creating"
                # (under the bug both would create a topic).
                await asyncio.sleep(0)
                created.append(args)
                return 100 + len(created)

            with patch("bridge.asyncio.to_thread", side_effect=slow_create):
                results = await asyncio.gather(
                    bridge._telegram_target(555, "X", "dialog", "X"),
                    bridge._telegram_target(555, "X", "dialog", "X"),
                )

            self.assertEqual(len(created), 1)  # exactly one topic created
            self.assertEqual(results[0][1], results[1][1])  # same thread id

    async def test_name_cache_is_bounded(self):
        bridge = self.make_bridge()
        with patch("bridge.NAME_CACHE_LIMIT", 3), \
                patch("bridge.resolve_users", new=AsyncMock(return_value={})):
            for i in range(6):
                await bridge._resolve_sender_name(object(), 1000 + i)
        self.assertLessEqual(len(bridge._name_cache), 3)

    async def test_on_packet_ignores_non_dict_frame(self):
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        # A valid-JSON but non-dict frame must be ignored cleanly, not raise
        # AttributeError out of the fire-and-forget handler task.
        await bridge._on_packet(client, [1, 2, 3])  # must not raise

    async def test_falls_back_when_topic_creation_fails(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", side_effect=RuntimeError("no rights")):
                chat_id, thread_id, in_topic = await bridge._telegram_target(
                    555, "Людмила", "dialog", "Людмила"
                )

            self.assertEqual(chat_id, 111)
            self.assertIsNone(thread_id)
            self.assertFalse(in_topic)

    async def test_text_inside_topic_routes_to_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(
                555,
                thread_id=42,
                title="Людмила",
                chat_type="dialog",
                sender="Людмила",
            )
            bridge._client = object()
            update = {
                "message": {
                    "chat": {"id": -100222},
                    "message_thread_id": 42,
                    "text": "Привет из Telegram",
                }
            }

            with patch("bridge.max_send", new=AsyncMock()) as max_send, \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._handle_update(update)

            max_send.assert_awaited_once_with(bridge._client, 555, "Привет из Telegram")

    async def test_media_inside_topic_uploads_file_to_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(
                555,
                thread_id=42,
                title="Family",
                chat_type="dialog",
                sender="Family",
            )
            bridge._client = object()
            update = {
                "message": {
                    "chat": {"id": -100222},
                    "message_thread_id": 42,
                    "document": {"file_id": "tg-file-1", "file_name": "report.pdf"},
                }
            }

            with patch("bridge.tg.download_file_by_id", return_value=(b"pdf", "docs/report.pdf")), \
                    patch("bridge.mediamax.send_uploaded_media", new=AsyncMock()) as send_media, \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._handle_update(update)

            send_media.assert_awaited_once_with(
                bridge._client,
                555,
                b"pdf",
                "report.pdf",
                "application/octet-stream",
                kind="file",
                text="",
                reply_to_message_id=None,
            )

    def test_telegram_sticker_attachment_metadata(self):
        bridge = self.make_bridge()

        static = bridge._telegram_attachment({
            "sticker": {"file_id": "s1", "file_unique_id": "u1"}
        })
        animated = bridge._telegram_attachment({
            "sticker": {"file_id": "s2", "file_unique_id": "u2", "is_animated": True}
        })
        video = bridge._telegram_attachment({
            "sticker": {"file_id": "s3", "file_unique_id": "u3", "is_video": True}
        })

        self.assertEqual(static["filename"], "telegram-sticker-u1.webp")
        self.assertEqual(static["mime_type"], "image/webp")
        self.assertEqual(animated["filename"], "telegram-sticker-u2.tgs")
        self.assertEqual(animated["mime_type"], "application/x-tgsticker")
        self.assertEqual(video["filename"], "telegram-sticker-u3.webm")
        self.assertEqual(video["mime_type"], "video/webm")

    async def test_preload_topics_from_login_creates_missing_topics(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        bridge._config["telegram_preload_chat_count"] = 10
        login_response = {
            "payload": {
                "chats": [
                    {"id": 100, "type": "CHAT", "title": "Family"},
                    {"id": 200, "cid": 200, "type": "DIALOG"},
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", side_effect=[41, 42]), \
                    patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")):
                await bridge._preload_topics_from_login(object(), login_response)

            self.assertEqual(bridge._state.get_topic(100)["telegram_thread_id"], 41)
            self.assertEqual(bridge._state.get_topic(100)["title"], "Family")
            self.assertEqual(bridge._state.get_topic(200)["telegram_thread_id"], 42)
            self.assertEqual(bridge._state.get_topic(200)["title"], "Alice")

    async def test_preload_topics_skips_existing_topic(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        login_response = {
            "payload": {"chats": [{"id": 100, "type": "CHAT", "title": "Family"}]}
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch("bridge.tg.create_forum_topic") as create_topic:
                await bridge._preload_topics_from_login(object(), login_response)

            create_topic.assert_not_called()

    async def test_seed_last_message_once(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        bridge._own_id = 999
        message = {"id": "m1", "sender": 999, "text": "Last text"}

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch("bridge.tg.send_message", return_value=555) as send_message:
                first = await bridge._seed_last_message(object(), 100, 41, message)
                second = await bridge._seed_last_message(object(), 100, 41, message)

            self.assertTrue(first)
            self.assertFalse(second)
            send_message.assert_called_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m1"
            )

    async def test_seed_last_message_with_media_without_text(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        message = {
            "id": "m2",
            "sender": 123,
            "text": "",
            "attaches": [{"_type": "STICKER", "url": "https://example.com/sticker.webp"}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")), \
                    patch.object(bridge, "_send_media_item", new=AsyncMock(return_value=True)) as send_media:
                seeded = await bridge._seed_last_message(object(), 100, 41, message)

            self.assertTrue(seeded)
            send_media.assert_awaited_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m2"
            )


class RedactionTests(unittest.TestCase):
    def test_bot_token_and_url_secret_are_scrubbed(self):
        import logging

        import main
        rec = logging.LogRecord(
            "x", logging.WARNING, "f.py", 1,
            "poll error url: /bot123456789:AAEsecretTokenValue1234567/getUpdates"
            "?sig=ABCDEFsecret123&x=1",
            None, None)
        main._RedactSecretsFilter().filter(rec)
        out = rec.getMessage()
        self.assertNotIn("AAEsecretTokenValue1234567", out)
        self.assertNotIn("ABCDEFsecret123", out)
        self.assertIn("bot<redacted>", out)


class MaxClientPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fail_pending_unblocks_awaiters(self):
        import asyncio

        import max_client
        client = max_client.BrowserMaxClient()
        fut = asyncio.get_event_loop().create_future()
        client._pending = {1: fut}
        client._fail_pending()
        self.assertTrue(fut.done())
        with self.assertRaises(ConnectionError):
            fut.result()
        self.assertEqual(client._pending, {})

    async def test_recv_loop_skips_bad_frames(self):
        import max_client

        class FakeConn:
            def __init__(self, frames):
                self._frames = frames

            def __aiter__(self):
                return self._agen()

            async def _agen(self):
                for frame in self._frames:
                    yield frame

        client = max_client.BrowserMaxClient()
        dispatched = []

        async def callback(_c, packet):
            dispatched.append(packet)

        client._incoming_event_callback = callback
        client._connection = FakeConn([
            "not json at all",                       # unparseable -> skipped
            "[1, 2, 3]",                             # valid JSON, non-dict -> skipped
            "42",                                    # valid JSON scalar -> skipped
            '{"opcode": 128, "payload": {"x": 1}}',  # valid event (no seq)
        ])
        await client._recv_loop()
        await asyncio.sleep(0.01)  # let the dispatched task run
        # Only the dict event is dispatched; non-dict frames never reach a
        # callback that assumes a dict (would otherwise AttributeError).
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["opcode"], 128)


if __name__ == "__main__":
    unittest.main()
