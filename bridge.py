"""MAX <-> Telegram bridge.

MAX -> Telegram: forwards incoming messages (text + attachments) to a Telegram
chat. Telegram -> MAX: when the user *replies* (Reply/свайп) to a forwarded
message in Telegram, the reply text is sent back to the originating MAX chat.
"""
import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path

from vkmax.client import MaxClient
from vkmax.functions.messages import reply_message as max_reply
from vkmax.functions.messages import send_message as max_send
from vkmax.functions.users import resolve_users

import attaches
import mediamax
import tg
from max_client import BrowserMaxClient, MaxAuthError
from state import BridgeState, normalize_topic_title

_logger = logging.getLogger(__name__)

INCOMING_MESSAGE_OPCODE = 128
RECONNECT_DELAY_SECONDS = 15
RECONNECT_MAX_DELAY = 300
REPLY_MAP_LIMIT = 10000
NAME_CACHE_LIMIT = 5000
# Cap concurrent per-packet handlers so a media burst can't exhaust the asyncio
# to_thread pool and starve the Telegram long-poll.
MEDIA_CONCURRENCY = 8
# Telegram bots can upload at most 50 MB; leave headroom.
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024
ATTACH_DEBUG_LOG = Path(__file__).parent / "attaches.log"
ATTACH_DEBUG_LOG_MAX_BYTES = 5 * 1024 * 1024

# kind -> (tg function, supports_caption)
_MEDIA_SENDERS = {
    "photo": (tg.send_photo, True),
    "animation": (tg.send_animation, True),
    "video": (tg.send_video, True),
    "voice": (tg.send_voice, True),
    "audio": (tg.send_audio, True),
    "document": (tg.send_document, True),
    "sticker": (tg.send_sticker, False),
}


def _extract_own_id(login_response: dict) -> int | None:
    profile = login_response.get("payload", {}).get("profile", {})
    for candidate in (profile.get("contact", {}).get("id"), profile.get("id")):
        if isinstance(candidate, int):
            return candidate
    return None


def _contact_display_name(contact: dict) -> str | None:
    """Pick the fullest available name (first+last) to match MAX's display.

    MAX returns several name candidates; `names[0].name` is often just the first
    name, so we collect all candidates and choose the fullest (most words).
    """
    candidates: list[str] = []
    names = contact.get("names")
    if isinstance(names, list):
        for entry in names:
            if not isinstance(entry, dict):
                continue
            full = f"{entry.get('firstName', '')} {entry.get('lastName', '')}".strip()
            if full:
                candidates.append(full)
            if entry.get("name"):
                candidates.append(str(entry["name"]).strip())
    full = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
    if full:
        candidates.append(full)
    if contact.get("name"):
        candidates.append(str(contact["name"]).strip())
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    # Prefer the fullest: most words first, then longest string.
    return max(candidates, key=lambda s: (len(s.split()), len(s)))


def _log_raw_attaches(message: dict) -> None:
    """Append raw attaches to a log so unsupported types can be refined later.

    Capped so a flood of attachment messages can't fill the disk; attach
    payloads may contain signed CDN URLs, so we keep the file small.
    """
    try:
        if (ATTACH_DEBUG_LOG.exists()
                and ATTACH_DEBUG_LOG.stat().st_size > ATTACH_DEBUG_LOG_MAX_BYTES):
            return
        with ATTACH_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.get("attaches"), ensure_ascii=False) + "\n")
    except OSError:
        pass


class MaxToTelegramBridge:
    def __init__(self, config: dict):
        self._config = config
        self._token = config["telegram_bot_token"]
        self._chat_id = config["telegram_chat_id"]
        self._fallback_chat_id = config.get("telegram_fallback_chat_id", self._chat_id)
        self._forum_chat_id = config.get("telegram_forum_chat_id")
        self._topics_enabled = bool(
            config.get("telegram_topics_enabled") and self._forum_chat_id
        )
        # One-shot: re-resolve and rename all topic titles from MAX, even ones
        # that already have a "good-looking" name (corrects drifted/short names).
        self._resync_titles = bool(config.get("telegram_resync_titles"))
        # Send a "✅ Отправлено в MAX" confirmation after each Telegram->MAX
        # message. Set false to keep topics clean (errors are still shown).
        self._confirm_sent = config.get("telegram_confirm_sent", True)
        self._own_id: int | None = None
        # Bounded LRU so a long-running process can't grow the cache forever.
        self._name_cache: "OrderedDict[int, str]" = OrderedDict()
        self._client: MaxClient | None = None
        self._state = BridgeState()
        # telegram message_id -> {"chat_id", "message_id", "sender"}
        self._reply_map: "OrderedDict[int, dict]" = OrderedDict()
        # Per-MAX-chat locks serialize topic creation so two concurrent packets
        # from a brand-new chat cannot create duplicate Telegram topics.
        self._topic_locks: "dict[str, asyncio.Lock]" = {}
        # Strong refs to in-flight per-packet handler tasks: vkmax spawns them
        # fire-and-forget, and without a reference they can be GC'd mid-run.
        self._handler_tasks: set = set()
        # Lazily created (needs a running loop): bounds concurrent forwards.
        self._forward_sem: "asyncio.Semaphore | None" = None

    # --- helpers -------------------------------------------------------------

    def _remember(self, tg_message_id: int | None, max_chat_id, max_message_id,
                  sender: str, telegram_chat_id=None,
                  message_thread_id: int | None = None) -> None:
        if not tg_message_id:
            return
        self._reply_map[tg_message_id] = {
            "chat_id": max_chat_id,
            "message_id": max_message_id,
            "sender": sender,
            "telegram_chat_id": telegram_chat_id or self._fallback_chat_id,
            "message_thread_id": message_thread_id,
        }
        while len(self._reply_map) > REPLY_MAP_LIMIT:
            self._reply_map.popitem(last=False)

    async def _resolve_sender_name(self, client: MaxClient, sender_id: int) -> str:
        if sender_id in self._name_cache:
            self._name_cache.move_to_end(sender_id)  # keep hot senders
            return self._name_cache[sender_id]
        name = str(sender_id)
        try:
            response = await resolve_users(client, [sender_id])
            for contact in response.get("payload", {}).get("contacts", []):
                display = _contact_display_name(contact)
                if display:
                    name = display
                    break
        except Exception as exc:
            _logger.warning("Could not resolve user %s: %s", sender_id, exc)
        self._name_cache[sender_id] = name
        while len(self._name_cache) > NAME_CACHE_LIMIT:
            self._name_cache.popitem(last=False)
        return name

    def _extract_chat_meta(self, payload: dict, sender: str) -> tuple[str, str]:
        chat_id = payload.get("chatId")
        message = payload.get("message", {})
        chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else {}
        candidates = [
            chat.get("title"),
            chat.get("name"),
            chat.get("theme"),
            payload.get("title"),
            payload.get("chatTitle"),
            message.get("chatTitle") if isinstance(message, dict) else None,
        ]
        title = next((str(value).strip() for value in candidates if value), "")
        chat_type = str(
            chat.get("type") or chat.get("chatType") or payload.get("chatType") or ""
        ).lower()
        if not title:
            title = sender if sender and sender != "неизвестный отправитель" else f"MAX чат {chat_id}"
        if not chat_type:
            chat_type = "dialog"
        return title, chat_type

    @staticmethod
    def _sync_chat_id(chat: dict):
        for key in ("id", "chatId", "chat_id", "cid"):
            value = chat.get(key)
            if value not in (None, 0, "0"):
                return value
        return None

    def _dialog_peer_id(self, chat: dict):
        participants = chat.get("participants")
        if not isinstance(participants, dict):
            return chat.get("cid")
        for raw_id in participants:
            try:
                participant_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if participant_id != self._own_id:
                return participant_id
        return chat.get("cid")

    async def _sync_chat_meta(self, client: MaxClient, chat: dict) -> tuple[str, str, str]:
        chat_id = self._sync_chat_id(chat)
        chat_type = str(chat.get("type") or chat.get("chatType") or "dialog").lower()
        title = next(
            (
                str(value).strip()
                for value in (
                    chat.get("title"),
                    chat.get("name"),
                    chat.get("displayName"),
                )
                if value
            ),
            "",
        )

        if chat_type == "dialog":
            # MAX shows the peer's full contact name for dialogs, which is more
            # reliable than the chat's own (often missing/partial) title field.
            contact_id = self._dialog_peer_id(chat) or chat.get("cid") or chat_id
            if isinstance(contact_id, int):
                resolved = await self._resolve_sender_name(client, contact_id)
                if resolved and not str(resolved).isdigit():
                    title = resolved

        fallback = f"MAX chat {chat_id}"
        title = normalize_topic_title(title, fallback)
        sender = title if title != fallback else None
        return title, chat_type, sender or fallback

    @staticmethod
    def _is_numeric_title(value: str | None) -> bool:
        return bool(value) and str(value).strip().lstrip("-").isdigit()

    async def _refresh_topic_title(self, max_chat_id, thread_id: int,
                                   title: str, chat_type: str,
                                   sender: str) -> bool:
        existing = self._state.get_topic(max_chat_id) or {}
        current = str(existing.get("title") or "").strip()
        if not title or title == current or self._is_numeric_title(title):
            return False
        # Normally we don't overwrite an already-good name (respects manual
        # edits). In resync mode we apply the freshly-resolved name regardless.
        if not self._resync_titles:
            if current and not self._is_numeric_title(current) and not current.startswith("MAX chat "):
                return False
        try:
            await asyncio.to_thread(
                tg.edit_forum_topic,
                self._token,
                self._forum_chat_id,
                thread_id,
                title,
            )
        except Exception as exc:
            _logger.warning("Could not rename Telegram topic for MAX chat %s: %s",
                            max_chat_id, exc)
            return False
        self._state.save_topic(
            max_chat_id,
            thread_id=thread_id,
            title=title,
            chat_type=chat_type,
            sender=sender,
        )
        _logger.info("Renamed Telegram topic %s for MAX chat %s to %s",
                     thread_id, max_chat_id, title)
        return True

    async def _preload_topics_from_login(self, client: MaxClient, login_response: dict) -> None:
        if not self._topics_enabled or not self._config.get("telegram_preload_topics"):
            return

        chats = login_response.get("payload", {}).get("chats", [])
        if not isinstance(chats, list):
            return

        limit = int(self._config.get("telegram_preload_chat_count") or 100)
        created = existing = failed = skipped = seeded = 0
        for chat in chats[:limit]:
            if not isinstance(chat, dict):
                skipped += 1
                continue
            chat_id = self._sync_chat_id(chat)
            if chat_id is None:
                skipped += 1
                continue
            existing_topic = self._state.get_topic(chat_id)
            if existing_topic:
                existing += 1
                thread_id = existing_topic.get("telegram_thread_id")
                if thread_id:
                    title, chat_type, sender = await self._sync_chat_meta(client, chat)
                    await self._refresh_topic_title(
                        chat_id, thread_id, title, chat_type, sender
                    )
                    if await self._seed_last_message(
                        client, chat_id, thread_id, chat.get("lastMessage")
                    ):
                        seeded += 1
                continue

            title, chat_type, sender = await self._sync_chat_meta(client, chat)
            _target_chat_id, thread_id, in_topic = await self._telegram_target(
                chat_id, title, chat_type, sender
            )
            if in_topic and thread_id:
                created += 1
                if await self._seed_last_message(
                    client, chat_id, thread_id, chat.get("lastMessage")
                ):
                    seeded += 1
            else:
                failed += 1
            await asyncio.sleep(0.35)

        _logger.info(
            "Topic preload finished: %s created, %s existing, %s seeded, %s failed, %s skipped.",
            created, existing, seeded, failed, skipped,
        )

    def _topic_lock(self, max_chat_id) -> asyncio.Lock:
        # No await between the get and the set, so this is atomic on the loop.
        key = str(max_chat_id)
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
        return lock

    def _existing_topic_target(self, max_chat_id, title, chat_type, sender):
        existing = self._state.get_topic(max_chat_id)
        if not (existing and existing.get("telegram_thread_id")):
            return None
        self._state.save_topic(
            max_chat_id,
            thread_id=existing["telegram_thread_id"],
            title=existing.get("title") or title,
            chat_type=existing.get("chat_type") or chat_type,
            sender=sender,
        )
        return (self._forum_chat_id, existing["telegram_thread_id"], True)

    async def _telegram_target(self, max_chat_id, title: str, chat_type: str,
                               sender: str) -> tuple[int | str, int | None, bool]:
        if not self._topics_enabled:
            return self._fallback_chat_id, None, False

        target = self._existing_topic_target(max_chat_id, title, chat_type, sender)
        if target is not None:
            return target

        # Serialize creation per chat: concurrent packets from a brand-new chat
        # must not both call createForumTopic (would make duplicate topics).
        async with self._topic_lock(max_chat_id):
            # Re-check under the lock — another task may have just created it.
            target = self._existing_topic_target(max_chat_id, title, chat_type, sender)
            if target is not None:
                return target

            topic_title = normalize_topic_title(title, f"MAX чат {max_chat_id}")
            try:
                thread_id = await asyncio.to_thread(
                    tg.create_forum_topic, self._token, self._forum_chat_id, topic_title
                )
            except Exception as exc:
                _logger.warning("Could not create Telegram topic for MAX chat %s: %s",
                                max_chat_id, exc)
                return self._fallback_chat_id, None, False

            self._state.save_topic(
                max_chat_id,
                thread_id=thread_id,
                title=topic_title,
                chat_type=chat_type,
                sender=sender,
            )
            _logger.info("Created Telegram topic %s for MAX chat %s (%s)",
                         thread_id, max_chat_id, topic_title)
            return self._forum_chat_id, thread_id, True

    @staticmethod
    def _topic_body(sender: str, text: str, notes: list[str]) -> str:
        body = "\n".join(part for part in [text, *notes] if part)
        return f"{sender}:\n{body}" if body else f"{sender}:"

    async def _message_sender_name(self, client: MaxClient, sender_id) -> str:
        if sender_id is not None and sender_id == self._own_id:
            return "Вы"
        if isinstance(sender_id, int):
            return await self._resolve_sender_name(client, sender_id)
        return "MAX"

    async def _seed_last_message(self, client: MaxClient, chat_id, thread_id: int,
                                 message: dict) -> bool:
        if not self._config.get("telegram_seed_last_messages"):
            return False
        if not isinstance(message, dict):
            return False
        message_id = message.get("id")
        if message_id is None:
            return False

        topic = self._state.get_topic(chat_id) or {}
        if str(topic.get("last_seeded_max_message_id")) == str(message_id):
            return False

        text = (message.get("text") or "").strip()
        parsed = attaches.parse(message)
        resolvable = {"file_resolve", "video_resolve"}
        media = [item for item in parsed if item.kind in _MEDIA_SENDERS]
        to_resolve = [item for item in parsed if item.kind in resolvable]
        notes = [
            item.text for item in parsed
            if item.kind not in _MEDIA_SENDERS and item.kind not in resolvable
        ]
        if not text and not notes and not media and not to_resolve:
            return False

        sender = await self._message_sender_name(client, message.get("sender"))
        first_msg_id = None
        header_sent = False
        if text or notes or (not media and not to_resolve):
            body = self._topic_body(sender, text, notes)
            first_msg_id = await asyncio.to_thread(
                tg.send_message, self._token, self._forum_chat_id, body,
                message_thread_id=thread_id,
            )
            self._remember(
                first_msg_id, chat_id, message_id, sender,
                self._forum_chat_id, thread_id,
            )
            header_sent = True

        ctx = (
            client, f"MAX | {sender} (chat {chat_id})", chat_id, message_id, sender,
            self._forum_chat_id, thread_id, True,
        )
        for item in media:
            header_sent = await self._send_media_item(item, header_sent, ctx)
        for item in to_resolve:
            header_sent = await self._send_resolved_item(item, header_sent, ctx)

        self._state.mark_seeded_message(
            chat_id,
            max_message_id=message_id,
            telegram_message_id=first_msg_id,
        )
        return True

    # --- MAX -> Telegram -----------------------------------------------------

    async def _on_packet(self, client: MaxClient, packet: dict) -> None:
        # vkmax spawns this as a bare fire-and-forget task; hold a strong ref so
        # the event loop can't garbage-collect it mid-forward.
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)
        # Drop packets from a torn-down session (the active client was replaced),
        # so a stale handler can't act against a dead/new connection.
        if client is not self._client:
            return
        # Guard before any .get(): a non-dict frame would raise AttributeError
        # in this fire-and-forget task (an unretrieved-exception log), not the
        # clean handling below. _recv_loop already filters these, but this keeps
        # the handler robust to any caller.
        if not isinstance(packet, dict):
            return
        if packet.get("opcode") != INCOMING_MESSAGE_OPCODE:
            return
        if self._forward_sem is None:
            self._forward_sem = asyncio.Semaphore(MEDIA_CONCURRENCY)
        async with self._forward_sem:
            try:
                payload = packet.get("payload", {})
                message = payload.get("message", {})
                sender_id = message.get("sender")
                if sender_id is not None and sender_id == self._own_id:
                    return  # our own outgoing message echoed back

                chat_id = payload.get("chatId")
                max_message_id = message.get("id")
                text = (message.get("text") or "").strip()
                parsed = attaches.parse(message)
                if message.get("attaches"):
                    _log_raw_attaches(message)

                sender = (await self._resolve_sender_name(client, sender_id)
                          if isinstance(sender_id, int) else "неизвестный отправитель")
                chat_title, chat_type = self._extract_chat_meta(payload, sender)
                header = f"MAX | {sender} (чат {chat_id})"

                await self._forward(client, header, text, parsed,
                                    chat_id, max_message_id, sender,
                                    chat_title, chat_type)
                _logger.info("Forwarded from %s (chat %s, %d attach)",
                             sender, chat_id, len(parsed))
            except Exception:
                _logger.exception("Failed to handle packet: %s", packet)

    async def _forward(self, client, header, text, parsed,
                       chat_id, max_message_id, sender, chat_title, chat_type):
        resolvable = {"file_resolve", "video_resolve"}
        media = [p for p in parsed if p.kind in _MEDIA_SENDERS]
        to_resolve = [p for p in parsed if p.kind in resolvable]
        notes = [p.text for p in parsed
                 if p.kind not in _MEDIA_SENDERS and p.kind not in resolvable]
        telegram_chat_id, thread_id, in_topic = await self._telegram_target(
            chat_id, chat_title, chat_type, sender
        )

        ctx = (client, header, chat_id, max_message_id, sender,
               telegram_chat_id, thread_id, in_topic)
        header_sent = False
        # A leading text message when there is text, notes, or nothing else.
        if text or notes or (not media and not to_resolve):
            body = (self._topic_body(sender, text, notes)
                    if in_topic else
                    ("\n".join(part for part in [header, text, *notes] if part) or header))
            msg_id = await asyncio.to_thread(tg.send_message, self._token,
                                             telegram_chat_id, body,
                                             message_thread_id=thread_id)
            self._remember(msg_id, chat_id, max_message_id, sender,
                           telegram_chat_id, thread_id)
            header_sent = True

        for item in media:
            header_sent = await self._send_media_item(item, header_sent, ctx)
        for item in to_resolve:
            header_sent = await self._send_resolved_item(item, header_sent, ctx)

    @staticmethod
    def _caption(header, header_sent, item_text):
        return item_text if header_sent else f"{header}\n{item_text}"

    async def _send_note(self, telegram_chat_id, text, thread_id):
        """Send a plain-text note; on failure log at error and return None so a
        broken Telegram destination is visible instead of silently dropped."""
        try:
            return await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, text,
                message_thread_id=thread_id)
        except Exception as exc:
            _logger.error("Could not deliver note to Telegram chat %s: %s",
                          telegram_chat_id, exc)
            return None

    async def _send_media_item(self, item, header_sent, ctx) -> bool:
        _client, header, chat_id, max_message_id, sender, telegram_chat_id, thread_id, in_topic = ctx
        caption = (item.text if header_sent else f"{sender}:\n{item.text}"
                   if in_topic else self._caption(header, header_sent, item.text))
        sender_fn, supports_caption = _MEDIA_SENDERS[item.kind]
        try:
            if supports_caption:
                msg_id = await asyncio.to_thread(
                    sender_fn, self._token, telegram_chat_id, item.url, caption,
                    message_thread_id=thread_id)
            else:
                msg_id = await asyncio.to_thread(
                    sender_fn, self._token, telegram_chat_id, item.url,
                    message_thread_id=thread_id)
        except Exception as exc:
            _logger.warning("Failed to send %s: %s", item.kind, exc)
            msg_id = await self._send_note(
                telegram_chat_id, f"{caption} [не удалось переслать медиа]",
                thread_id)
        self._remember(msg_id, chat_id, max_message_id, sender,
                       telegram_chat_id, thread_id)
        return True

    async def _send_resolved_item(self, item, header_sent, ctx) -> bool:
        """Resolve a file/video to a temporary URL, then upload it to Telegram."""
        client, header, chat_id, max_message_id, sender, telegram_chat_id, thread_id, in_topic = ctx
        caption = (item.text if header_sent else f"{sender}:\n{item.text}"
                   if in_topic else self._caption(header, header_sent, item.text))
        if item.size and item.size > TELEGRAM_UPLOAD_LIMIT:
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id,
                f"{caption} [слишком большой для Telegram] — открыть в MAX",
                message_thread_id=thread_id)
            self._remember(msg_id, chat_id, max_message_id, sender,
                           telegram_chat_id, thread_id)
            return True
        try:
            if item.kind == "file_resolve":
                url = await mediamax.resolve_file_url(
                    client, item.file_id, chat_id, max_message_id)
                msg_id = await asyncio.to_thread(
                    tg.send_document, self._token, telegram_chat_id, url,
                    caption, item.filename, message_thread_id=thread_id)
            else:  # video_resolve
                url = await mediamax.resolve_video_url(
                    client, item.video_id, chat_id, max_message_id)
                msg_id = await asyncio.to_thread(
                    tg.send_video, self._token, telegram_chat_id, url, caption,
                    message_thread_id=thread_id)
        except Exception as exc:
            _logger.warning("Failed to resolve/send %s: %s", item.kind, exc)
            msg_id = await self._send_note(
                telegram_chat_id, f"{caption} — открыть в MAX", thread_id)
        self._remember(msg_id, chat_id, max_message_id, sender,
                       telegram_chat_id, thread_id)
        return True

    # --- Telegram -> MAX -----------------------------------------------------

    async def _send_reply_to_max(self, target: dict, text: str) -> None:
        telegram_chat_id = target.get("telegram_chat_id") or self._fallback_chat_id
        thread_id = target.get("message_thread_id")
        if self._client is None:
            await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id,
                "⚠️ MAX сейчас не подключён, ответ не отправлен. Повторите позже.",
                message_thread_id=thread_id)
            return
        chat_id = target["chat_id"]
        message_id = target.get("message_id")
        try:
            if message_id is not None:
                await max_reply(self._client, chat_id, text, message_id)
            else:
                await max_send(self._client, chat_id, text)
        except Exception as exc:
            _logger.warning("Could not send Telegram reply to MAX chat %s: %s",
                            chat_id, exc)
            await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id,
                f"Не удалось отправить в MAX: {exc}",
                message_thread_id=thread_id)
            return
        if self._confirm_sent:
            await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id,
                f"✅ Отправлено в MAX → {target.get('sender', 'чат')}",
                message_thread_id=thread_id)

    @staticmethod
    def _telegram_media_note(message: dict) -> str | None:
        if message.get("sticker"):
            sticker = message["sticker"]
            emoji = sticker.get("emoji") or ""
            return f"[Telegram sticker {emoji}]".strip()
        if message.get("document"):
            document = message["document"]
            name = document.get("file_name") or "file"
            return f"[Telegram file: {name}]"
        if message.get("photo"):
            return "[Telegram photo]"
        if message.get("video"):
            video = message["video"]
            name = video.get("file_name") or "video"
            return f"[Telegram video: {name}]"
        if message.get("animation"):
            animation = message["animation"]
            name = animation.get("file_name") or "animation"
            return f"[Telegram animation: {name}]"
        if message.get("voice"):
            return "[Telegram voice message]"
        if message.get("audio"):
            audio = message["audio"]
            name = audio.get("file_name") or audio.get("title") or "audio"
            return f"[Telegram audio: {name}]"
        if message.get("video_note"):
            return "[Telegram video note]"
        return None

    @staticmethod
    def _telegram_attachment(message: dict) -> dict | None:
        if message.get("sticker"):
            sticker = message["sticker"]
            if sticker.get("is_animated"):
                ext, mime_type = "tgs", "application/x-tgsticker"
            elif sticker.get("is_video"):
                ext, mime_type = "webm", "video/webm"
            else:
                ext, mime_type = "webp", "image/webp"
            unique = sticker.get("file_unique_id") or sticker.get("file_id") or "sticker"
            return {
                "file_id": sticker.get("file_id"),
                "filename": f"telegram-sticker-{unique}.{ext}",
                "mime_type": mime_type,
                "kind": "file",
            }
        if message.get("document"):
            document = message["document"]
            return {
                "file_id": document.get("file_id"),
                "filename": document.get("file_name") or "telegram-file",
                "mime_type": document.get("mime_type") or "application/octet-stream",
                "kind": "file",
            }
        if message.get("photo"):
            photo = message["photo"][-1]
            return {
                "file_id": photo.get("file_id"),
                "filename": "telegram-photo.jpg",
                "mime_type": "image/jpeg",
                "kind": "photo",
            }
        for key, fallback_name, fallback_mime, kind in (
            ("animation", "telegram-animation.mp4", "video/mp4", "video"),
            ("video", "telegram-video.mp4", "video/mp4", "video"),
            ("voice", "telegram-voice.ogg", "audio/ogg", "file"),
            ("audio", "telegram-audio.mp3", "audio/mpeg", "file"),
            ("video_note", "telegram-video-note.mp4", "video/mp4", "video"),
        ):
            item = message.get(key)
            if item:
                return {
                    "file_id": item.get("file_id"),
                    "filename": item.get("file_name") or fallback_name,
                    "mime_type": item.get("mime_type") or fallback_mime,
                    "kind": kind,
                }
        return None

    @classmethod
    def _telegram_outgoing_text(cls, message: dict) -> str:
        text = (message.get("text") or message.get("caption") or "").strip()
        media_note = cls._telegram_media_note(message)
        if media_note and text:
            return f"{text}\n\n{media_note}"
        return text or media_note or ""

    async def _send_telegram_update_to_max(self, target: dict, message: dict) -> None:
        attachment = self._telegram_attachment(message)
        caption = (message.get("caption") or "").strip()
        if attachment and attachment.get("file_id"):
            telegram_chat_id = target.get("telegram_chat_id") or self._fallback_chat_id
            thread_id = target.get("message_thread_id")
            if self._client is None:
                await self._send_reply_to_max(target, self._telegram_outgoing_text(message))
                return
            try:
                content, _file_path = await asyncio.to_thread(
                    tg.download_file_by_id,
                    self._token,
                    attachment["file_id"],
                )
                await mediamax.send_uploaded_media(
                    self._client,
                    target["chat_id"],
                    content,
                    attachment["filename"],
                    attachment["mime_type"],
                    kind=attachment.get("kind", "file"),
                    text=caption,
                    reply_to_message_id=target.get("message_id"),
                )
                if self._confirm_sent:
                    await asyncio.to_thread(
                        tg.send_message,
                        self._token,
                        telegram_chat_id,
                        f"✅ Файл отправлен в MAX → {target.get('sender', 'чат')}",
                        message_thread_id=thread_id,
                    )
                return
            except Exception as exc:
                _logger.warning("Could not upload Telegram media to MAX chat %s: %s",
                                target.get("chat_id"), exc)
                fallback_text = self._telegram_outgoing_text(message)
                if fallback_text:
                    await self._send_reply_to_max(target, fallback_text)
                else:
                    await asyncio.to_thread(
                        tg.send_message,
                        self._token,
                        telegram_chat_id,
                        f"Не удалось отправить файл в MAX: {exc}",
                        message_thread_id=thread_id,
                    )
                return

        text = self._telegram_outgoing_text(message)
        if text:
            await self._send_reply_to_max(target, text)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        # Only accept commands from the configured owner chat (tolerate the id
        # being stored/sent as int vs str).
        incoming_chat = message.get("chat", {}).get("id")
        allowed_chats = {str(self._chat_id), str(self._fallback_chat_id)}
        if self._forum_chat_id:
            allowed_chats.add(str(self._forum_chat_id))
        if str(incoming_chat) not in allowed_chats:
            return
        text = self._telegram_outgoing_text(message)
        if text.startswith("/"):
            return
        # Act on real content, not on the display note: an attachment with no
        # caption (and no media-note label) must still be routed to MAX.
        if not text and not self._telegram_attachment(message):
            return
        reply = message.get("reply_to_message")
        target = self._reply_map.get(reply.get("message_id")) if reply else None
        if target:
            await self._send_telegram_update_to_max(target, message)
        elif self._forum_chat_id and str(incoming_chat) == str(self._forum_chat_id):
            thread_id = message.get("message_thread_id")
            topic = self._state.find_by_thread(thread_id) if thread_id else None
            if topic:
                await self._send_telegram_update_to_max({
                    "chat_id": topic["max_chat_id"],
                    "message_id": None,
                    "sender": topic.get("title") or "чат",
                    "telegram_chat_id": self._forum_chat_id,
                    "message_thread_id": thread_id,
                }, message)
        else:
            await asyncio.to_thread(
                tg.send_message, self._token, incoming_chat,
                "ℹ️ Чтобы ответить в MAX, сделайте «Ответить» (Reply / свайп) "
                "на пересланном сообщении и напишите текст.")

    async def _poll_telegram(self) -> None:
        """Long-poll Telegram for replies; skip the backlog on startup."""
        offset = None
        try:
            backlog = await asyncio.to_thread(tg.get_updates, self._token, None, 0)
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        except Exception as exc:
            _logger.warning("Telegram backlog drain failed: %s", exc)
        fail_delay = 5
        while True:
            try:
                updates = await asyncio.to_thread(tg.get_updates, self._token, offset, 25)
                fail_delay = 5
            except Exception as exc:
                if "409" in str(exc) or "Conflict" in str(exc):
                    _logger.error("Telegram getUpdates 409 Conflict — another "
                                  "instance is polling this bot. Retry in %ss.",
                                  fail_delay)
                else:
                    _logger.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(fail_delay)
                fail_delay = min(fail_delay * 2, 60)
                continue
            for update in updates:
                uid = update.get("update_id")
                if uid is None:
                    continue  # advance past anything malformed
                offset = uid + 1
                try:
                    await self._handle_update(update)
                except Exception:
                    _logger.exception("Failed to handle Telegram update")

    # --- MAX session lifecycle ----------------------------------------------

    async def _run_session(self) -> None:
        client = BrowserMaxClient()
        await client.connect()
        try:
            preload_topics = bool(
                self._topics_enabled and self._config.get("telegram_preload_topics")
            )
            login_response = await client.login_by_token(
                self._config["max_login_token"],
                chats_sync=1 if preload_topics else 0,
                contacts_sync=1 if preload_topics else 0,
                chats_count=int(self._config.get("telegram_preload_chat_count") or 100),
            )
            self._own_id = _extract_own_id(login_response)
            self._client = client
            await self._preload_topics_from_login(client, login_response)
            await client.set_callback(self._on_packet)
            _logger.info("Bridge online (own id: %s).", self._own_id)
            if self._topics_enabled:
                print("Мост запущен. Сообщения MAX идут в темы Telegram; ответы — в теме или через Reply.")
            else:
                print("Мост запущен. Сообщения MAX идут в Telegram; ответы — через Reply.")
            await client._connection.wait_closed()
            _logger.warning("MAX connection closed by server.")
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _max_loop(self) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            started = loop.time()
            try:
                await self._run_session()
            except MaxAuthError as exc:
                _logger.error("MAX auth failed: %s", exc)
                print("Похоже, токен MAX устарел. Удалите config.json и "
                      "пройдите настройку заново.")
            except Exception as exc:
                _logger.error("Session error: %s", exc)
            # A session that stayed up a while resets the backoff; rapid repeated
            # drops (e.g. a revoked token) escalate the delay and warn the user.
            if loop.time() - started > 120:
                failures = 0
            failures += 1
            delay = min(RECONNECT_DELAY_SECONDS * (2 ** (failures - 1)),
                        RECONNECT_MAX_DELAY)
            if failures == 5:
                _logger.error("MAX keeps disconnecting %d times in a row — the "
                              "token is likely revoked.", failures)
                print("⚠️ MAX постоянно отключается — возможно, токен отозван. "
                      "Возьмите свежий токен (web.max.ru) и перезапустите.")
            _logger.info("Reconnecting in %s seconds...", delay)
            await asyncio.sleep(delay)

    async def run_forever(self) -> None:
        await asyncio.gather(self._max_loop(), self._poll_telegram())
