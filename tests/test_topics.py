import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from types import SimpleNamespace

from bridge import MaxToTelegramBridge
from config import normalize_config
from state import BridgeState, normalize_topic_title


def _user(*names):
    """Duck-typed PyMax User with a names list of (first, last) tuples."""
    name_objs = [SimpleNamespace(first_name=f, last_name=last, name=None)
                 for f, last in names]
    return SimpleNamespace(names=name_objs)


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


class DisplayNameTests(unittest.TestCase):
    def test_prefers_full_name_over_first_name_only(self):
        user = _user(("Алина", "Чернова"))
        self.assertEqual(MaxToTelegramBridge._display_name(user), "Алина Чернова")

    def test_single_name_when_no_last_name(self):
        user = _user(("Кирилл", None))
        self.assertEqual(MaxToTelegramBridge._display_name(user), "Кирилл")

    def test_returns_none_without_names(self):
        self.assertIsNone(MaxToTelegramBridge._display_name(_user()))


class TopicBodyTests(unittest.TestCase):
    def test_group_keeps_sender_prefix(self):
        self.assertEqual(
            MaxToTelegramBridge._topic_body("Иван", "привет", []), "Иван:\nпривет")

    def test_channel_drops_redundant_sender_prefix(self):
        # A channel post (sender == "MAX") must NOT get the "MAX:" prefix that
        # just duplicates the channel name shown above the message.
        self.assertEqual(
            MaxToTelegramBridge._topic_body("MAX", "Афиша на выходные", [], is_channel=True),
            "Афиша на выходные")

    def test_channel_media_caption_has_no_prefix(self):
        self.assertEqual(
            MaxToTelegramBridge._topic_caption("MAX", "Фото", is_channel=True), "Фото")
        self.assertEqual(
            MaxToTelegramBridge._topic_caption("Иван", "Фото", is_channel=False),
            "Иван:\nФото")

    def test_delivery_body_channel_omits_sender_prefix(self):
        body = MaxToTelegramBridge._delivery_body(
            "Коммерсантъ", "Новость", [], is_channel=True,
            attribution="↪ Источник", in_topic=True, header="",
        )
        self.assertNotIn("Коммерсантъ:", body)
        self.assertIn("Новость", body)
        self.assertIn("↪", body)


class SmartActionTests(unittest.TestCase):
    def test_max_link_becomes_join(self):
        self.assertEqual(
            MaxToTelegramBridge._smart_action("https://max.ru/join/AbC-d_e"),
            "/join https://max.ru/join/AbC-d_e")

    def test_link_extracted_from_surrounding_text(self):
        # A link pasted inside a sentence is still actioned (trailing comma trimmed).
        self.assertEqual(
            MaxToTelegramBridge._smart_action("вступи: max.ru/join/XyZ, спасибо"),
            "/join max.ru/join/XyZ")

    def test_username_becomes_find(self):
        self.assertEqual(
            MaxToTelegramBridge._smart_action("@cool_channel"), "/find @cool_channel")

    def test_phone_becomes_find(self):
        self.assertEqual(
            MaxToTelegramBridge._smart_action("+7 999 123-45-67"),
            "/find +7 999 123-45-67")

    def test_plain_text_is_ignored(self):
        self.assertIsNone(MaxToTelegramBridge._smart_action("привет, как дела?"))

    def test_empty_is_ignored(self):
        self.assertIsNone(MaxToTelegramBridge._smart_action("   "))


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

    def test_delete_topic_forgets_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = BridgeState(Path(tmp) / "state.json")
            state.save_topic(555, thread_id=7, title="X", chat_type="dialog")
            self.assertTrue(state.delete_topic(555))
            self.assertIsNone(state.get_topic(555))
            self.assertFalse(state.delete_topic(555))  # already gone


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
        self.assertEqual(config["telegram_preload_chat_source"], "login")
        self.assertEqual(config["telegram_preload_message_depth"], 1)
        self.assertEqual(config["telegram_preload_fetch_pages"], 20)

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

    async def test_create_topic_sets_icon_params(self):
        bridge = self.make_bridge()
        bridge._forum_icon_sticker_ids_cache = ["emoji_a", "emoji_b"]
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", return_value=42) as create:
                await bridge._telegram_target(555, "Test", "dialog", "Test")
            create.assert_called_once()
            kwargs = create.call_args.kwargs
            self.assertIn("icon_color", kwargs)
            self.assertEqual(kwargs["icon_custom_emoji_id"], "emoji_b")
            self.assertTrue(bridge._state.get_topic(555).get("topic_icon_set"))

    async def test_refresh_topic_icon_backfills_once(self):
        bridge = self.make_bridge()
        bridge._forum_icon_sticker_ids_cache = ["emoji_a"]
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="T", chat_type="chat")
            with patch("bridge.tg.edit_forum_topic") as edit:
                first = await bridge._refresh_topic_icon(100, 41)
                second = await bridge._refresh_topic_icon(100, 41)
            self.assertTrue(first)
            self.assertFalse(second)
            edit.assert_called_once()
            self.assertTrue(bridge._state.get_topic(100).get("topic_icon_set"))

    async def test_concurrent_new_chat_creates_one_topic(self):
        # HIGH-fix: two concurrent packets from the same brand-new chat must
        # create exactly ONE Telegram topic, not duplicate it.
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            created = []

            async def slow_create(func, *args, **kwargs):
                # asyncio.to_thread passes the target callable first; yield so
                # the second coroutine reaches the lock while we're "creating"
                # (under the bug both would create a topic). Only count the
                # createForumTopic call (the path also offloads the icon-sticker
                # fetch to a thread, which is not a topic creation).
                await asyncio.sleep(0)
                if getattr(func, "__name__", "") == "create_forum_topic":
                    created.append((args, kwargs))
                    return 100 + len(created)
                return []

            with patch("bridge.asyncio.to_thread", side_effect=slow_create):
                results = await asyncio.gather(
                    bridge._telegram_target(555, "X", "dialog", "X"),
                    bridge._telegram_target(555, "X", "dialog", "X"),
                )

            self.assertEqual(len(created), 1)  # exactly one topic created
            self.assertEqual(results[0][1], results[1][1])  # same thread id

    async def test_name_cache_is_bounded(self):
        bridge = self.make_bridge()
        client = Mock()
        client.get_user = AsyncMock(return_value=None)
        with patch("bridge.NAME_CACHE_LIMIT", 3):
            for i in range(6):
                await bridge._resolve_sender_name(client, 1000 + i)
        self.assertLessEqual(len(bridge._name_cache), 3)

    async def test_find_command_does_not_remember_dm_target(self):
        # /find must NOT create a reply_map send-target from a user-supplied id
        # (a MAX user_id is not a dialog chatId).
        import maxactions
        bridge = self.make_bridge()
        bridge._client = object()
        result = maxactions.CommandResult("🔍 Нашёл: Пётр\n🆔 id: 777")
        with patch("bridge.tg.send_message", return_value=42), \
                patch("bridge.maxactions.find", new=AsyncMock(return_value=result)):
            await bridge._handle_command(111, None, "/find 777")
        self.assertNotIn(42, bridge._reply_map)

    async def test_help_command_replies(self):
        bridge = self.make_bridge()
        sent = []
        with patch("bridge.tg.send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            await bridge._handle_command(111, None, "/help")
        self.assertTrue(sent and "/join" in sent[0])

    async def test_bare_link_in_chat_triggers_join_without_command(self):
        # The simplification: a pasted link acts like /join — no command typed.
        import maxactions
        bridge = self.make_bridge()
        bridge._client = object()
        result = maxactions.CommandResult("✅ Вступил: Канал")
        update = {"message": {"chat": {"id": 111}, "text": "https://max.ru/join/AbCdEf"}}
        with patch("bridge.maxactions.join", new=AsyncMock(return_value=result)) as join, \
                patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        join.assert_awaited_once()
        self.assertEqual(join.await_args.args[1], "https://max.ru/join/AbCdEf")

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

    async def test_fallback_forward_includes_header_and_reply_map(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._own_id = 999
            client = Mock()
            client.get_user = AsyncMock(return_value=_user(("Иван", "Петров")))
            client.get_chat = AsyncMock(return_value=SimpleNamespace(
                title="Семья", type="CHAT",
            ))
            bridge._client = client
            message = SimpleNamespace(
                id=1, chat_id=555, sender=7, text="привет",
                attaches=[], model_extra={}, stats=None,
            )
            with patch("bridge.tg.create_forum_topic",
                       side_effect=RuntimeError("no rights")), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(message, client)

            send.assert_called_once()
            self.assertEqual(send.call_args.args[1], 111)
            body = send.call_args.args[2]
            self.assertIn("MAX |", body)
            self.assertIn("(чат 555)", body)
            self.assertIn("привет", body)
            self.assertIsNone(send.call_args.kwargs.get("message_thread_id"))
            self.assertIn(10, bridge._reply_map)
            self.assertEqual(bridge._reply_map[10]["chat_id"], 555)

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
            bridge._client = Mock()
            bridge._client.send_message = AsyncMock()
            update = {
                "message": {
                    "chat": {"id": -100222},
                    "message_thread_id": 42,
                    "text": "Привет из Telegram",
                }
            }

            with patch("bridge.tg.send_message", return_value=10):
                await bridge._handle_update(update)

            bridge._client.send_message.assert_awaited_once_with(
                555, "Привет из Telegram", reply_to=None)

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

    async def test_preload_topics_creates_missing_topics(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        bridge._config["telegram_preload_chat_count"] = 10
        bridge._config["telegram_preload_chat_delay_seconds"] = 0
        bridge._own_id = 999
        # PyMax client.chats: typed Chat objects (duck-typed here). The dialog
        # resolves its title from the peer participant's display name.
        client = Mock()
        client.chats = [
            SimpleNamespace(id=100, type="CHAT", title="Family",
                            participants={}, last_message=None, cid=None),
            SimpleNamespace(id=200, type="DIALOG", title=None,
                            participants={999: 1, 777: 1}, last_message=None,
                            cid=200),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", side_effect=[41, 42]), \
                    patch.object(bridge, "_resolve_sender_name",
                                 new=AsyncMock(return_value="Alice")):
                await bridge._preload_topics(client)

            self.assertEqual(bridge._state.get_topic(100)["telegram_thread_id"], 41)
            self.assertEqual(bridge._state.get_topic(100)["title"], "Family")
            self.assertEqual(bridge._state.get_topic(200)["telegram_thread_id"], 42)
            self.assertEqual(bridge._state.get_topic(200)["title"], "Alice")

    async def test_preload_topics_skips_existing_topic(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        bridge._config["telegram_preload_chat_delay_seconds"] = 0
        client = Mock()
        client.chats = [SimpleNamespace(id=100, type="CHAT", title="Family",
                                        participants={}, last_message=None,
                                        cid=None)]

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch("bridge.tg.create_forum_topic") as create_topic:
                await bridge._preload_topics(client)

            create_topic.assert_not_called()

    async def test_seed_last_message_once(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        bridge._own_id = 999
        message = SimpleNamespace(id="m1", sender=999, text="Last text", attaches=[])

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch("bridge.tg.send_message", return_value=555) as send_message:
                first = await bridge._seed_last_message(Mock(), 100, 41, message)
                second = await bridge._seed_last_message(Mock(), 100, 41, message)

            self.assertTrue(first)
            self.assertFalse(second)
            send_message.assert_called_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m1"
            )

    async def test_seed_last_message_with_media_without_text(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        sticker = SimpleNamespace(type="STICKER",
                                  url="https://example.com/sticker.webp",
                                  lottie_url=None)
        message = SimpleNamespace(id="m2", sender=123, text="", attaches=[sticker])

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")), \
                    patch.object(bridge, "_send_media_item", new=AsyncMock(return_value=(True, 10, True))) as send_media:
                seeded = await bridge._seed_last_message(Mock(), 100, 41, message)

            self.assertTrue(seeded)
            send_media.assert_awaited_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m2"
            )


class DeliveryStateTests(unittest.TestCase):
    def test_mark_delivered_and_is_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = BridgeState(Path(tmp) / "state.json")
            state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            self.assertFalse(state.is_delivered(100, "m1"))
            state.mark_delivered(100, "m1")
            self.assertTrue(state.is_delivered(100, "m1"))
            topic = state.get_topic(100)
            self.assertIn("m1", topic["delivered_max_message_ids"])
            self.assertEqual(topic["last_delivered_max_message_id"], "m1")

    def test_delivered_ids_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = BridgeState(Path(tmp) / "state.json")
            state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            from state import DELIVERED_IDS_LIMIT
            for i in range(DELIVERED_IDS_LIMIT + 10):
                state.mark_delivered(100, f"id-{i}")
            ids = state.get_topic(100)["delivered_max_message_ids"]
            self.assertEqual(len(ids), DELIVERED_IDS_LIMIT)
            self.assertEqual(ids[0], "id-10")
            self.assertEqual(ids[-1], f"id-{DELIVERED_IDS_LIMIT + 9}")


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


if __name__ == "__main__":
    unittest.main()
