"""Unit tests for message_sync.MessageSync (edits, reactions, classifier, watch)."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from message_links import MessageLinkRegistry
from message_sync import (
    EditResolveResult,
    MaxEventClassifier,
    MaxEventKind,
    MaxToTgEditDeps,
    MessageSync,
    SyncConfig,
    parse_reaction_frame_payload,
    reaction_from_counters,
)
from pymax.protocol import Opcode


def _message(*, id=1, chat_id=555, text="", stats=None):
    msg = SimpleNamespace(
        id=id, chat_id=chat_id, sender=7, text=text, attaches=[],
        model_extra={}, stats=stats,
    )
    return msg


class ClassifierTests(unittest.TestCase):
    def test_stats_only_drop(self):
        clf = MaxEventClassifier()
        msg = _message(id=5, text="same", stats={"comments": 1})
        clf.record_new(555, 5, msg)
        edited = _message(id=5, text="same", stats={"comments": 9})
        self.assertEqual(
            clf.classify_edit(555, 5, edited), MaxEventKind.STATS_ONLY,
        )

    def test_reaction_only_when_fingerprint_unchanged(self):
        clf = MaxEventClassifier()
        msg = _message(id=1, text="hi")
        clf.record_new(555, 1, msg)
        edited = _message(id=1, text="hi")
        edited.reaction_info = SimpleNamespace(total_count=1, counters=[])
        self.assertEqual(
            clf.classify_edit(555, 1, edited), MaxEventKind.REACTION_ONLY,
        )

    def test_content_edit_when_text_changes(self):
        clf = MaxEventClassifier()
        msg = _message(id=1, text="old")
        clf.record_new(555, 1, msg)
        self.assertEqual(
            clf.classify_edit(555, 1, _message(id=1, text="new")),
            MaxEventKind.CONTENT_EDIT,
        )


class MessageSyncTests(unittest.IsolatedAsyncioTestCase):
    def _sync(self, tmp, **config_overrides):
        links_path = Path(tmp) / "links.db"
        links = MessageLinkRegistry(links_path)
        cfg_kwargs = {
            "poll_delays": (0,),
            "coalesce_seconds": 0,
            "events_log": None,
        }
        cfg_kwargs.update(config_overrides)
        cfg = SyncConfig(**cfg_kwargs)
        client_holder: list = [None]

        async def resolve_for_edit(message, client):
            resolved = SimpleNamespace(
                text=getattr(message, "text", ""),
                elements=[],
                attaches=[],
                author="Иван",
                attribution=None,
                reply_parent_max_id=None,
                reply_quote=None,
            )
            return EditResolveResult(
                resolved=resolved,
                chat_title="Семья",
                chat_type="CHAT",
            )

        deps = MaxToTgEditDeps(
            token="token",
            links=links,
            mirror_edit_marker=cfg.mirror_edit_marker,
            split_caption=lambda b: (b, None),
            entry_thread_id=lambda e, fb: e.get("message_thread_id") or fb,
            topic_lock=lambda _cid: _NullAsyncContext(),
            telegram_target=AsyncMock(return_value=(-100222, 42, True)),
            resolve_for_edit=resolve_for_edit,
            is_locale_system_text=lambda t: False,
            reply_parameters_for_max=lambda *a, **k: None,
            is_channel_chat=lambda *a: False,
            media_senders=frozenset(),
            attaches_parse=lambda _m: [],
        )
        sync = MessageSync(
            links=links,
            token="token",
            config=cfg,
            edit_deps=deps,
            get_client=lambda: client_holder[0],
            bot_id=lambda: 999,
        )
        return sync, links, client_holder

    async def test_max_to_tg_edit_on_linked_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp, mirror_edit_marker=False)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                message_thread_id=42,
                role="text",
                origin="max_to_tg",
            )
            sync._classifier.record_new(555, 1, _message(text="old"))
            with patch(
                "message_sync.tg.edit_message_text",
                return_value={"message_id": 10, "edit_date": 1},
            ) as edit:
                await sync.on_max_message_edit(
                    _message(text="новое"), client,
                )
            edit.assert_called_once()
            self.assertIn(("555", "1"), sync._edit_guard_until)

    async def test_max_to_tg_reaction_via_push_155(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="max_to_tg",
            )
            frame = SimpleNamespace(
                opcode=int(Opcode.NOTIF_MSG_REACTIONS_CHANGED),
                cmd=0,
                payload={
                    "chatId": 555,
                    "messageId": "1",
                    "counters": [{"count": 2, "reaction": "👍"}],
                    "totalCount": 2,
                },
            )
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.on_max_raw(frame, client)
            react.assert_called_once_with(
                "token", -100222, 10, "👍", message_thread_id=None,
            )

    async def test_max_to_tg_reaction_from_message_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                message_thread_id=42,
                origin="max_to_tg",
            )
            msg = _message(text="привет")
            sync._classifier.record_new(555, 1, msg)
            edited = _message(text="привет")
            edited.reaction_info = SimpleNamespace(
                total_count=1,
                counters=[SimpleNamespace(count=1, reaction="❤️")],
            )
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.on_max_message_edit(edited, client)
            react.assert_called_once_with(
                "token", -100222, 10, "❤", message_thread_id=42,
            )

    async def test_empty_polls_keep_watch_alive(self):
        """Two polls with no reaction must not cancel the 60s/300s schedule."""
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp, poll_delays=())
            client = Mock()
            client.get_reactions = AsyncMock(return_value={
                "1": SimpleNamespace(counters=[], total_count=0),
            })
            holder[0] = client
            link = links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="tg_to_max",
            )
            sync.on_link_created(link)
            await sync._poll_chat_watch("555")
            await sync._poll_chat_watch("555")
            self.assertIn(("555", "1"), sync._watch)

    async def test_watch_poll_mirrors_tg_to_max_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp, poll_delays=())
            client = Mock()
            client.get_reactions = AsyncMock(return_value={
                "77": SimpleNamespace(
                    counters=[SimpleNamespace(count=1, reaction="🔥")],
                    total_count=1,
                ),
            })
            holder[0] = client
            link = links.link(
                "555", "77",
                telegram_chat_id=-100222,
                telegram_message_id=99,
                origin="tg_to_max",
            )
            sync.on_link_created(link)
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync._poll_chat_watch("555")
            react.assert_called_once_with(
                "token", -100222, 99, "🔥", message_thread_id=None,
            )

    async def test_duplicate_reaction_skips_telegram_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="max_to_tg",
            )
            sync._last_applied[("555", "1")] = "👍"
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.apply_max_to_tg(
                    555, "1", "👍", client, source="test",
                )
            react.assert_not_called()

    async def test_edit_guard_blocks_reaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="max_to_tg",
            )
            loop = asyncio.get_running_loop()
            sync._edit_guard_until[("555", "1")] = loop.time() + 60
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.apply_max_to_tg(
                    555, "1", "👍", client, source="test",
                )
            react.assert_not_called()

    async def test_max_to_tg_reaction_nested_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "77",
                telegram_chat_id=-100222,
                telegram_message_id=99,
                origin="tg_to_max",
            )
            frame = SimpleNamespace(
                opcode=int(Opcode.NOTIF_MSG_REACTIONS_CHANGED),
                cmd=0,
                payload={
                    "chatId": 555,
                    "messageId": "77",
                    "reactionInfo": {
                        "counters": [{"count": 1, "reaction": "❤️"}],
                        "totalCount": 1,
                    },
                },
            )
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.on_max_raw(frame, client)
            react.assert_called_once_with(
                "token", -100222, 99, "❤", message_thread_id=None,
            )

    async def test_apply_maps_unsupported_max_emoji(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="max_to_tg",
            )
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync.apply_max_to_tg(
                    555, "1", "🤟", client, source="test",
                )
            react.assert_called_once_with(
                "token", -100222, 10, "❤", message_thread_id=None,
            )

    async def test_tg_to_max_reaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            client.add_reaction = AsyncMock()
            holder[0] = client
            links.link(
                "555", "42",
                telegram_chat_id=111,
                telegram_message_id=100,
                origin="tg_to_max",
            )
            await sync.on_tg_reaction({
                "message_id": 100,
                "user": {"id": 1},
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            })
            client.add_reaction.assert_awaited_once_with(555, "42", "👍")

    async def test_bot_tg_reaction_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            client.add_reaction = AsyncMock()
            holder[0] = client
            links.link(
                "555", "42",
                telegram_chat_id=111,
                telegram_message_id=100,
                origin="tg_to_max",
            )
            await sync.on_tg_reaction({
                "message_id": 100,
                "user": {"id": 999},
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            })
            client.add_reaction.assert_not_awaited()

    async def test_reaction_clear_on_empty_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, links, holder = self._sync(tmp)
            client = Mock()
            client.get_reactions = AsyncMock(return_value={
                "1": SimpleNamespace(counters=[], total_count=0),
            })
            holder[0] = client
            links.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                origin="max_to_tg",
            )
            sync._watch[("555", "1")] = SimpleNamespace(
                max_chat_id="555",
                max_message_id="1",
                created_at=time.time(),
                stable_count=0,
                last_fetched=None,
                pending_tasks=[],
            )
            sync._last_applied[("555", "1")] = "👍"
            with patch("message_sync.tg.set_message_reaction") as react:
                await sync._poll_chat_watch("555")
            react.assert_called_once_with(
                "token", -100222, 10, None, message_thread_id=None,
            )

    def test_parse_reaction_frame_nested(self):
        chat_id, msg_id, counters, total, source = parse_reaction_frame_payload({
            "chatId": 1,
            "messageId": "2",
            "reactionInfo": {
                "counters": [{"count": 1, "reaction": "❤️"}],
                "totalCount": 1,
            },
        })
        self.assertEqual(source, "nested")
        self.assertEqual(reaction_from_counters(counters, total), "❤️")


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


if __name__ == "__main__":
    unittest.main()
