"""Stage 3: typed PyMax inbound handlers on MaxToTelegramBridge."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bridge as bridge_module
from bridge import MaxToTelegramBridge
from message_content import message_content_fingerprint
from state import BridgeState
import tg


def _user(*, name=None, first=None, last=None):
    return SimpleNamespace(
        names=[SimpleNamespace(name=name, first_name=first, last_name=last)])


def _chat(title=None, type_="DIALOG"):
    return SimpleNamespace(title=title, type=type_)


def _message(*, id=1, chat_id=555, sender=7, text="", attaches=None,
             model_extra=None, stats=None):
    return SimpleNamespace(id=id, chat_id=chat_id, sender=sender, text=text,
                           attaches=attaches or [],
                           model_extra=model_extra or {},
                           stats=stats)


class TypedInboundTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, tmp, **overrides):
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        }
        config.update(overrides)
        bridge = MaxToTelegramBridge(config)
        bridge._state = BridgeState(Path(tmp) / "state.json")
        bridge._own_id = 999
        return bridge

    def _client(self, *, user=None, chat=None):
        client = Mock()
        client.get_user = AsyncMock(return_value=user or _user(first="Иван",
                                                               last="Петров"))
        client.get_chat = AsyncMock(return_value=chat or _chat(title="Семья",
                                                              type_="CHAT"))
        return client

    async def test_forwards_text_into_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(_message(text="привет"), client)

            send.assert_called_once()
            body = send.call_args.args[2]
            self.assertIn("Иван Петров:", body)
            self.assertIn("привет", body)
            self.assertEqual(send.call_args.kwargs["message_thread_id"], 42)
            self.assertIn(10, bridge._reply_map)

    async def test_forwards_own_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(_message(sender=999, text="мой echo"),
                                         client)
            send.assert_called_once()
            body = send.call_args.args[2]
            self.assertIn("Вы:", body)
            self.assertIn("мой echo", body)

    async def test_telegram_reply_not_reforwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            outbound = SimpleNamespace(id=77)
            bridge._mark_max_outbound(555, outbound)
            with patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(
                    _message(id=77, chat_id=555, sender=999, text="from TG"),
                    client,
                )
            send.assert_not_called()

    async def test_ignores_events_from_other_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            bridge._client = self._client()
            with patch("bridge.tg.send_message") as send:
                await bridge._on_message(_message(text="hi"), object())
            send.assert_not_called()

    async def test_sender_name_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(id=1, text="a"), client)
                await bridge._on_message(_message(id=2, text="b"), client)
            client.get_user.assert_awaited_once()

    async def test_edit_mirrors_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp, telegram_mirror_edit_marker=False)
            bridge._state.save_topic(555, thread_id=42, title="X",
                                     chat_type="dialog")
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
                "message_thread_id": 42,
            }]
            with patch(
                "bridge.tg.edit_message_text",
                return_value={"message_id": 10, "edit_date": 1710000000},
            ) as edit, patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message_edit(_message(text="новое"), client)
            edit.assert_called_once()
            self.assertEqual(
                edit.call_args.kwargs["message_thread_id"], 42,
            )
            self.assertIn(("555", "1"), bridge._mirror_edit_until)
            send.assert_not_called()

    async def test_edit_unmapped_forwards_without_edit_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._content_fingerprints[("555", "5")] = (
                message_content_fingerprint(_message(id=5, text="old"))
            )
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message_edit(_message(id=5, text="new"), client)
            body = send.call_args.args[2]
            self.assertIn("new", body)
            self.assertNotIn("(изменено)", body)

    async def test_photo_attachment_routed_to_media_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            photo = SimpleNamespace(type="PHOTO", base_url="https://cdn/p.jpg")
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch.object(bridge, "_send_media_item",
                                 new=AsyncMock(return_value=(True, 10, True))) as send_media, \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(text="", attaches=[photo]),
                                         client)
            send_media.assert_awaited_once()
            item = send_media.await_args.args[0]
            self.assertEqual(item.kind, "photo")
            self.assertEqual(item.url, "https://cdn/p.jpg")

    async def test_dialog_uses_sender_as_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client(chat=_chat(title=None, type_="DIALOG"))
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(text="привет"), client)
            self.assertEqual(bridge._state.get_topic(555)["title"], "Иван Петров")

    async def test_thread_not_found_drops_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            bridge._state.save_topic(555, thread_id=42, title="X",
                                     chat_type="dialog")
            client = self._client()
            bridge._client = client
            err = RuntimeError("Bad Request: message thread not found")
            with patch("bridge.tg.send_message", side_effect=err):
                await bridge._on_message(_message(text="привет"), client)
            self.assertIsNone(bridge._state.get_topic(555))

    async def test_mirror_delete_removes_tg_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            bridge._state.save_topic(555, thread_id=42, title="X",
                                     chat_type="dialog")
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
            }]
            event = SimpleNamespace(chat_id=555, message_ids=[1])
            with patch("bridge.tg.delete_message") as delete, \
                    patch("bridge.tg.send_message") as send:
                await bridge._on_message_delete(event, client)
            delete.assert_called_once_with(
                "token", -100222, 10,
            )
            send.assert_not_called()
            self.assertNotIn(("555", "1"), bridge._forward_map)

    async def test_forwarded_group_post_shows_attribution_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            original = _message(
                id=200, chat_id=100, sender=7, text="Новость дня", attaches=[],
            )
            wrapper = _message(
                id=300,
                chat_id=555,
                sender=None,
                text="",
                model_extra={
                    "link": {
                        "type": "FORWARD",
                        "chatId": 100,
                        "messageId": 200,
                    },
                },
            )
            client.get_message = AsyncMock(return_value=original)
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(wrapper, client)

            body = send.call_args.args[2]
            self.assertIn("↪", body)
            self.assertIn("Иван Петров", body)
            self.assertIn("Новость дня", body)

    async def test_embedded_forward_posts_text_and_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            wrapper = _message(
                id=300,
                chat_id=555,
                sender=180016258,
                text="",
                model_extra={
                    "link": {
                        "type": "FORWARD",
                        "chatId": -73194865803385,
                        "message": {
                            "sender": 7,
                            "id": "116826476060678268",
                            "text": "Только что нашли погибшего",
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
            client.get_message = AsyncMock()
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send_msg, \
                    patch.object(bridge, "_send_media_item",
                                 new=AsyncMock(return_value=(True, 10, True))) as send_media:
                await bridge._on_message(wrapper, client)

            client.get_message.assert_not_called()
            send_msg.assert_not_called()
            send_media.assert_awaited_once()
            caption = send_media.await_args.kwargs.get("caption_override")
            self.assertIsNotNone(caption)
            self.assertIn("↪", caption.text)
            self.assertIn("погибшего", caption.text)

    async def test_forward_fetch_failure_shows_fallback_not_bare_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            wrapper = _message(
                id=300,
                chat_id=555,
                sender=7,
                text="",
                model_extra={
                    "link": {
                        "type": "FORWARD",
                        "chatId": 100,
                        "messageId": 200,
                    },
                },
            )
            client.get_message = AsyncMock(return_value=None)
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(wrapper, client)

            body = send.call_args.args[2]
            self.assertIn("↪", body)
            self.assertIn("не удалось загрузить", body)
            self.assertNotEqual(body.strip(), "Иван Петров:")
            self.assertNotRegex(body, r"^Иван Петров:\s*$")

    async def test_empty_non_forward_skips_telegram_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(
                    _message(sender=None, text="", attaches=[]), client,
                )
            send.assert_not_called()

    async def test_duplicate_message_id_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._state.save_topic(555, thread_id=42, title="X", chat_type="chat")
            bridge._state.mark_delivered(555, 1)
            bridge._hydrate_delivered_cache()
            with patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(_message(id=1, text="again"), client)
            send.assert_not_called()

    async def test_stats_only_edit_does_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            msg = _message(id=5, text="same", stats={"comments": 1})
            bridge._content_fingerprints[("555", "5")] = (
                message_content_fingerprint(msg)
            )
            edited = _message(id=5, text="same", stats={"comments": 9})
            with patch("bridge.tg.send_message") as send:
                await bridge._on_message_edit(edited, client)
            send.assert_not_called()

    async def test_text_edit_still_sends_when_unmapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._state.mark_delivered(555, 5)
            bridge._hydrate_delivered_cache()
            bridge._content_fingerprints[("555", "5")] = (
                message_content_fingerprint(_message(id=5, text="old"))
            )
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message_edit(_message(id=5, text="new"), client)
            send.assert_called_once()
            self.assertNotIn("(изменено)", send.call_args.args[2])

    async def test_mirror_reaction_sets_tg_reaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
            }]
            counter = SimpleNamespace(count=2, reaction="👍")
            event = SimpleNamespace(
                chat_id=555, message_id="1", counters=[counter], total_count=2,
            )
            with patch("bridge.tg.set_message_reaction") as react:
                await bridge._on_reaction_update(event, client)
            react.assert_called_once_with("token", -100222, 10, "👍")

    async def test_mirror_reaction_skipped_after_recent_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
            }]
            loop = asyncio.get_running_loop()
            bridge._mirror_edit_until[("555", "1")] = loop.time() + 60
            counter = SimpleNamespace(count=2, reaction="👍")
            event = SimpleNamespace(
                chat_id=555, message_id="1", counters=[counter], total_count=2,
            )
            with patch("bridge.tg.set_message_reaction") as react:
                await bridge._on_reaction_update(event, client)
            react.assert_not_called()

    async def test_mirror_edit_marker_on_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            self.assertTrue(bridge._mirror_edit_marker)
            bridge._state.save_topic(555, thread_id=42, title="X",
                                     chat_type="dialog")
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
                "message_thread_id": 42,
            }]
            with patch(
                "bridge.tg.edit_message_text",
                return_value={"message_id": 10, "edit_date": 1},
            ) as edit, patch("bridge.tg.send_message"):
                await bridge._on_message_edit(_message(text="новое"), client)
            self.assertIn(" · ред.", edit.call_args.args[3])

    async def test_mirror_edit_marker_appends_suffix_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp, telegram_mirror_edit_marker=True)
            bridge._state.save_topic(555, thread_id=42, title="X",
                                     chat_type="dialog")
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "1")] = [{
                "telegram_chat_id": -100222,
                "message_id": 10,
                "role": "text",
                "message_thread_id": 42,
            }]
            with patch(
                "bridge.tg.edit_message_text",
                return_value={"message_id": 10, "edit_date": 1},
            ) as edit, patch("bridge.tg.send_message"):
                await bridge._on_message_edit(_message(text="новое"), client)
            body = edit.call_args.args[3]
            self.assertIn(" · ред.", body)

    async def test_text_and_photo_embeds_body_in_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            photo = SimpleNamespace(type="PHOTO", base_url="https://cdn/p.jpg")
            mock_photo = Mock(return_value=10)
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send_msg, \
                    patch.dict(bridge_module._MEDIA_SENDERS,
                               {"photo": (mock_photo, True)}):
                await bridge._on_message(
                    _message(text="Статья дня", attaches=[photo]), client,
                )
            send_msg.assert_not_called()
            mock_photo.assert_called_once()
            caption = mock_photo.call_args.args[3]
            self.assertIn("Статья дня", caption)
            self.assertIn("Иван Петров:", caption)

    async def test_text_and_two_photos_uses_media_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            photos = [
                SimpleNamespace(type="PHOTO", base_url="https://cdn/a.jpg"),
                SimpleNamespace(type="PHOTO", base_url="https://cdn/b.jpg"),
            ]
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send_msg, \
                    patch("bridge.tg.send_media_group", return_value=[10, 11]) as group:
                await bridge._on_message(
                    _message(text="Альбом", attaches=photos), client,
                )
            send_msg.assert_not_called()
            group.assert_called_once()
            caption = group.call_args.args[3]
            self.assertIn("Альбом", caption)
            self.assertIn("Иван Петров:", caption)

    async def test_long_text_with_photo_sends_overflow_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            photo = SimpleNamespace(type="PHOTO", base_url="https://cdn/p.jpg")
            prefix = "Иван Петров:\n"
            article = "A" * (tg.MAX_CAPTION_LEN - len(prefix) + 50)
            mock_photo = Mock(return_value=10)
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=11) as send_msg, \
                    patch.dict(bridge_module._MEDIA_SENDERS,
                               {"photo": (mock_photo, True)}):
                await bridge._on_message(
                    _message(text=article, attaches=[photo]), client,
                )
            mock_photo.assert_called_once()
            caption = mock_photo.call_args.args[3]
            self.assertLessEqual(len(caption), tg.MAX_CAPTION_LEN)
            send_msg.assert_called_once()
            overflow = send_msg.call_args.args[2]
            self.assertTrue(overflow)
            self.assertIn("A", overflow)

    async def test_reply_link_includes_quote_in_telegram_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            msg = _message(
                text="мой ответ",
                model_extra={
                    "link": {
                        "type": "REPLY",
                        "message": {"text": "оригинал"},
                    },
                },
            )
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(msg, client)

            body = send.call_args.args[2]
            self.assertIn("мой ответ", body)
            self.assertIn("оригинал", body)
            self.assertIn("ответ на", body.lower())

    async def test_reply_uses_native_reply_parameters_when_parent_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = self._client()
            bridge._client = client
            bridge._forward_map[("555", "100")] = [{
                "telegram_chat_id": -100222,
                "message_id": 50,
                "role": "text",
                "message_thread_id": 42,
            }]
            msg = _message(
                id=200,
                text="мой ответ",
                model_extra={
                    "link": {
                        "type": "REPLY",
                        "messageId": 100,
                        "message": {"text": "оригинал"},
                    },
                },
            )
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10) as send:
                await bridge._on_message(msg, client)

            reply_params = send.call_args.kwargs.get("reply_parameters")
            self.assertIsNotNone(reply_params)
            self.assertEqual(reply_params["message_id"], 50)
            self.assertEqual(reply_params["quote"], "оригинал")
            body = send.call_args.args[2]
            self.assertIn("мой ответ", body)
            self.assertNotIn("оригинал", body)

    async def test_mark_read_on_forward_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(
                tmp, max_mark_read_on_telegram_forward=True,
            )
            client = self._client()
            client.read_message = AsyncMock()
            bridge._client = client
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(id=99, text="привет"), client)

            client.read_message.assert_awaited_once_with(99, 555)

    async def test_mark_read_on_forward_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            self.assertFalse(bridge._mark_read_on_forward)
            client = self._client()
            client.read_message = AsyncMock()
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._on_message(_message(text="привет"), client)

            client.read_message.assert_not_called()

    async def test_mark_read_skipped_on_partial_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(
                tmp, max_mark_read_on_telegram_forward=True,
            )
            client = self._client()
            client.read_message = AsyncMock()
            with patch.object(
                bridge, "_forward", new_callable=AsyncMock,
                return_value=(True, False),
            ):
                await bridge._handle_incoming_message(
                    _message(id=99, text="частично"), client,
                )

            client.read_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
