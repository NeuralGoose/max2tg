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


if __name__ == "__main__":
    unittest.main()
