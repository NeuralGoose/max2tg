"""MAX <-> Telegram bridge.

MAX -> Telegram: forwards incoming messages (text + attachments) to a Telegram
chat. Telegram -> MAX: when the user *replies* (Reply/свайп) to a forwarded
message in Telegram, the reply text is sent back to the originating MAX chat.
"""
import asyncio
import logging
import re
from collections import OrderedDict

import attaches
import maxactions
import mediamax
import message_content
import tg
from max_client import build_max_client
from maxauth import TelegramAuthPoll
from state import BridgeState, normalize_topic_title

_logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 15
RECONNECT_MAX_DELAY = 300
REPLY_MAP_LIMIT = 10000
NAME_CACHE_LIMIT = 5000
# Bound the edit-dedup fingerprint map and the per-chat topic-lock map so a
# long-running bridge over many chats does not grow them without limit.
CONTENT_FP_LIMIT = 10000
TOPIC_LOCK_LIMIT = 5000
# Cap concurrent inbound-message handlers so a media burst can't exhaust the
# asyncio to_thread pool and starve the Telegram long-poll.
MEDIA_CONCURRENCY = 8
# Telegram bots can upload at most 50 MB; leave headroom.
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024
# Allowed icon_color values for createForumTopic (Telegram Bot API).
_FORUM_ICON_COLORS = (7322096, 16766590, 13338331, 9367192, 16749490, 16478047)

# Owner-only Telegram commands to drive MAX (join chats, find people, start DMs).
_HELP_TEXT = (
    "🤖 Что я умею\n\n"
    "📥 Пересылаю сюда сообщения из MAX. Ответить — Reply (свайп) на пересланном.\n\n"
    "➕ Вступить в чат/канал — просто пришлите ссылку (команда не нужна):\n"
    "   https://max.ru/join/…\n\n"
    "🔍 Найти человека — пришлите телефон или @ник, получите его id:\n"
    "   +79991234567   ·   @nickname\n\n"
    "✍️ Написать новому человеку — /dm <id> <текст> (id берётся из 🔍):\n"
    "   /dm 21243808 привет\n\n"
    "⌨️ Ещё: /join <ссылка>, /find <телефон|@ник|id>, /help."
)
_WELCOME_TEXT = "👋 Привет! Я зеркалю ваш MAX в Telegram.\n\n" + _HELP_TEXT
# Registered in Telegram's "/" menu so the commands are discoverable.
_BOT_COMMANDS = [
    {"command": "join", "description": "Вступить в канал/группу/чат MAX по ссылке"},
    {"command": "find", "description": "Найти человека/канал: телефон, @ник, id"},
    {"command": "dm", "description": "Написать человеку: /dm <id из /find> <текст>"},
    {"command": "help", "description": "Справка по командам"},
]

# Bare-message shortcuts: send a link / phone / @username with no slash command.
_SMART_MAX_LINK = re.compile(r"\S*max\.ru/\S+", re.IGNORECASE)
_SMART_USERNAME = re.compile(r"@[A-Za-z0-9_.]{3,32}")
_SMART_PHONE = re.compile(r"[+]?\d[\d\s()\-]{6,18}")

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
# Direct-URL kinds that can be batched into a Telegram album.
_ALBUM_KINDS = frozenset({"photo", "video"})
_MAX_ALBUM_SIZE = 10


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
        # The active PyMax client (WebClient or Client), set once started.
        self._client = None
        self._state = BridgeState()
        # telegram message_id -> {"chat_id", "message_id", "sender"}
        self._reply_map: "OrderedDict[int, dict]" = OrderedDict()
        # Per-MAX-chat locks serialize topic creation so two concurrent messages
        # from a brand-new chat cannot create duplicate Telegram topics.
        self._topic_locks: "dict[str, asyncio.Lock]" = {}
        # Lazily created (needs a running loop): bounds concurrent forwards.
        self._forward_sem: "asyncio.Semaphore | None" = None
        # Shared Telegram getUpdates cursor handed from interactive SMS auth to
        # the main poll loop; the long-poll task itself (started once).
        self._auth_poll: "TelegramAuthPoll | None" = None
        self._tg_poll_task: "asyncio.Task | None" = None
        # Hot cache of (max_chat_id, max_message_id) already sent to Telegram.
        self._delivered_cache: set[tuple[str, str]] = set()
        # Last seen content fingerprint per message (for stats-only edit skip).
        self._content_fingerprints: "OrderedDict[tuple[str, str], str]" = OrderedDict()
        self._excluded_chat_ids = frozenset(
            self._config.get("telegram_exclude_chat_ids") or {0},
        )
        self._forum_icon_sticker_ids_cache: list[str] | None = None
        tg.set_api_min_interval(
            float(config.get("telegram_api_min_interval_seconds") or 0.05),
        )

    # --- helpers -------------------------------------------------------------

    def _hydrate_delivered_cache(self) -> None:
        self._delivered_cache.clear()
        for chat_id, topic in self._state._data.get("topics", {}).items():
            if not isinstance(topic, dict):
                continue
            for mid in topic.get("delivered_max_message_ids") or []:
                self._delivered_cache.add((str(chat_id), str(mid)))
            legacy = topic.get("last_seeded_max_message_id")
            if legacy:
                self._delivered_cache.add((str(chat_id), str(legacy)))
        # Chats delivered without a topic (fallback / legacy single-chat mode).
        for chat_id, ids in self._state.delivered_no_topic_map().items():
            for mid in ids:
                self._delivered_cache.add((str(chat_id), str(mid)))

    def _is_delivered(self, chat_id, message_id) -> bool:
        key = (str(chat_id), str(message_id))
        if key in self._delivered_cache:
            return True
        return self._state.is_delivered(chat_id, message_id)

    def _mark_delivered(self, chat_id, message_id) -> None:
        key = (str(chat_id), str(message_id))
        if key in self._delivered_cache:
            return
        self._delivered_cache.add(key)
        self._state.mark_delivered(chat_id, message_id)

    def _mark_max_outbound(self, chat_id, sent_message) -> None:
        """Mark a MAX message we sent (Telegram reply, /dm, etc.) so its echo is not re-forwarded."""
        if sent_message is None or chat_id is None:
            return
        message_id = getattr(sent_message, "id", None)
        if message_id is not None:
            self._mark_delivered(chat_id, message_id)

    def _is_excluded_chat(self, chat_id) -> bool:
        try:
            return int(chat_id) in self._excluded_chat_ids
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_locale_system_text(text: str | None) -> bool:
        return bool(text) and str(text).startswith("welcome.")

    def _forum_icon_sticker_ids(self) -> list[str]:
        if self._forum_icon_sticker_ids_cache is not None:
            return self._forum_icon_sticker_ids_cache
        try:
            self._forum_icon_sticker_ids_cache = (
                tg.get_forum_topic_icon_sticker_ids(self._token)
            )
        except Exception as exc:
            _logger.warning("Could not load forum topic icon stickers: %s", exc)
            self._forum_icon_sticker_ids_cache = []
        return self._forum_icon_sticker_ids_cache

    async def _ensure_icon_stickers_loaded(self) -> None:
        """Populate the forum icon-sticker cache off the event loop (the
        underlying Telegram call is blocking HTTP via requests, so calling it
        directly from an async path would stall the loop on first use)."""
        if self._forum_icon_sticker_ids_cache is not None:
            return
        await asyncio.to_thread(self._forum_icon_sticker_ids)

    def _topic_icon_for_chat(self, chat_id) -> tuple[int, str | None]:
        sticker_ids = self._forum_icon_sticker_ids()
        key = abs(int(chat_id))
        color = _FORUM_ICON_COLORS[key % len(_FORUM_ICON_COLORS)]
        emoji_id = sticker_ids[key % len(sticker_ids)] if sticker_ids else None
        return color, emoji_id

    def _mark_topic_icon_set(self, max_chat_id) -> None:
        topic = self._state.get_topic(max_chat_id)
        if not topic:
            return
        topic["topic_icon_set"] = True
        self._state._data["topics"][str(max_chat_id)] = topic
        self._state.save()

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

    @staticmethod
    def _display_name(user) -> str | None:
        """Fullest display name from a PyMax User.names (list[Name])."""
        candidates: list[str] = []
        for entry in getattr(user, "names", None) or []:
            first = getattr(entry, "first_name", None) or ""
            last = getattr(entry, "last_name", None) or ""
            full = f"{first} {last}".strip()
            if full:
                candidates.append(full)
            name = getattr(entry, "name", None)
            if name:
                candidates.append(str(name).strip())
        candidates = [c for c in candidates if c]
        if not candidates:
            return None
        # Prefer the fullest: most words first, then longest string.
        return max(candidates, key=lambda s: (len(s.split()), len(s)))

    async def _resolve_sender_name(self, client, sender_id: int) -> str:
        """Resolve a MAX user id to a display name via client.get_user (cached)."""
        if sender_id in self._name_cache:
            self._name_cache.move_to_end(sender_id)  # keep hot senders
            return self._name_cache[sender_id]
        name = str(sender_id)
        try:
            user = await client.get_user(sender_id)
            display = self._display_name(user) if user else None
            if display:
                name = display
        except Exception as exc:
            _logger.warning("Could not resolve user %s: %s", sender_id, exc)
        self._name_cache[sender_id] = name
        while len(self._name_cache) > NAME_CACHE_LIMIT:
            self._name_cache.popitem(last=False)
        return name

    def _dialog_peer_id(self, chat):
        """The other participant in a PyMax dialog Chat (participants: dict)."""
        participants = getattr(chat, "participants", None)
        if isinstance(participants, dict):
            for raw_id in participants:
                try:
                    participant_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if participant_id != self._own_id:
                    return participant_id
        return getattr(chat, "cid", None) or getattr(chat, "id", None)

    async def _chat_meta(self, client, chat_id, sender: str) -> tuple[str, str]:
        """Title + lowercased chat type from PyMax client.get_chat, with a
        dialog/sender fallback when the chat has no usable title."""
        title, chat_type = "", ""
        try:
            chat = await client.get_chat(chat_id)
        except Exception as exc:
            _logger.warning("Could not fetch MAX chat %s: %s", chat_id, exc)
            chat = None
        if chat is not None:
            title = (getattr(chat, "title", None) or "").strip()
            ctype = getattr(chat, "type", None)
            chat_type = str(getattr(ctype, "value", ctype) or "").lower()
        if not title:
            title = (sender if sender and sender != "неизвестный отправитель"
                     else f"MAX чат {chat_id}")
        if not chat_type:
            chat_type = "dialog"
        return title, chat_type

    async def _sync_chat_meta(self, client, chat) -> tuple[str, str, str]:
        """Title/type/sender for a preloaded PyMax Chat object."""
        chat_id = getattr(chat, "id", None)
        ctype = getattr(chat, "type", None)
        chat_type = str(getattr(ctype, "value", ctype) or "dialog").lower()
        title = (getattr(chat, "title", None) or "").strip()

        if chat_type == "dialog":
            # MAX shows the peer's full contact name for dialogs, which is more
            # reliable than the chat's own (often missing/partial) title field.
            contact_id = self._dialog_peer_id(chat)
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

    async def _refresh_topic_icon(self, max_chat_id, thread_id: int) -> bool:
        existing = self._state.get_topic(max_chat_id) or {}
        if existing.get("topic_icon_set"):
            return False
        await self._ensure_icon_stickers_loaded()
        _color, icon_emoji_id = self._topic_icon_for_chat(max_chat_id)
        if not icon_emoji_id:
            return False
        try:
            await asyncio.to_thread(
                tg.edit_forum_topic,
                self._token,
                self._forum_chat_id,
                thread_id,
                icon_custom_emoji_id=icon_emoji_id,
            )
        except Exception as exc:
            _logger.warning("Could not set topic icon for MAX chat %s: %s",
                            max_chat_id, exc)
            return False
        self._mark_topic_icon_set(max_chat_id)
        _logger.info("Set Telegram topic icon for MAX chat %s (thread %s)",
                     max_chat_id, thread_id)
        return True

    async def _collect_preload_chats(self, client) -> tuple[list, int]:
        """Gather chats for topic preload: login cache, optionally fetch_chats."""
        limit = int(self._config.get("telegram_preload_chat_count") or 100)
        source = str(self._config.get("telegram_preload_chat_source") or "login").lower()
        fetch_pages = int(self._config.get("telegram_preload_fetch_pages") or 20)

        merged: list = []
        seen: set = set()

        def _add(chat) -> None:
            chat_id = getattr(chat, "id", None)
            if chat_id is None or chat_id in seen:
                return
            if self._is_excluded_chat(chat_id):
                return
            seen.add(chat_id)
            merged.append(chat)

        for chat in list(getattr(client, "chats", None) or []):
            _add(chat)

        if source == "fetch":
            fetch_fn = getattr(client, "fetch_chats", None)
            if fetch_fn is not None:
                marker = None
                prev_marker = None
                for _ in range(fetch_pages):
                    batch = await fetch_fn(marker)
                    if not batch:
                        break
                    for chat in batch:
                        _add(chat)
                    times = [
                        getattr(c, "last_event_time", 0) or 0
                        for c in batch
                    ]
                    if not times:
                        break
                    next_marker = min(times) - 1
                    if next_marker == prev_marker:
                        break
                    prev_marker = marker
                    marker = next_marker

        discovered = len(merged)
        return merged[:limit], discovered

    def _prepend_deferred_preload_chats(self, client, chats: list) -> list:
        """Put deferred chat ids first; attach objects from client.chats when found."""
        deferred = self._state.get_pending_preload_chat_ids()
        if not deferred:
            return chats
        by_id: dict[str, object] = {}
        for chat in chats:
            cid = getattr(chat, "id", None)
            if cid is not None:
                by_id[str(cid)] = chat
        for chat in getattr(client, "chats", None) or []:
            cid = getattr(chat, "id", None)
            if cid is not None:
                by_id.setdefault(str(cid), chat)
        merged: list = []
        seen: set[str] = set()
        for did in deferred:
            if did in by_id and did not in seen:
                merged.append(by_id[did])
                seen.add(did)
        for chat in chats:
            cid = getattr(chat, "id", None)
            if cid is None:
                continue
            sid = str(cid)
            if sid not in seen:
                merged.append(chat)
                seen.add(sid)
        return merged

    async def _await_with_rate_limit_retry(
        self,
        coro_fn,
        stats: dict,
        *,
        max_attempts: int = 3,
    ):
        for attempt in range(max_attempts):
            try:
                return await coro_fn()
            except Exception as exc:
                if tg.is_rate_limit_error(exc) and attempt < max_attempts - 1:
                    stats["rate_limited_pauses"] = (
                        stats.get("rate_limited_pauses", 0) + 1
                    )
                    stats["retried"] = stats.get("retried", 0) + 1
                    wait = tg.retry_after_from_error(exc) or 5.0
                    _logger.info("Preload rate limited, sleeping %.1fs", wait)
                    await asyncio.sleep(wait + 0.2)
                    continue
                raise

    async def _preload_topics(self, client) -> None:
        """Pre-create Telegram topics from MAX chats so conversations exist
        before new messages arrive; optionally seed recent history."""
        if not self._topics_enabled or not self._config.get("telegram_preload_topics"):
            return

        normal_interval = float(
            self._config.get("telegram_api_min_interval_seconds") or 0.05,
        )
        preload_interval = float(
            self._config.get("telegram_preload_api_min_interval_seconds") or 1.0,
        )
        tg.set_api_min_interval(preload_interval)
        stats: dict = {
            "rate_limited_pauses": 0,
            "retried": 0,
            "deferred": 0,
        }
        try:
            chats, discovered = await self._collect_preload_chats(client)
            chats = self._prepend_deferred_preload_chats(client, chats)
            raw_depth = self._config.get("telegram_preload_message_depth")
            depth = 1 if raw_depth is None else int(raw_depth)
            delay = float(self._config.get("telegram_preload_chat_delay_seconds") or 0.35)
            seeding_enabled = bool(self._config.get("telegram_seed_last_messages"))
            max_seed_ops = (
                len(chats) * max(depth, 1)
                if seeding_enabled and depth > 0 else 0
            )
            created = existing = failed = skipped = seeded_messages = 0
            topic_targets: dict = {}

            # Phase 1: ensure topics exist (createForumTopic + title refresh).
            for chat in chats:
                chat_id = getattr(chat, "id", None)
                if chat_id is None:
                    skipped += 1
                    continue
                if self._is_excluded_chat(chat_id):
                    skipped += 1
                    continue
                try:

                    async def _ensure_topic(chat=chat, chat_id=chat_id):
                        existing_topic = self._state.get_topic(chat_id)
                        if existing_topic and existing_topic.get("telegram_thread_id"):
                            thread_id = existing_topic["telegram_thread_id"]
                            title, chat_type, sender = await self._sync_chat_meta(
                                client, chat,
                            )
                            await self._refresh_topic_title(
                                chat_id, thread_id, title, chat_type, sender,
                            )
                            await self._refresh_topic_icon(chat_id, thread_id)
                            return thread_id, "existing"
                        title, chat_type, sender = await self._sync_chat_meta(
                            client, chat,
                        )
                        _target_chat_id, thread_id, in_topic = await self._telegram_target(
                            chat_id, title, chat_type, sender,
                        )
                        if in_topic and thread_id:
                            return thread_id, "created"
                        return None, "failed"

                    thread_id, status = await self._await_with_rate_limit_retry(
                        _ensure_topic, stats,
                    )
                    if thread_id:
                        topic_targets[chat_id] = thread_id
                    if status == "existing":
                        existing += 1
                        self._state.remove_pending_preload_chat(chat_id)
                    elif status == "created":
                        created += 1
                        self._state.remove_pending_preload_chat(chat_id)
                    else:
                        # Soft failure (topic create fell back to single chat):
                        # keep it queued so a later run retries instead of
                        # silently dropping it.
                        failed += 1
                        self._state.add_pending_preload_chat(chat_id)
                except Exception as exc:
                    failed += 1
                    stats["deferred"] += 1
                    self._state.add_pending_preload_chat(chat_id)
                    if "thread not found" in str(exc).lower():
                        self._state.delete_topic(chat_id)
                        _logger.warning(
                            "Preload: dropped stale topic for chat %s "
                            "(thread deleted); will recreate.", chat_id,
                        )
                    else:
                        _logger.warning("Preload topic setup skipped chat %s: %s",
                                        chat_id, exc)
                if delay > 0:
                    await asyncio.sleep(delay)

            # Phase 2: seed recent messages (slower; media-heavy).
            if seeding_enabled and depth > 0:
                seed_delay = max(delay, 0.5)
                for chat in chats:
                    chat_id = getattr(chat, "id", None)
                    if chat_id is None:
                        continue
                    if self._is_excluded_chat(chat_id):
                        continue
                    thread_id = topic_targets.get(chat_id)
                    if thread_id is None:
                        topic = self._state.get_topic(chat_id) or {}
                        thread_id = topic.get("telegram_thread_id")
                    if not thread_id:
                        continue
                    if max_seed_ops <= seeded_messages:
                        break
                    try:

                        async def _seed(chat=chat, chat_id=chat_id, thread_id=thread_id):
                            return await self._seed_chat_messages(
                                client, chat_id, thread_id, chat, depth=depth,
                            )

                        count = await self._await_with_rate_limit_retry(_seed, stats)
                        seeded_messages += count
                        self._state.remove_pending_preload_chat(chat_id)
                    except Exception as exc:
                        failed += 1
                        stats["deferred"] += 1
                        self._state.add_pending_preload_chat(chat_id)
                        _logger.warning("Preload seed skipped chat %s: %s",
                                        chat_id, exc)
                    if seed_delay > 0:
                        await asyncio.sleep(seed_delay)

            _logger.info(
                "Topic preload finished: discovered=%s processing=%s "
                "created=%s existing=%s seeded_messages=%s failed=%s skipped=%s "
                "rate_limited_pauses=%s retried=%s deferred=%s.",
                discovered, len(chats), created, existing, seeded_messages,
                failed, skipped,
                stats.get("rate_limited_pauses", 0),
                stats.get("retried", 0),
                stats.get("deferred", 0),
            )
        finally:
            tg.set_api_min_interval(normal_interval)

    def _topic_lock(self, max_chat_id) -> asyncio.Lock:
        # No await between the get and the set, so this is atomic on the loop.
        key = str(max_chat_id)
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
            if len(self._topic_locks) > TOPIC_LOCK_LIMIT:
                self._evict_idle_topic_locks(keep=key)
        return lock

    def _evict_idle_topic_locks(self, *, keep: str) -> None:
        """Drop locks that are not currently held to bound the map. A new lock is
        created on demand next time that chat is seen, so this is safe."""
        for k in list(self._topic_locks):
            if len(self._topic_locks) <= TOPIC_LOCK_LIMIT:
                break
            if k == keep:
                continue
            existing = self._topic_locks.get(k)
            if existing is not None and not existing.locked():
                del self._topic_locks[k]

    def _set_content_fingerprint(self, key: tuple[str, str], fp: str) -> None:
        self._content_fingerprints[key] = fp
        self._content_fingerprints.move_to_end(key)
        while len(self._content_fingerprints) > CONTENT_FP_LIMIT:
            self._content_fingerprints.popitem(last=False)

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
                               sender: str, *,
                               _lock_held: bool = False,
                               ) -> tuple[int | str, int | None, bool]:
        if not self._topics_enabled:
            return self._fallback_chat_id, None, False

        target = self._existing_topic_target(max_chat_id, title, chat_type, sender)
        if target is not None:
            return target

        if _lock_held:
            return await self._create_forum_topic_locked(
                max_chat_id, title, chat_type, sender,
            )

        # Serialize creation per chat: concurrent packets from a brand-new chat
        # must not both call createForumTopic (would make duplicate topics).
        async with self._topic_lock(max_chat_id):
            target = self._existing_topic_target(max_chat_id, title, chat_type, sender)
            if target is not None:
                return target
            return await self._create_forum_topic_locked(
                max_chat_id, title, chat_type, sender,
            )

    async def _create_forum_topic_locked(
        self, max_chat_id, title: str, chat_type: str, sender: str,
    ) -> tuple[int | str, int | None, bool]:
        topic_title = normalize_topic_title(title, f"MAX чат {max_chat_id}")
        await self._ensure_icon_stickers_loaded()
        icon_color, icon_emoji_id = self._topic_icon_for_chat(max_chat_id)
        try:
            thread_id = await asyncio.to_thread(
                tg.create_forum_topic,
                self._token,
                self._forum_chat_id,
                topic_title,
                icon_color=icon_color,
                icon_custom_emoji_id=icon_emoji_id,
            )
        except Exception as exc:
            _logger.warning(
                "Could not create Telegram topic for MAX chat %s: %s — "
                "falling back to single-chat mode (chat %s). Check that the bot "
                "is a forum admin with Manage Topics.",
                max_chat_id, exc, self._fallback_chat_id,
            )
            return self._fallback_chat_id, None, False

        self._state.save_topic(
            max_chat_id,
            thread_id=thread_id,
            title=topic_title,
            chat_type=chat_type,
            sender=sender,
        )
        if icon_emoji_id:
            self._mark_topic_icon_set(max_chat_id)
        _logger.info("Created Telegram topic %s for MAX chat %s (%s)",
                     thread_id, max_chat_id, topic_title)
        return self._forum_chat_id, thread_id, True

    @staticmethod
    def _topic_body(sender: str, text: str, notes: list[str],
                    is_channel: bool = False,
                    attribution: str | None = None) -> str:
        content = "\n".join(part for part in [text, *notes] if part)
        if attribution:
            body = f"{attribution}\n\n{content}" if content else attribution
        else:
            body = content
        if is_channel:
            return body or sender
        return f"{sender}:\n{body}" if body else f"{sender}:"

    @staticmethod
    def _topic_caption(sender: str, item_text: str, is_channel: bool) -> str:
        """First media item's caption inside a topic: a '{sender}:' label, except
        in a channel where it would duplicate the channel name shown as the topic."""
        return item_text if is_channel else f"{sender}:\n{item_text}"

    @staticmethod
    def _delivery_body(sender: str, text: str, notes: list[str], *,
                       is_channel: bool, attribution: str | None,
                       in_topic: bool, header: str) -> str:
        if in_topic:
            return MaxToTelegramBridge._topic_body(
                sender, text, notes, is_channel, attribution,
            )
        parts = [header, attribution, text, *notes]
        return "\n".join(part for part in parts if part) or header

    @staticmethod
    def _split_caption(body: str) -> tuple[str, str | None]:
        """Split body into Telegram caption (<=1024) and optional overflow text."""
        if len(body) <= tg.MAX_CAPTION_LEN:
            return body, None
        cut = body[:tg.MAX_CAPTION_LEN]
        newline = cut.rfind("\n")
        if newline > int(tg.MAX_CAPTION_LEN * 0.7):
            cut = cut[:newline]
        overflow = body[len(cut):].lstrip("\n")
        return cut, overflow or None

    async def _message_sender_name(self, client, sender_id) -> str:
        if sender_id is not None and sender_id == self._own_id:
            return "Вы"
        if isinstance(sender_id, int):
            return await self._resolve_sender_name(client, sender_id)
        return "MAX"

    @staticmethod
    def _normalize_message_time(msg) -> int:
        raw = getattr(msg, "time", 0) or 0
        try:
            t = int(raw)
        except (TypeError, ValueError):
            return 0
        if t >= 1_000_000_000_000:
            return t
        return t * 1000

    @staticmethod
    def _message_id_sort_key(msg) -> int:
        mid = getattr(msg, "id", None)
        if mid is None:
            return 0
        try:
            return int(mid)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _message_chrono_key(cls, msg, *, fallback_index: int) -> tuple[int, int, int]:
        return (
            cls._normalize_message_time(msg),
            cls._message_id_sort_key(msg),
            fallback_index,
        )

    @classmethod
    def _message_chrono_tuple(cls, msg) -> tuple[int, int]:
        return (
            cls._normalize_message_time(msg),
            cls._message_id_sort_key(msg),
        )

    @classmethod
    def _sort_messages_for_seed(cls, messages: list) -> list:
        indexed = list(enumerate(messages))
        indexed.sort(
            key=lambda pair: cls._message_chrono_key(
                pair[1], fallback_index=pair[0],
            ),
        )
        return [msg for _, msg in indexed]

    def _seed_would_break_telegram_order(
        self, chat_id, ordered_messages: list,
    ) -> bool:
        """True when older undelivered messages would append below newer seeded ones."""
        delivered_keys: list[tuple[int, int]] = []
        undelivered_keys: list[tuple[int, int]] = []
        for msg in ordered_messages:
            message_id = getattr(msg, "id", None)
            if message_id is None:
                continue
            key = self._message_chrono_tuple(msg)
            if self._is_delivered(chat_id, message_id):
                delivered_keys.append(key)
            else:
                undelivered_keys.append(key)
        if not delivered_keys or not undelivered_keys:
            return False
        newest_delivered = max(delivered_keys)
        return any(key < newest_delivered for key in undelivered_keys)

    async def _seed_one_message(
        self, client, chat_id, thread_id: int, message,
    ) -> bool:
        if not self._config.get("telegram_seed_last_messages"):
            return False
        if message is None:
            return False
        message_id = getattr(message, "id", None)
        if message_id is None:
            return False

        if self._is_delivered(chat_id, message_id):
            return False

        topic = self._state.get_topic(chat_id) or {}
        chat_title = str(topic.get("title") or f"MAX чат {chat_id}")
        chat_type = str(topic.get("chat_type") or "chat")
        resolved = await message_content.resolve_message_content(
            message,
            client,
            chat_type=chat_type,
            chat_title=chat_title,
            own_id=self._own_id,
            resolve_sender_name=lambda uid: self._resolve_sender_name(client, uid),
        )
        text = resolved.text
        if self._is_locale_system_text(text):
            return False
        parsed = attaches.parse(message_content.content_message(resolved))
        resolvable = {"file_resolve", "video_resolve"}
        media = [item for item in parsed if item.kind in _MEDIA_SENDERS]
        to_resolve = [item for item in parsed if item.kind in resolvable]
        notes = [
            item.text for item in parsed
            if item.kind not in _MEDIA_SENDERS and item.kind not in resolvable
        ]
        if not text and not notes and not media and not to_resolve:
            return False

        is_channel = topic.get("chat_type") == "channel"
        sender = resolved.author
        delivered, first_msg_id, _fully = await self._deliver_to_telegram(
            client,
            f"MAX | {sender} (chat {chat_id})",
            text,
            parsed,
            chat_id,
            message_id,
            sender,
            self._forum_chat_id,
            thread_id,
            in_topic=True,
            is_channel=is_channel,
            attribution=resolved.attribution,
        )
        if not delivered:
            return False

        self._state.mark_seeded_message(
            chat_id,
            max_message_id=message_id,
            telegram_message_id=first_msg_id,
        )
        self._delivered_cache.add((str(chat_id), str(message_id)))
        return True

    async def _seed_chat_messages(
        self, client, chat_id, thread_id: int, chat, *, depth: int,
    ) -> int:
        """Seed up to ``depth`` recent messages into a Telegram topic."""
        if depth <= 0 or not self._config.get("telegram_seed_last_messages"):
            return 0

        async with self._topic_lock(chat_id):
            fetch_history = getattr(client, "fetch_history", None)
            if fetch_history is None:
                last_message = getattr(chat, "last_message", None)
                return 1 if await self._seed_one_message(
                    client, chat_id, thread_id, last_message,
                ) else 0

            try:
                messages = await fetch_history(chat_id, backward=depth)
            except Exception as exc:
                _logger.warning(
                    "Preload history fetch failed for chat %s: %s", chat_id, exc,
                )
                return 0
            if not messages:
                return 0

            ordered = self._sort_messages_for_seed(messages)
            _logger.info(
                "Preload seed chat %s: %d messages, ids oldest→newest: %s",
                chat_id,
                len(ordered),
                [getattr(m, "id", None) for m in ordered],
            )

            if self._seed_would_break_telegram_order(chat_id, ordered):
                _logger.warning(
                    "Preload seed skipped chat %s: older undelivered messages "
                    "would appear below already-seeded newer ones in Telegram. "
                    "Delete the topic or clear topic state in state.json, then "
                    "re-run preload.",
                    chat_id,
                )
                return 0

            seeded = 0
            for message in ordered:
                if await self._seed_one_message(client, chat_id, thread_id, message):
                    seeded += 1
            return seeded

    async def _seed_last_message(self, client, chat_id, thread_id: int,
                                 message) -> bool:
        return await self._seed_one_message(client, chat_id, thread_id, message)

    # --- MAX -> Telegram -----------------------------------------------------

    async def _deliver_to_telegram(
        self,
        client,
        header: str,
        text: str,
        parsed: list,
        chat_id,
        max_message_id,
        sender: str,
        telegram_chat_id,
        thread_id: int | None,
        *,
        in_topic: bool,
        is_channel: bool,
        attribution: str | None = None,
    ) -> tuple[bool, int | None, bool]:
        """Forward parsed MAX content to Telegram.

        Returns (delivered, first_msg_id, fully_delivered). ``delivered`` is True
        if any part reached Telegram; ``fully_delivered`` is False when a piece
        (text or media) failed and only a note/placeholder was sent, so the MAX
        message is not marked delivered and gets another chance after a restart."""
        resolvable = {"file_resolve", "video_resolve"}
        media = [p for p in parsed if p.kind in _MEDIA_SENDERS]
        to_resolve = [p for p in parsed if p.kind in resolvable]
        notes = [
            p.text for p in parsed
            if p.kind not in _MEDIA_SENDERS and p.kind not in resolvable
        ]
        album_media = [
            p for p in media
            if p.kind in _ALBUM_KINDS and p.url
        ]
        other_media = [p for p in media if p not in album_media]

        body = self._delivery_body(
            sender, text, notes,
            is_channel=is_channel,
            attribution=attribution,
            in_topic=in_topic,
            header=header,
        )
        content = "\n".join(part for part in [text, *notes] if part)
        has_content = bool(content.strip()) or bool(attribution)
        can_caption_media = bool(album_media or to_resolve)

        ctx = (
            client, header, chat_id, max_message_id, sender,
            telegram_chat_id, thread_id, in_topic, is_channel,
        )
        delivered = False
        fully_delivered = True
        first_msg_id: int | None = None
        reply_mapped = False
        body_placed = False
        pending_caption: str | None = None
        pending_overflow: str | None = None

        if has_content and can_caption_media:
            pending_caption, pending_overflow = self._split_caption(body)
        elif has_content and not can_caption_media:
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, body,
                message_thread_id=thread_id,
            )
            if msg_id:
                first_msg_id = msg_id
                self._remember(
                    msg_id, chat_id, max_message_id, sender,
                    telegram_chat_id, thread_id,
                )
                reply_mapped = True
                delivered = True
            else:
                fully_delivered = False
            body_placed = True

        if album_media:
            while album_media:
                batch = album_media[:_MAX_ALBUM_SIZE]
                album_media = album_media[_MAX_ALBUM_SIZE:]
                caption = pending_caption
                pending_caption = None
                if len(batch) >= 2:
                    items = [
                        {"type": item.kind, "url": item.url}
                        for item in batch
                    ]
                    try:
                        ids = await asyncio.to_thread(
                            tg.send_media_group,
                            self._token,
                            telegram_chat_id,
                            items,
                            caption,
                            message_thread_id=thread_id,
                        )
                    except Exception as exc:
                        _logger.warning("Failed to send media group: %s", exc)
                        ids = []
                    if ids and not reply_mapped:
                        first_msg_id = ids[0]
                        self._remember(
                            ids[0], chat_id, max_message_id, sender,
                            telegram_chat_id, thread_id,
                        )
                        reply_mapped = True
                    if ids:
                        delivered = True
                        body_placed = True
                    else:
                        for item in batch:
                            body_placed, msg_id, ok = await self._send_media_item(
                                item, body_placed, ctx,
                                caption_override=caption,
                                remember=not reply_mapped,
                            )
                            if msg_id and not reply_mapped:
                                first_msg_id = msg_id
                                reply_mapped = True
                            if msg_id:
                                delivered = True
                            if not ok:
                                fully_delivered = False
                            caption = None
                else:
                    item = batch[0]
                    body_placed, msg_id, ok = await self._send_media_item(
                        item, body_placed, ctx,
                        caption_override=caption,
                        remember=not reply_mapped,
                    )
                    if msg_id and not reply_mapped:
                        first_msg_id = msg_id
                        reply_mapped = True
                    if msg_id:
                        delivered = True
                    if not ok:
                        fully_delivered = False
                    body_placed = True

        for item in other_media:
            cap = pending_caption
            pending_caption = None
            body_placed, msg_id, ok = await self._send_media_item(
                item, body_placed, ctx,
                caption_override=cap,
                remember=not reply_mapped,
            )
            if msg_id and not reply_mapped:
                first_msg_id = msg_id
                reply_mapped = True
            if msg_id:
                delivered = True
            if not ok:
                fully_delivered = False

        for item in to_resolve:
            cap = pending_caption
            pending_caption = None
            body_placed, msg_id, ok = await self._send_resolved_item(
                item, body_placed, ctx,
                caption_override=cap,
                remember=not reply_mapped,
            )
            if msg_id and not reply_mapped:
                first_msg_id = msg_id
                reply_mapped = True
            if msg_id:
                delivered = True
            if not ok:
                fully_delivered = False

        if pending_overflow:
            overflow_id = await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, pending_overflow,
                message_thread_id=thread_id,
            )
            if overflow_id:
                delivered = True
                if not reply_mapped:
                    first_msg_id = overflow_id
                    self._remember(
                        overflow_id, chat_id, max_message_id, sender,
                        telegram_chat_id, thread_id,
                    )
                    reply_mapped = True
            else:
                fully_delivered = False

        if not delivered and not has_content and not media and not to_resolve:
            _logger.warning(
                "Skipping empty forward to Telegram for MAX message id=%s "
                "chat_id=%s",
                max_message_id,
                chat_id,
            )
        elif has_content and can_caption_media and not body_placed:
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, body,
                message_thread_id=thread_id,
            )
            if msg_id:
                first_msg_id = msg_id
                self._remember(
                    msg_id, chat_id, max_message_id, sender,
                    telegram_chat_id, thread_id,
                )
                delivered = True
            else:
                fully_delivered = False

        return delivered, first_msg_id, fully_delivered

    async def _forward(self, client, header, text, parsed,
                       chat_id, max_message_id, sender, chat_title, chat_type,
                       *, attribution: str | None = None) -> tuple[bool, bool]:
        """Returns (delivered, fully_delivered)."""
        async with self._topic_lock(chat_id):
            telegram_chat_id, thread_id, in_topic = await self._telegram_target(
                chat_id, chat_title, chat_type, sender, _lock_held=True,
            )
            topic = self._state.get_topic(chat_id) if in_topic else None
            is_channel = (chat_type == "channel"
                          or bool(topic and topic.get("chat_type") == "channel"))

            delivered, _first_msg_id, fully_delivered = await self._deliver_to_telegram(
                client,
                header,
                text,
                parsed,
                chat_id,
                max_message_id,
                sender,
                telegram_chat_id,
                thread_id,
                in_topic=in_topic,
                is_channel=is_channel,
                attribution=attribution,
            )
            return delivered, fully_delivered

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

    def _media_item_caption(
        self,
        item,
        body_placed: bool,
        ctx,
        *,
        caption_override: str | None,
    ) -> str | None:
        (_client, header, _chat_id, _max_message_id, sender, _telegram_chat_id,
         _thread_id, in_topic, is_channel) = ctx
        if caption_override is not None:
            return caption_override
        if body_placed:
            return item.text or None
        if in_topic:
            return self._topic_caption(sender, item.text, is_channel)
        return self._caption(header, body_placed, item.text)

    async def _send_media_item(
        self,
        item,
        body_placed: bool,
        ctx,
        *,
        caption_override: str | None = None,
        remember: bool = True,
    ) -> tuple[bool, int | None, bool]:
        """Returns (body_placed, telegram_msg_id, ok). ``ok`` is False when the
        real media could not be sent and only a failure note was posted, so the
        caller can avoid marking the MAX message fully delivered."""
        (_client, header, chat_id, max_message_id, sender, telegram_chat_id,
         thread_id, in_topic, is_channel) = ctx
        caption = self._media_item_caption(
            item, body_placed, ctx, caption_override=caption_override,
        )
        sender_fn, supports_caption = _MEDIA_SENDERS[item.kind]
        msg_id = None
        ok = True
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
            note = f"{caption} [не удалось переслать медиа]" if caption else "[не удалось переслать медиа]"
            msg_id = await self._send_note(telegram_chat_id, note, thread_id)
            ok = False
        if remember and msg_id:
            self._remember(msg_id, chat_id, max_message_id, sender,
                           telegram_chat_id, thread_id)
        return True, msg_id, ok

    async def _send_resolved_item(
        self,
        item,
        body_placed: bool,
        ctx,
        *,
        caption_override: str | None = None,
        remember: bool = True,
    ) -> tuple[bool, int | None, bool]:
        """Resolve a file/video to a temporary URL, then upload it to Telegram.

        Returns (body_placed, telegram_msg_id, ok). An oversize file is treated
        as ok (a retry would not help); only a resolve/send exception sets ok to
        False so the caller can retry the MAX message on the next run."""
        (client, header, chat_id, max_message_id, sender, telegram_chat_id,
         thread_id, in_topic, is_channel) = ctx
        caption = self._media_item_caption(
            item, body_placed, ctx, caption_override=caption_override,
        )
        msg_id = None
        if item.size and item.size > TELEGRAM_UPLOAD_LIMIT:
            note = (
                f"{caption} [слишком большой для Telegram] — открыть в MAX"
                if caption
                else "[слишком большой для Telegram] — открыть в MAX"
            )
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, note,
                message_thread_id=thread_id)
            if remember and msg_id:
                self._remember(msg_id, chat_id, max_message_id, sender,
                               telegram_chat_id, thread_id)
            return True, msg_id, True
        ok = True
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
            note = f"{caption} — открыть в MAX" if caption else "открыть в MAX"
            msg_id = await self._send_note(
                telegram_chat_id, note, thread_id)
            ok = False
        if remember and msg_id:
            self._remember(msg_id, chat_id, max_message_id, sender,
                           telegram_chat_id, thread_id)
        return True, msg_id, ok

    # --- MAX -> Telegram (typed PyMax handlers) ------------------------------
    #
    # These consume PyMax's typed Message / MessageDeleteEvent, registered on the
    # client via on_message / on_message_edit / on_message_delete, and reuse the
    # shared _forward / topic machinery.

    async def _handle_incoming_message(self, message, client, *,
                                       edited: bool, _retry: bool = False) -> None:
        # Drop events from a torn-down/replaced session.
        if client is not self._client:
            return
        if self._forward_sem is None:
            self._forward_sem = asyncio.Semaphore(MEDIA_CONCURRENCY)
        async with self._forward_sem:
            chat_id = None
            retry_ctx = None
            try:
                sender_id = getattr(message, "sender", None)
                chat_id = getattr(message, "chat_id", None)
                max_message_id = getattr(message, "id", None)
                if chat_id is not None and self._is_excluded_chat(chat_id):
                    return
                if not edited and chat_id is not None and max_message_id is not None:
                    if self._is_delivered(chat_id, max_message_id):
                        return

                sender_hint = await self._message_sender_name(client, sender_id)
                chat_title, chat_type = await self._chat_meta(
                    client, chat_id, sender_hint)
                resolved = await message_content.resolve_message_content(
                    message,
                    client,
                    chat_type=chat_type,
                    chat_title=chat_title,
                    own_id=self._own_id,
                    resolve_sender_name=lambda uid: self._resolve_sender_name(
                        client, uid),
                )
                text = resolved.text
                if self._is_locale_system_text(text):
                    return
                if edited:
                    text = (f"✏️ (изменено) {text}" if text
                            else "✏️ (сообщение изменено)")
                parsed = attaches.parse(message_content.content_message(resolved))
                sender = resolved.author
                header = f"MAX | {sender} (чат {chat_id})"
                # Captured so a stale-topic ("thread not found") failure can
                # recreate the topic and retry without re-resolving everything.
                retry_ctx = (header, text, parsed, sender, chat_title,
                             chat_type, resolved.attribution)
                delivered, fully_delivered = await self._forward(
                    client, header, text, parsed, chat_id,
                    max_message_id, sender, chat_title, chat_type,
                    attribution=resolved.attribution,
                )
                if delivered and fully_delivered:
                    self._mark_delivered(chat_id, max_message_id)
                    _logger.info("Forwarded from %s (chat %s, %d attach)",
                                 sender, chat_id, len(parsed))
                elif delivered:
                    # Some media/text failed (only a placeholder note went out);
                    # do not mark delivered so it is retried on the next run.
                    _logger.warning(
                        "Partial forward from %s (chat %s): not marking "
                        "delivered so it retries after restart.", sender, chat_id)
            except Exception as exc:
                if "thread not found" in str(exc).lower() and chat_id is not None:
                    self._state.delete_topic(chat_id)
                    if _retry or retry_ctx is None:
                        _logger.warning("Dropped stale topic for chat %s "
                                        "(Telegram thread deleted).", chat_id)
                    else:
                        _logger.warning("Dropped stale topic for chat %s "
                                        "(thread deleted); recreating and "
                                        "retrying.", chat_id)
                        await self._retry_forward_after_stale_topic(
                            client, chat_id, max_message_id, retry_ctx)
                else:
                    _logger.exception("Failed to handle MAX message id=%s",
                                      getattr(message, "id", None))

    async def _retry_forward_after_stale_topic(
        self, client, chat_id, max_message_id, retry_ctx) -> None:
        """Re-drive a forward once after a stale topic was dropped, so the
        triggering message lands in the freshly recreated topic instead of
        being lost."""
        header, text, parsed, sender, chat_title, chat_type, attribution = retry_ctx
        try:
            delivered, fully_delivered = await self._forward(
                client, header, text, parsed, chat_id,
                max_message_id, sender, chat_title, chat_type,
                attribution=attribution,
            )
            if delivered and fully_delivered:
                self._mark_delivered(chat_id, max_message_id)
                _logger.info("Re-forwarded from %s (chat %s) into recreated "
                             "topic.", sender, chat_id)
        except Exception:
            _logger.exception("Retry after stale topic failed for chat %s",
                              chat_id)

    async def _on_message(self, message, client) -> None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is not None and message_id is not None:
            key = (str(chat_id), str(message_id))
            self._set_content_fingerprint(
                key, message_content.message_content_fingerprint(message))
        await self._handle_incoming_message(message, client, edited=False)

    async def _on_message_edit(self, message, client) -> None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is not None and message_id is not None:
            key = (str(chat_id), str(message_id))
            fp = message_content.message_content_fingerprint(message)
            if self._content_fingerprints.get(key) == fp:
                return
            self._set_content_fingerprint(key, fp)
        await self._handle_incoming_message(message, client, edited=True)

    async def _on_message_delete(self, event, client) -> None:
        if client is not self._client:
            return
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        try:
            topic = self._state.get_topic(chat_id)
            if topic and topic.get("telegram_thread_id"):
                telegram_chat_id, thread_id = (
                    self._forum_chat_id, topic["telegram_thread_id"])
            else:
                telegram_chat_id, thread_id = self._fallback_chat_id, None
            count = len(getattr(event, "message_ids", None) or []) or 1
            note = (f"🗑 Удалено сообщений в MAX: {count}" if count > 1
                    else "🗑 Сообщение удалено в MAX")
            await asyncio.to_thread(
                tg.send_message, self._token, telegram_chat_id, note,
                message_thread_id=thread_id)
        except Exception:
            _logger.exception("Failed to handle MAX delete event")

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
            # PyMax unifies send/reply: reply_to=None is a plain send.
            sent = await self._client.send_message(chat_id, text, reply_to=message_id)
            self._mark_max_outbound(chat_id, sent)
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
                sent = await mediamax.send_uploaded_media(
                    self._client,
                    target["chat_id"],
                    content,
                    attachment["filename"],
                    attachment["mime_type"],
                    kind=attachment.get("kind", "file"),
                    text=caption,
                    reply_to_message_id=target.get("message_id"),
                )
                self._mark_max_outbound(target["chat_id"], sent)
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

    async def _register_commands(self) -> None:
        """Publish the command list to Telegram's "/" menu (once, best-effort)."""
        try:
            await asyncio.to_thread(tg.set_my_commands, self._token, _BOT_COMMANDS)
        except Exception as exc:
            _logger.warning("Could not register bot commands: %s", exc)

    @staticmethod
    def _smart_action(text: str) -> str | None:
        """Turn a bare pasted message into a command, so the user can just send a
        link / phone / @username instead of typing /join or /find. Returns the
        synthetic command string, or None if nothing actionable matches."""
        t = (text or "").strip()
        if not t:
            return None
        link = _SMART_MAX_LINK.search(t)
        if link:
            return f"/join {link.group(0).rstrip('.,);')}"
        if _SMART_USERNAME.fullmatch(t):
            return f"/find {t}"
        if _SMART_PHONE.fullmatch(t):
            return f"/find {t}"
        return None

    async def _handle_command(self, incoming_chat, thread_id, text: str) -> None:
        """Owner-only slash commands that drive MAX (join/find/dm). Caller already
        verified the message came from an allowed chat."""
        parts = text.strip().split(maxsplit=2)
        cmd = parts[0].lower().lstrip("/").split("@", 1)[0]  # tolerate /cmd@botname

        async def reply(msg: str):
            try:
                return await asyncio.to_thread(
                    tg.send_message, self._token, incoming_chat, msg,
                    message_thread_id=thread_id)
            except Exception as exc:
                _logger.error("Could not send command reply: %s", exc)
                return None

        if cmd == "start":
            await reply(_WELCOME_TEXT)
            return
        if cmd == "help":
            await reply(_HELP_TEXT)
            return
        if cmd not in ("join", "find", "dm"):
            return  # ignore unknown commands silently (could be Telegram's own)
        client = self._client
        if client is None:
            await reply("⏳ MAX ещё подключается — попробуйте через минуту.")
            return
        if cmd == "join":
            if len(parts) < 2:
                await reply("Использование: /join <ссылка max.ru/… или @username>")
                return
            result = await maxactions.join(client, parts[1])
        elif cmd == "find":
            if len(parts) < 2:
                await reply("Использование: /find <+телефон | @ник | id | ссылка>")
                return
            result = await maxactions.find(client, " ".join(parts[1:]))
        else:  # dm
            if len(parts) < 3:
                await reply("Использование: /dm <id> <текст>")
                return
            result = await maxactions.start_dm(client, parts[1], parts[2])

        await reply(result.text)
        if result.outbound_chat_id is not None and result.outbound_message_id is not None:
            self._mark_delivered(result.outbound_chat_id, result.outbound_message_id)

    def _owner_user_ids(self) -> set[str]:
        """Telegram user ids allowed to drive MAX via commands. A user's private
        chat id equals their user id, so the admin/fallback chat ids identify the
        owner. Group/negative ids are ignored (they are not user ids)."""
        ids: set[str] = set()
        for cid in (self._chat_id, self._fallback_chat_id):
            try:
                if cid is not None and int(cid) > 0:
                    ids.add(str(int(cid)))
            except (TypeError, ValueError):
                continue
        return ids

    def _is_authorized_commander(self, message: dict, incoming_chat) -> bool:
        """Whether this message may run /commands (join/find/dm). DMs to the bot
        are inherently owner-only; the forum is a multi-member supergroup, so
        there we require the sender's user id to match the configured owner."""
        if str(incoming_chat) != str(self._forum_chat_id):
            return True
        owner_ids = self._owner_user_ids()
        if not owner_ids:
            # Cannot determine the owner user id (e.g. admin chat is a group):
            # preserve legacy behavior rather than locking everyone out.
            return True
        sender = (message.get("from") or {}).get("id")
        return str(sender) in owner_ids

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
            if not self._is_authorized_commander(message, incoming_chat):
                _logger.warning("Ignoring /command from non-owner in forum chat.")
                return
            await self._handle_command(
                incoming_chat, message.get("message_thread_id"), text)
            return
        # Act on real content, not on the display note: an attachment with no
        # caption (and no media-note label) must still be routed to MAX.
        if not text and not self._telegram_attachment(message):
            return
        reply = message.get("reply_to_message")
        target = self._reply_map.get(reply.get("message_id")) if reply else None
        if target:
            await self._send_telegram_update_to_max(target, message)
            return
        thread_id = message.get("message_thread_id")
        in_forum = self._forum_chat_id and str(incoming_chat) == str(self._forum_chat_id)
        if in_forum and thread_id:
            topic = self._state.find_by_thread(thread_id)
            if topic:
                await self._send_telegram_update_to_max({
                    "chat_id": topic["max_chat_id"],
                    "message_id": None,
                    "sender": topic.get("title") or "чат",
                    "telegram_chat_id": self._forum_chat_id,
                    "message_thread_id": thread_id,
                }, message)
                return
        # A loose message (not a reply, not inside a chat topic): a bare link /
        # @username / phone acts like the matching command — no /join needed.
        action = self._smart_action(text)
        if action:
            if not self._is_authorized_commander(message, incoming_chat):
                _logger.warning("Ignoring smart-action from non-owner in forum chat.")
                return
            await self._handle_command(incoming_chat, thread_id, action)
            return
        await asyncio.to_thread(
            tg.send_message, self._token, incoming_chat,
            "ℹ️ Написать в чат MAX — Reply (свайп) на пересланном сообщении.\n"
            "Вступить в чат/канал — просто пришлите ссылку.",
            message_thread_id=thread_id)

    async def _poll_telegram(self, start_offset: int | None = None) -> None:
        """Long-poll Telegram for replies; skip the backlog on startup.

        ``start_offset`` continues from where the interactive SMS auth left the
        shared getUpdates cursor, so replies typed during auth aren't re-read.
        When None (token/qr), the backlog is drained instead.
        """
        offset = start_offset
        if offset is None:
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

    def _auth_method(self) -> str:
        return (self._config.get("max_auth_method") or "token").strip().lower()

    def _start_telegram_poll(self) -> None:
        """Start the Telegram long-poll task once, continuing from the SMS-auth
        cursor if one was used. Safe to call repeatedly (e.g. each reconnect)."""
        if self._tg_poll_task is not None and not self._tg_poll_task.done():
            return
        start_offset = self._auth_poll.offset if self._auth_poll else None
        self._tg_poll_task = asyncio.create_task(self._poll_telegram(start_offset))

    async def _on_start(self, client) -> None:
        """PyMax fires this after a successful (re)connect + login."""
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        self._own_id = getattr(contact, "id", None)
        self._hydrate_delivered_cache()
        _logger.info("Bridge online (own id: %s).", self._own_id)
        try:
            await self._preload_topics(client)
        except Exception:
            _logger.exception("Topic preload failed")
        # SMS/QR auth can block start() waiting for Telegram replies (SMS code,
        # 2FA password), so only now is it safe to let the main poll loop consume
        # updates without racing the auth provider for the same getUpdates cursor.
        self._start_telegram_poll()
        if self._topics_enabled:
            print("Мост запущен. Сообщения MAX идут в темы Telegram; ответы — в теме или через Reply.")
        else:
            _logger.warning(
                "Topics disabled — bridge is in legacy single-chat mode "
                "(all MAX chats → chat %s). Configure telegram_forum_chat_id "
                "and telegram_topics_enabled for production use.",
                self._fallback_chat_id,
            )
            print("Мост запущен (устаревший режим без тем). Включите режим тем в config.")

    async def _run_session(self) -> None:
        client = build_max_client(
            self._config,
            bot_token=self._token,
            admin_chat_id=self._chat_id,
            poll=self._auth_poll,
        )
        client.on_message()(self._on_message)
        client.on_message_edit()(self._on_message_edit)
        client.on_message_delete()(self._on_message_delete)
        client.on_start()(self._on_start)
        self._client = client
        try:
            # start() connects, authenticates, and listens until the connection
            # closes; with reconnect=True it transparently reconnects internally.
            await client.start()
            _logger.warning("MAX client stopped.")
        finally:
            self._client = None
            try:
                await client.stop()
            except Exception:
                pass

    async def _max_loop(self) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            started = loop.time()
            try:
                await self._run_session()
            except Exception as exc:
                _logger.error("MAX session error: %s", exc)
            # A session that stayed up a while resets the backoff; rapid repeated
            # failures (e.g. a revoked token) escalate the delay and warn the user.
            if loop.time() - started > 120:
                failures = 0
            failures += 1
            delay = min(RECONNECT_DELAY_SECONDS * (2 ** (failures - 1)),
                        RECONNECT_MAX_DELAY)
            if failures == 5:
                _logger.error("MAX keeps failing to start %d times in a row — the "
                              "token/credentials are likely invalid.", failures)
                print("⚠️ MAX не удаётся подключиться — проверьте токен/доступ "
                      "и перезапустите.")
            _logger.info("Reconnecting in %s seconds...", delay)
            await asyncio.sleep(delay)

    async def _drain_telegram_backlog(self) -> int | None:
        """Return an offset just past the current Telegram backlog, so the SMS
        auth provider reads only messages sent *after* the code prompt (a stale
        /start or old reply must not be mistaken for the code)."""
        try:
            backlog = await asyncio.to_thread(tg.get_updates, self._token, None, 0)
            if backlog:
                return backlog[-1]["update_id"] + 1
        except Exception as exc:
            _logger.warning("Telegram backlog drain (auth) failed: %s", exc)
        return None

    async def run_forever(self) -> None:
        await self._register_commands()
        # SMS/QR login may need interactive replies from Telegram (SMS code or
        # 2FA password after QR scan), so the poll cursor is shared with the auth
        # provider and the main poll loop only starts once login finishes (from
        # _on_start). Token auth is non-interactive, so its poll can start now.
        if self._auth_method() in ("sms", "qr"):
            start_offset = await self._drain_telegram_backlog()
            self._auth_poll = TelegramAuthPoll(
                self._token, self._chat_id, start_offset=start_offset)
        else:
            self._start_telegram_poll()
        await self._max_loop()
