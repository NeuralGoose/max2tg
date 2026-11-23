"""Topic renames, MAX bot buttons, channel comments and /delete."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pymax.types import ButtonType

import keyboards
from bridge import MaxToTelegramBridge
from comments import CommentStore, comment_text
from state import BridgeState
from tests.test_link_fixtures import seed_max_to_tg_link

OWNER = 111
FORUM = -100222


def _keyboard_attach():
    return SimpleNamespace(
        type="INLINE_KEYBOARD",
        callback_id="cb-1",
        keyboard=SimpleNamespace(buttons=[
            [SimpleNamespace(type="CALLBACK", text="Да", payload="yes"),
             SimpleNamespace(type="LINK", text="Сайт", url="https://example.org")],
            [SimpleNamespace(type="MESSAGE", text="Меню"),
             SimpleNamespace(type="REQUEST_CONTACT", text="Телефон"),
             SimpleNamespace(type="CLIPBOARD", text="Код", payload="1234")],
        ]),
    )


def _message(*, id=1, chat_id=555, sender=7, text="", attaches=None, post_id=None):
    return SimpleNamespace(id=id, chat_id=chat_id, sender=sender, text=text,
                           attaches=attaches or [], model_extra={}, stats=None,
                           post_id=post_id)


def _tg_message(text, *, reply_to=None, message_id=50, sender=OWNER, thread_id=42):
    message = {
        "message_id": message_id,
        "chat": {"id": FORUM},
        "from": {"id": sender},
        "text": text,
        "message_thread_id": thread_id,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return message


class DeferredFeatureTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, tmp, **overrides):
        state_path = Path(tmp) / "state.json"
        config = {
            "telegram_bot_token": "token",
            "telegram_chat_id": OWNER,
            "telegram_fallback_chat_id": OWNER,
            "telegram_forum_chat_id": FORUM,
            "telegram_topics_enabled": True,
            "telegram_confirm_sent": False,
            "max_login_token": "max",
            "links_db_path": str(state_path.with_name("links.db")),
        }
        config.update(overrides)
        bridge = MaxToTelegramBridge(config)
        bridge._state = BridgeState(state_path)
        bridge._own_id = 999
        client = Mock()
        client.get_user = AsyncMock(return_value=SimpleNamespace(
            names=[SimpleNamespace(name=None, first_name="Иван", last_name="Петров")]))
        client.get_chat = AsyncMock(return_value=SimpleNamespace(
            title="Бот", type="DIALOG"))
        bridge._client = client
        return bridge, client

    # --- live topic renames ---------------------------------------------------

    async def test_group_rename_renames_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            bridge._state.save_topic(555, thread_id=42, title="Старое",
                                     chat_type="chat")
            chat = SimpleNamespace(id=555, title="Новое", type="CHAT")
            with patch("bridge.tg.edit_forum_topic") as edit:
                await bridge._on_max_chat_update(chat, client)
                await bridge._on_max_chat_update(chat, client)
            edit.assert_called_once_with("token", FORUM, 42, "Новое")
            self.assertEqual(bridge._state.get_topic(555)["title"], "Новое")

    async def test_dialog_and_unknown_chats_not_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            bridge._state.save_topic(555, thread_id=42, title="Иван",
                                     chat_type="dialog")
            with patch("bridge.tg.edit_forum_topic") as edit:
                await bridge._on_max_chat_update(
                    SimpleNamespace(id=555, title="x", type="DIALOG"), client)
                await bridge._on_max_chat_update(
                    SimpleNamespace(id=777, title="Чужой", type="CHAT"), client)
            edit.assert_not_called()

    # --- MAX bot buttons --------------------------------------------------------

    def test_build_reply_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = keyboards.ButtonStore(Path(tmp) / "links.db")
            markup = keyboards.build_reply_markup(
                _message(attaches=[_keyboard_attach()]), store, 555, 1)
            rows = markup["inline_keyboard"]
            self.assertEqual([b["text"] for b in rows[0]], ["Да", "Сайт"])
            self.assertEqual(rows[0][1]["url"], "https://example.org")
            self.assertEqual([b["text"] for b in rows[1]], ["Меню", "Код"])
            self.assertEqual(rows[1][1]["copy_text"], {"text": "1234"})
            data = rows[0][0]["callback_data"]
            self.assertLessEqual(len(data.encode()), 64)
            saved = store.get(data[len(keyboards.CALLBACK_PREFIX):])
            self.assertEqual((saved["callback_id"], saved["payload"]), ("cb-1", "yes"))
            store.close()

    async def test_inbound_keyboard_attached_to_telegram_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            with patch("bridge.tg.create_forum_topic", return_value=42), \
                    patch("bridge.tg.send_message", return_value=10), \
                    patch("bridge.tg.edit_message_reply_markup") as markup:
                await bridge._on_message(
                    _message(text="Выберите", attaches=[_keyboard_attach()]), client)
            markup.assert_called_once()
            self.assertEqual(markup.call_args.args[2], 10)
            self.assertIn("inline_keyboard", markup.call_args.args[3])

    async def _press(self, bridge, attach_index, *, sender=OWNER):
        markup = keyboards.build_reply_markup(
            _message(attaches=[_keyboard_attach()]), bridge._buttons, 555, 1)
        button = markup["inline_keyboard"][attach_index[0]][attach_index[1]]
        with patch("bridge.tg.answer_callback_query") as answer:
            await bridge._handle_update({"callback_query": {
                "id": "q1", "data": button["callback_data"],
                "from": {"id": sender},
                "message": {"chat": {"id": FORUM}, "message_id": 10},
            }})
        return answer

    async def test_callback_press_relayed_to_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.send_callback = AsyncMock()
            answer = await self._press(bridge, (0, 0))
            client.send_callback.assert_awaited_once_with(
                "cb-1", ButtonType.CALLBACK, "yes")
            answer.assert_called_once_with("token", "q1", None)

    async def test_message_button_sends_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.send_message = AsyncMock()
            await self._press(bridge, (1, 0))
            client.send_message.assert_awaited_once_with(555, "Меню")

    async def test_callback_from_stranger_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.send_callback = AsyncMock()
            answer = await self._press(bridge, (0, 0), sender=12345)
            client.send_callback.assert_not_awaited()
            answer.assert_called_once_with("token", "q1", "Нет доступа")

    # --- /delete ---------------------------------------------------------------

    async def test_delete_command_deletes_in_max_and_telegram(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.delete_message = AsyncMock(return_value=True)
            seed_max_to_tg_link(bridge, tg_message_id=10)
            with patch("bridge.tg.delete_message") as delete, \
                    patch("bridge.tg.send_message") as send:
                await bridge._handle_update(
                    {"message": _tg_message("/delete", reply_to=10)})
            client.delete_message.assert_awaited_once_with(555, [1], for_me=False)
            self.assertEqual(
                [c.args[1:] for c in delete.call_args_list],
                [(FORUM, 10), (FORUM, 50)])
            send.assert_not_called()
            self.assertFalse(bridge._links.is_max_linked("555", "1"))

    async def test_delete_falls_back_to_for_me(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.delete_message = AsyncMock(
                side_effect=[RuntimeError("not allowed"), True])
            seed_max_to_tg_link(bridge, tg_message_id=10)
            with patch("bridge.tg.delete_message"), \
                    patch("bridge.tg.send_message") as send:
                await bridge._handle_update(
                    {"message": _tg_message("/delete", reply_to=10)})
            self.assertEqual(client.delete_message.await_args_list[1].kwargs,
                             {"for_me": True})
            self.assertIn("только у вас", send.call_args.args[2])

    async def test_delete_disabled_or_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp, sync_delete_enabled=False)
            client.delete_message = AsyncMock()
            seed_max_to_tg_link(bridge, tg_message_id=10)
            with patch("bridge.tg.send_message"), \
                    patch("bridge.tg.delete_message") as delete:
                await bridge._handle_update(
                    {"message": _tg_message("/delete", reply_to=10)})
                await bridge._on_message_delete(
                    SimpleNamespace(chat_id=555, message_ids=[1]), client)
            client.delete_message.assert_not_awaited()
            delete.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.delete_message = AsyncMock()
            with patch("bridge.tg.send_message") as send:
                await bridge._handle_update(
                    {"message": _tg_message("/delete", reply_to=99)})
            client.delete_message.assert_not_awaited()
            self.assertIn("Ответьте /delete", send.call_args.args[2])

    async def test_delete_from_non_owner_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            client.delete_message = AsyncMock()
            seed_max_to_tg_link(bridge, tg_message_id=10)
            await bridge._handle_update(
                {"message": _tg_message("/delete", reply_to=10, sender=5)})
            client.delete_message.assert_not_awaited()

    # --- channel comments ------------------------------------------------------

    def _channel(self, bridge):
        bridge._state.save_topic(555, thread_id=42, title="Канал",
                                 chat_type="channel")
        seed_max_to_tg_link(bridge, tg_message_id=10, message_thread_id=42)

    async def test_reply_to_channel_post_sends_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            self._channel(bridge)
            client.send_comment = AsyncMock(return_value=SimpleNamespace(id=77))
            client.send_message = AsyncMock()
            await bridge._handle_update({"message": _tg_message("круто", reply_to=10)})
            client.send_comment.assert_awaited_once_with(
                555, 1, "круто", reply_to=None, attachments=None)
            client.send_message.assert_not_awaited()
            store = bridge._comments.store
            self.assertEqual(store.comment_for_tg(50)["comment_id"], "77")
            self.assertEqual(store.active_watches(), [("555", "1", "77")])

    async def test_reply_to_mirrored_comment_answers_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            self._channel(bridge)
            bridge._comments.store.link(20, 555, 1, 70)
            client.send_comment = AsyncMock(return_value=SimpleNamespace(id=78))
            await bridge._handle_update({"message": _tg_message("согласен", reply_to=20)})
            client.send_comment.assert_awaited_once_with(
                555, 1, "согласен", reply_to=70, attachments=None)

    async def test_reply_in_regular_chat_is_not_a_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            bridge._state.save_topic(555, thread_id=42, title="Семья",
                                     chat_type="chat")
            seed_max_to_tg_link(bridge, tg_message_id=10)
            client.send_comment = AsyncMock()
            client.send_message = AsyncMock(return_value=SimpleNamespace(id=5))
            await bridge._handle_update({"message": _tg_message("ок", reply_to=10)})
            client.send_comment.assert_not_awaited()
            client.send_message.assert_awaited_once_with(555, "ок", reply_to=1)

    async def test_watched_thread_mirrors_new_comments_from_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            self._channel(bridge)
            bridge._comments.store.watch(555, 1, 3600, last_comment_id=5)
            client.fetch_comments = AsyncMock(return_value=[
                _message(id=7, sender=8, text="второй"),
                _message(id=4, sender=8, text="старый"),
                _message(id=6, sender=999, text="мой"),
            ])
            with patch("tg.send_message", return_value=30) as send:
                await bridge._comments.poll_once()
            send.assert_called_once()
            self.assertIn("второй", send.call_args.args[2])
            self.assertEqual(send.call_args.kwargs["reply_to_message_id"], 10)
            self.assertEqual(send.call_args.kwargs["message_thread_id"], 42)
            store = bridge._comments.store
            self.assertEqual(store.active_watches(), [("555", "1", "7")])
            self.assertEqual(store.comment_for_tg(30)["comment_id"], "7")

    async def test_comments_command_shows_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            self._channel(bridge)
            client.fetch_comments = AsyncMock(return_value=[
                _message(id=i, sender=8, text=f"c{i}") for i in range(1, 13)])
            with patch("tg.send_message", side_effect=range(100, 200)) as send:
                await bridge._handle_update(
                    {"message": _tg_message("/comments", reply_to=10)})
            self.assertEqual(send.call_count, 10)
            self.assertIn("c3", send.call_args_list[0].args[2])
            self.assertEqual(bridge._comments.store.active_watches(),
                             [("555", "1", "12")])

    async def test_inbound_comment_not_forwarded_as_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, client = self._bridge(tmp)
            with patch("bridge.tg.send_message") as send:
                await bridge._on_message(_message(text="коммент", post_id=1), client)
            send.assert_not_called()

    def test_comment_watch_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommentStore(Path(tmp) / "links.db")
            store.watch(555, 1, -1)
            store.watch(555, 2, 3600)
            self.assertEqual([w[1] for w in store.active_watches()], ["2"])
            store.close()

    def test_comment_text_includes_attachments(self):
        comment = _message(text="", attaches=[SimpleNamespace(type="CALL")])
        self.assertEqual(comment_text(comment, "Иван"), "💬 Иван:\n📞 Звонок")
        self.assertEqual(comment_text(_message(text="hi"), "Иван"), "💬 Иван:\nhi")


if __name__ == "__main__":
    unittest.main()
