"""Unified MAX ↔ Telegram edit and reaction mirroring via MessageLinkRegistry."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Awaitable

import tg
from formatting import FormattedText, build_delivery_formatted, telegram_message_markdown
from message_content import content_message, message_content_fingerprint
from message_links import MessageLink, MessageLinkRegistry
from pymax.protocol import Opcode
from reaction_emoji import normalize_max_reaction_for_telegram

_logger = logging.getLogger(__name__)

MIRROR_EDIT_MARKER = " · ред."
REACTION_AFTER_EDIT_GUARD_SECONDS = 2.0
CONTENT_FP_LIMIT = 10000
HEAD_ROLES = frozenset({"text", "caption"})
REACTION_EVENT_OPCODES = frozenset({
    Opcode.NOTIF_MSG_REACTIONS_CHANGED,
    Opcode.NOTIF_MSG_YOU_REACTED,
})
DEFAULT_POLL_DELAYS = (2, 15, 60, 300)
STABLE_POLLS_TO_STOP = 2


class MaxEventKind(Enum):
    NEW = auto()
    STATS_ONLY = auto()
    REACTION_ONLY = auto()
    CONTENT_EDIT = auto()


@dataclass
class SyncConfig:
    mirror_edit_marker: bool = True
    sync_debug: bool = False
    poll_delays: tuple[int, ...] = DEFAULT_POLL_DELAYS
    watch_ttl: int = 86400
    coalesce_seconds: float = 0.5
    events_log: Path | None = None
    events_log_max_bytes: int = 5 * 1024 * 1024
    events_frame_max_chars: int = 20000


@dataclass
class EditResolveResult:
    resolved: Any
    chat_title: str
    chat_type: str


@dataclass
class MaxToTgEditDeps:
    """Bridge services required to render and apply MAX→TG text/caption edits."""

    token: str
    links: MessageLinkRegistry
    mirror_edit_marker: bool
    split_caption: Callable[[FormattedText], tuple[FormattedText, FormattedText | None]]
    entry_thread_id: Callable[[dict, int | None], int | None]
    topic_lock: Callable[[Any], Any]
    telegram_target: Callable[..., Awaitable[tuple[Any, int | None, bool]]]
    resolve_for_edit: Callable[..., Awaitable[EditResolveResult]]
    is_locale_system_text: Callable[[str | None], bool]
    reply_parameters_for_max: Callable[..., dict | None]
    is_channel_chat: Callable[[Any, str, bool], bool]
    media_senders: frozenset[str]
    attaches_parse: Callable[[Any], list]


@dataclass
class _WatchEntry:
    max_chat_id: str
    max_message_id: str
    created_at: float
    stable_count: int = 0
    last_fetched: str | None = None
    pending_tasks: list[asyncio.Task] = field(default_factory=list)


def reaction_from_counters(counters, total_count: int) -> str | None:
    total = int(total_count or 0)
    if total <= 0 or not counters:
        return None
    best_emoji: str | None = None
    best_count = -1
    for counter in counters or []:
        if isinstance(counter, dict):
            emoji = counter.get("reaction")
            count = int(counter.get("count") or 0)
        else:
            emoji = getattr(counter, "reaction", None)
            count = int(getattr(counter, "count", 0) or 0)
        if emoji and count > best_count:
            best_count = count
            best_emoji = emoji
    return best_emoji


def parse_reaction_frame_payload(payload) -> tuple:
    if not isinstance(payload, dict):
        return None, None, None, 0, "invalid"
    info = payload.get("reactionInfo")
    if isinstance(info, dict):
        chat_id = payload.get("chatId", info.get("chatId"))
        message_id = payload.get("messageId", info.get("messageId"))
        counters = info.get("counters") or payload.get("counters") or []
        total_count = info.get("totalCount", payload.get("totalCount", 0))
        source = "nested"
    else:
        chat_id = payload.get("chatId")
        message_id = payload.get("messageId")
        counters = payload.get("counters") or []
        total_count = payload.get("totalCount", 0)
        source = "flat"
    return chat_id, message_id, counters, total_count, source


def top_tg_reaction_emoji(reactions: list[dict]) -> str | None:
    best_emoji: str | None = None
    best_count = 0
    for item in reactions:
        if item.get("type") != "emoji":
            continue
        emoji = item.get("emoji")
        count = int(item.get("total_count") or 0)
        if emoji and count > best_count:
            best_count = count
            best_emoji = emoji
    return best_emoji


class MaxEventClassifier:
    def __init__(self) -> None:
        self._fingerprints: OrderedDict[tuple[str, str], str] = OrderedDict()

    def record_new(self, chat_id, message_id, message) -> None:
        key = (str(chat_id), str(message_id))
        self._set_fingerprint(key, message_content_fingerprint(message))

    def classify_edit(self, chat_id, message_id, message) -> MaxEventKind:
        key = (str(chat_id), str(message_id))
        fp = message_content_fingerprint(message)
        prev = self._fingerprints.get(key)
        if prev is None:
            self._set_fingerprint(key, fp)
            return MaxEventKind.CONTENT_EDIT
        if prev == fp:
            if getattr(message, "reaction_info", None) is not None:
                return MaxEventKind.REACTION_ONLY
            if getattr(message, "stats", None):
                return MaxEventKind.STATS_ONLY
            return MaxEventKind.REACTION_ONLY
        self._set_fingerprint(key, fp)
        return MaxEventKind.CONTENT_EDIT

    def discard_fingerprint(self, chat_id, message_id) -> None:
        self._fingerprints.pop((str(chat_id), str(message_id)), None)

    def _set_fingerprint(self, key: tuple[str, str], fp: str) -> None:
        self._fingerprints[key] = fp
        self._fingerprints.move_to_end(key)
        while len(self._fingerprints) > CONTENT_FP_LIMIT:
            self._fingerprints.popitem(last=False)


class MessageSync:
    def __init__(
        self,
        *,
        links: MessageLinkRegistry,
        token: str,
        config: SyncConfig,
        edit_deps: MaxToTgEditDeps,
        get_client: Callable[[], Any],
        bot_id: Callable[[], int | None],
    ) -> None:
        self._links = links
        self._token = token
        self._config = config
        self._edit_deps = edit_deps
        self._get_client = get_client
        self._bot_id = bot_id
        self._classifier = MaxEventClassifier()
        self._last_applied: dict[tuple[str, str], str | None] = {}
        self._edit_guard_until: dict[tuple[str, str], float] = {}
        self._watch: dict[tuple[str, str], _WatchEntry] = {}
        self._chat_poll_tasks: dict[str, asyncio.Task] = {}
        self._on_delivered_discard: Callable[[str, str], None] | None = None

    def set_delivered_discard(self, cb: Callable[[str, str], None]) -> None:
        self._on_delivered_discard = cb

    def on_link_created(self, link: MessageLink) -> None:
        key = (link.max_chat_id, link.max_message_id)
        now = time.time()
        entry = _WatchEntry(
            max_chat_id=link.max_chat_id,
            max_message_id=link.max_message_id,
            created_at=now,
        )
        self._watch[key] = entry
        _logger.info(
            "Sync watch started MAX %s/%s (origin=%s, TG msg %s)",
            link.max_chat_id, link.max_message_id,
            link.origin, link.telegram_message_id,
        )
        for delay in self._config.poll_delays:
            task = asyncio.create_task(
                self._delayed_poll(link.max_chat_id, link.max_message_id, delay),
            )
            entry.pending_tasks.append(task)

    async def on_max_message(self, message, client) -> None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is not None and message_id is not None:
            self._classifier.record_new(chat_id, message_id, message)

    async def on_max_message_edit(self, message, client) -> MaxEventKind | None:
        if client is not self._get_client():
            return None
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return None
        kind = self._classifier.classify_edit(chat_id, message_id, message)
        if kind == MaxEventKind.STATS_ONLY:
            return kind
        if kind == MaxEventKind.REACTION_ONLY:
            await self._apply_from_message_reaction_info(message, client, "message_edit")
            return kind
        if kind == MaxEventKind.CONTENT_EDIT:
            if self._links.is_max_linked(chat_id, message_id):
                await self._mirror_max_to_tg_edit(message, client)
                return kind
            if self._on_delivered_discard:
                self._on_delivered_discard(str(chat_id), str(message_id))
            return kind
        return kind

    async def on_max_delivered_redelivery(
        self, message, client,
    ) -> None:
        """Called when an already-delivered MAX message is seen again."""
        await self._apply_from_message_reaction_info(message, client, "redelivery")

    async def on_max_raw(self, frame, client) -> None:
        if client is not self._get_client():
            return
        if frame.opcode not in REACTION_EVENT_OPCODES:
            return
        self._log_event_frame(frame)
        payload = frame.payload or {}
        chat_id, message_id, counters, total_count, source = (
            parse_reaction_frame_payload(payload)
        )
        if frame.opcode == Opcode.NOTIF_MSG_YOU_REACTED:
            _logger.debug(
                "MAX raw reaction opcode=156 (you_reacted) cmd=%s chat=%s "
                "msg=%s source=%s",
                frame.cmd, chat_id, message_id, source,
            )
            return
        emoji = reaction_from_counters(counters, total_count)
        _logger.info(
            "MAX raw reaction opcode=155 cmd=%s chat=%s msg=%s reaction=%r "
            "source=%s",
            frame.cmd, chat_id, message_id, emoji, source,
        )
        if chat_id is None or message_id is None:
            _logger.info(
                "Reaction mirror skipped (missing_ids): opcode=155 payload=%s",
                payload,
            )
            return
        await self.apply_max_to_tg(chat_id, message_id, emoji, client, source="raw_155")
        await self._poll_chat_watch(str(chat_id))

    async def on_tg_edited_message(self, message: dict) -> None:
        target = self._links.max_target_for_tg(message.get("message_id"))
        if not target:
            return
        client = self._get_client()
        if client is None:
            return
        text = telegram_message_markdown(message)
        if not text:
            return
        max_chat_id = target["max_chat_id"]
        max_message_id = target["max_message_id"]
        try:
            await client.edit_message(
                int(max_chat_id), int(max_message_id), text,
            )
        except Exception as exc:
            _logger.warning(
                "Could not mirror Telegram edit to MAX chat %s msg %s: %s",
                max_chat_id, max_message_id, exc,
            )

    async def on_tg_reaction(self, reaction_update: dict) -> None:
        user = reaction_update.get("user") or {}
        bot_id = self._bot_id()
        if bot_id and user.get("id") == bot_id:
            self._debug_skip("tg_reaction_ignored_bot_user")
            return
        tg_msg_id = reaction_update.get("message_id")
        target = self._links.max_target_for_tg(tg_msg_id)
        if not target:
            self._debug_skip("tg_reaction_no_max_mapping", tg_message_id=tg_msg_id)
            return
        new_reactions = reaction_update.get("new_reaction") or []
        emoji = None
        if new_reactions:
            emoji = new_reactions[0].get("emoji")
        await self._apply_tg_to_max(target, emoji)

    async def on_tg_reaction_count(self, reaction_update: dict) -> None:
        tg_msg_id = reaction_update.get("message_id")
        target = self._links.max_target_for_tg(tg_msg_id)
        if not target:
            self._debug_skip(
                "tg_reaction_count_no_max_mapping", tg_message_id=tg_msg_id,
            )
            return
        reactions = reaction_update.get("reactions") or []
        emoji = top_tg_reaction_emoji(reactions)
        await self._apply_tg_to_max(target, emoji)

    async def apply_max_to_tg(
        self, chat_id, message_id, emoji: str | None, client, *, source: str,
    ) -> None:
        if client is not self._get_client():
            self._debug_skip("stale_client")
            return
        key = (str(chat_id), str(message_id))
        entries = self._links.tg_targets_for_max(chat_id, message_id)
        if not entries:
            _logger.info(
                "Reaction mirror skipped (no_message_link): MAX %s/%s source=%s",
                chat_id, message_id, source,
            )
            return
        guard_until = self._edit_guard_until.get(key)
        if guard_until is not None:
            now = asyncio.get_running_loop().time()
            if now < guard_until:
                self._debug_skip("edit_guard_active", max_message_id=message_id)
                return
            self._edit_guard_until.pop(key, None)
        head = MessageLinkRegistry.head_tg_target(entries)
        if head is None:
            _logger.info(
                "Reaction mirror skipped (no_head_target): MAX %s/%s",
                chat_id, message_id,
            )
            return
        if self._last_applied.get(key) == emoji:
            _logger.info(
                "Reaction mirror skipped (duplicate): MAX %s/%s reaction=%r",
                chat_id, message_id, emoji,
            )
            return
        tg_emoji = normalize_max_reaction_for_telegram(emoji)
        if emoji is not None and tg_emoji is None:
            _logger.info(
                "Reaction mirror skipped (telegram_unsupported): MAX %s/%s "
                "reaction=%r",
                chat_id, message_id, emoji,
            )
            self._last_applied[key] = emoji
            self._stop_watch(key)
            return
        if tg_emoji is not None and tg_emoji != emoji:
            _logger.info(
                "Mapped MAX reaction %r → %r for Telegram (MAX %s/%s)",
                emoji, tg_emoji, chat_id, message_id,
            )
        try:
            await asyncio.to_thread(
                tg.set_message_reaction,
                self._token,
                head["telegram_chat_id"],
                head["message_id"],
                tg_emoji,
                message_thread_id=head.get("message_thread_id"),
            )
            self._last_applied[key] = emoji
            _logger.info(
                "Mirrored MAX reaction %r to Telegram msg %s "
                "(MAX %s/%s, source=%s%s)",
                emoji, head["message_id"], chat_id, message_id, source,
                f", tg={tg_emoji!r}" if tg_emoji != emoji else "",
            )
            if emoji is not None:
                self._stop_watch(key)
        except Exception as exc:
            if emoji is not None and "REACTION_INVALID" in str(exc):
                _logger.info(
                    "Reaction %r not allowed on Telegram for msg %s "
                    "(MAX %s/%s); stopping watch",
                    emoji, head["message_id"], chat_id, message_id,
                )
                self._last_applied[key] = emoji
                self._stop_watch(key)
                return
            _logger.warning(
                "Failed to mirror MAX reaction %r to Telegram msg %s "
                "(MAX %s/%s): %s",
                emoji, head["message_id"], chat_id, message_id, exc,
            )

    async def _apply_from_message_reaction_info(
        self, message, client, source: str,
    ) -> None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return
        reaction_info = getattr(message, "reaction_info", None)
        if reaction_info is None:
            if self._links.is_max_linked(chat_id, message_id):
                self._schedule_chat_poll(str(chat_id))
            return
        emoji = reaction_from_counters(
            getattr(reaction_info, "counters", None) or [],
            getattr(reaction_info, "total_count", 0),
        )
        _logger.info(
            "MAX reaction signal (%s) chat=%s msg=%s reaction=%r",
            source, chat_id, message_id, emoji,
        )
        await self.apply_max_to_tg(
            chat_id, message_id, emoji, client, source=source,
        )

    async def _apply_tg_to_max(self, target: dict, emoji: str | None) -> None:
        client = self._get_client()
        if client is None:
            return
        max_chat_id = int(target["max_chat_id"])
        max_message_id = str(target["max_message_id"])
        try:
            if emoji:
                await client.add_reaction(max_chat_id, max_message_id, emoji)
            else:
                await client.remove_reaction(max_chat_id, max_message_id)
            _logger.info(
                "Mirrored Telegram reaction %r to MAX chat %s msg %s",
                emoji, max_chat_id, max_message_id,
            )
        except Exception as exc:
            _logger.warning(
                "Could not mirror Telegram reaction to MAX chat %s msg %s: %s",
                max_chat_id, max_message_id, exc,
            )

    async def _mirror_max_to_tg_edit(self, message, client) -> None:
        deps = self._edit_deps
        chat_id = getattr(message, "chat_id", None)
        max_message_id = getattr(message, "id", None)
        if chat_id is None or max_message_id is None:
            return
        key = (str(chat_id), str(max_message_id))
        entries = deps.links.tg_targets_for_max(chat_id, max_message_id)
        if not entries:
            return
        try:
            ctx = await deps.resolve_for_edit(message, client)
            resolved = ctx.resolved
            if deps.is_locale_system_text(resolved.text):
                return
            parsed = deps.attaches_parse(content_message(resolved))
            resolvable = {"file_resolve", "video_resolve"}
            notes = [
                p.text for p in parsed
                if p.kind not in deps.media_senders and p.kind not in resolvable
            ]
            sender = resolved.author
            header = f"MAX | {sender} (чат {chat_id})"
            async with deps.topic_lock(chat_id):
                telegram_chat_id, thread_id, in_topic = await deps.telegram_target(
                    chat_id, ctx.chat_title, ctx.chat_type,
                    sender, _lock_held=True,
                )
                reply_parameters = deps.reply_parameters_for_max(
                    chat_id,
                    resolved.reply_parent_max_id,
                    resolved.reply_quote,
                )
                text_reply_quote = (
                    None if reply_parameters and resolved.reply_quote
                    else resolved.reply_quote
                )
                body_fmt = build_delivery_formatted(
                    FormattedText.from_max(resolved.text, resolved.elements),
                    notes,
                    in_topic=in_topic,
                    sender=sender,
                    is_channel=deps.is_channel_chat(
                        chat_id, ctx.chat_type, in_topic,
                    ),
                    attribution=resolved.attribution,
                    header=header,
                    reply_quote=text_reply_quote,
                )
                if deps.mirror_edit_marker:
                    body_fmt = body_fmt.append_plain_suffix(MIRROR_EDIT_MARKER)
            caption_entries = [e for e in entries if e["role"] == "caption"]
            text_entries = [e for e in entries if e["role"] == "text"]
            edited_any = False
            if caption_entries:
                cap_fmt, overflow_fmt = deps.split_caption(body_fmt)
                for entry in caption_entries:
                    try:
                        result = await asyncio.to_thread(
                            tg.edit_message_caption,
                            deps.token,
                            entry["telegram_chat_id"],
                            entry["message_id"],
                            cap_fmt.text,
                            message_thread_id=deps.entry_thread_id(
                                entry, thread_id,
                            ),
                            caption_entities=cap_fmt.entities or None,
                        )
                        self._log_edit_result("caption", entry["message_id"], result)
                        if result:
                            edited_any = True
                    except Exception as exc:
                        _logger.warning(
                            "Failed to mirror MAX edit to Telegram caption "
                            "msg %s: %s", entry["message_id"], exc,
                        )
                overflow = (
                    overflow_fmt if overflow_fmt and overflow_fmt.text else None
                )
                for entry in text_entries:
                    fmt = overflow or body_fmt
                    try:
                        result = await asyncio.to_thread(
                            tg.edit_message_text,
                            deps.token,
                            entry["telegram_chat_id"],
                            entry["message_id"],
                            fmt.text,
                            message_thread_id=deps.entry_thread_id(
                                entry, thread_id,
                            ),
                            entities=fmt.entities or None,
                        )
                        self._log_edit_result("text", entry["message_id"], result)
                        if result:
                            edited_any = True
                    except Exception as exc:
                        _logger.warning(
                            "Failed to mirror MAX edit to Telegram text "
                            "msg %s: %s", entry["message_id"], exc,
                        )
            elif text_entries:
                for entry in text_entries:
                    try:
                        result = await asyncio.to_thread(
                            tg.edit_message_text,
                            deps.token,
                            entry["telegram_chat_id"],
                            entry["message_id"],
                            body_fmt.text,
                            message_thread_id=deps.entry_thread_id(
                                entry, thread_id,
                            ),
                            entities=body_fmt.entities or None,
                        )
                        self._log_edit_result("text", entry["message_id"], result)
                        if result:
                            edited_any = True
                    except Exception as exc:
                        _logger.warning(
                            "Failed to mirror MAX edit to Telegram text "
                            "msg %s: %s", entry["message_id"], exc,
                        )
            if edited_any:
                loop = asyncio.get_running_loop()
                self._edit_guard_until[key] = (
                    loop.time() + REACTION_AFTER_EDIT_GUARD_SECONDS
                )
        except Exception:
            _logger.exception(
                "Failed to mirror MAX edit for chat %s msg %s",
                chat_id, max_message_id,
            )

    def _log_edit_result(
        self, role: str, tg_message_id: int, result: dict | None,
    ) -> None:
        if not result:
            return
        edit_date = result.get("edit_date")
        _logger.info(
            "Mirrored MAX edit to Telegram %s msg %s (edit_date=%s)",
            role, tg_message_id, edit_date,
        )

    async def _delayed_poll(
        self, max_chat_id: str, max_message_id: str, delay: int,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            key = (max_chat_id, max_message_id)
            if key not in self._watch:
                return
            await self._poll_chat_watch(max_chat_id)
        except asyncio.CancelledError:
            raise

    def _schedule_chat_poll(self, max_chat_id: str) -> None:
        existing = self._chat_poll_tasks.get(max_chat_id)
        if existing is not None and not existing.done():
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(self._config.coalesce_seconds)
                await self._poll_chat_watch(max_chat_id)
            finally:
                self._chat_poll_tasks.pop(max_chat_id, None)

        self._chat_poll_tasks[max_chat_id] = asyncio.create_task(_run())

    async def _poll_chat_watch(self, max_chat_id: str) -> None:
        client = self._get_client()
        if client is None:
            return
        now = time.time()
        keys = [
            k for k, w in self._watch.items()
            if w.max_chat_id == max_chat_id
            and now - w.created_at < self._config.watch_ttl
        ]
        if not keys:
            return
        to_fetch: list[str] = []
        for key in keys:
            entry = self._watch[key]
            if (entry.last_fetched is not None
                    and self._last_applied.get(key) == entry.last_fetched):
                continue
            to_fetch.append(key[1])
        if not to_fetch:
            return
        try:
            reactions = await client.get_reactions(int(max_chat_id), to_fetch)
        except Exception as exc:
            _logger.warning(
                "Reaction watch poll failed for MAX chat %s: %s",
                max_chat_id, exc,
            )
            return
        if not reactions:
            reactions = {}
        for key in keys:
            mid = key[1]
            if mid not in to_fetch:
                continue
            info = reactions.get(mid)
            emoji = None
            if info is not None:
                emoji = reaction_from_counters(
                    getattr(info, "counters", None) or [],
                    getattr(info, "total_count", 0),
                )
            entry = self._watch.get(key)
            if entry is not None:
                entry.last_fetched = emoji
            if self._last_applied.get(key) == emoji:
                if emoji is not None:
                    if entry is not None:
                        entry.stable_count += 1
                        if entry.stable_count >= STABLE_POLLS_TO_STOP:
                            self._stop_watch(key)
                continue
            _logger.info(
                "MAX reaction watch poll chat=%s msg=%s reaction=%r",
                max_chat_id, mid, emoji,
            )
            await self.apply_max_to_tg(
                max_chat_id, mid, emoji, client, source="watch_poll",
            )
            if entry is not None:
                entry.stable_count = 0

    def _stop_watch(self, key: tuple[str, str]) -> None:
        entry = self._watch.pop(key, None)
        if entry is None:
            return
        for task in entry.pending_tasks:
            if not task.done():
                task.cancel()

    def _log_event_frame(self, frame) -> None:
        path = self._config.events_log
        if path is None:
            return
        try:
            if path.exists() and path.stat().st_size > self._config.events_log_max_bytes:
                return
            line = json.dumps(
                {
                    "opcode": frame.opcode,
                    "cmd": frame.cmd,
                    "payload": frame.payload,
                },
                ensure_ascii=False,
                default=str,
            )
            if len(line) > self._config.events_frame_max_chars:
                line = line[: self._config.events_frame_max_chars] + "…"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _debug_skip(self, reason: str, **details) -> None:
        if not self._config.sync_debug:
            return
        if details:
            _logger.debug("Reaction mirror skip (%s): %s", reason, details)
        else:
            _logger.debug("Reaction mirror skip (%s)", reason)
