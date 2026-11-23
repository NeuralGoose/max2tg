"""MAX channel comments ↔ Telegram.

Telegram → MAX: a Telegram reply to a mirrored channel post is sent as a MAX
comment on that post (a reply to a mirrored comment answers that comment).

MAX → Telegram: comment threads are not pushed to us, so threads you take part
in (you commented from Telegram, or asked for them with /comments) are polled
with ``fetch_comments`` for a while, and new comments from other people are
posted in Telegram as replies to the mirrored post.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import attaches
import tg
from message_links import MessageLinkRegistry

_logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60
DEFAULT_WATCH_TTL = 3 * 86400
FETCH_BATCH = 30


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class CommentStore:
    """SQLite tables (in the links DB) for mirrored comments and watched threads."""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS comment_links (
                telegram_message_id INTEGER PRIMARY KEY,
                max_chat_id TEXT NOT NULL,
                post_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_comment_links_max
                ON comment_links (max_chat_id, comment_id);
            CREATE TABLE IF NOT EXISTS comment_watch (
                max_chat_id TEXT NOT NULL,
                post_id TEXT NOT NULL,
                last_comment_id TEXT,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (max_chat_id, post_id)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def link(self, tg_message_id: int, max_chat_id, post_id, comment_id) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO comment_links
                (telegram_message_id, max_chat_id, post_id, comment_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(tg_message_id), str(max_chat_id), str(post_id),
             str(comment_id), int(time.time())),
        )
        self._conn.commit()

    def comment_for_tg(self, tg_message_id) -> dict[str, Any] | None:
        if not tg_message_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM comment_links WHERE telegram_message_id = ?",
            (int(tg_message_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "max_chat_id": row["max_chat_id"],
            "post_id": row["post_id"],
            "comment_id": row["comment_id"],
        }

    def remove_comment(self, max_chat_id, comment_id) -> list[int]:
        """Forget a comment; returns the Telegram message ids it was mirrored to."""
        rows = self._conn.execute(
            "SELECT telegram_message_id FROM comment_links "
            "WHERE max_chat_id = ? AND comment_id = ?",
            (str(max_chat_id), str(comment_id)),
        ).fetchall()
        self._conn.execute(
            "DELETE FROM comment_links WHERE max_chat_id = ? AND comment_id = ?",
            (str(max_chat_id), str(comment_id)),
        )
        self._conn.commit()
        return [int(r["telegram_message_id"]) for r in rows]

    def watch(self, max_chat_id, post_id, ttl: int,
              last_comment_id=None) -> None:
        """Start or extend watching a thread; ``last_comment_id`` only moves forward."""
        key = (str(max_chat_id), str(post_id))
        row = self._conn.execute(
            "SELECT last_comment_id FROM comment_watch "
            "WHERE max_chat_id = ? AND post_id = ?", key,
        ).fetchone()
        last = row["last_comment_id"] if row else None
        if last_comment_id is not None and (
                last is None or _int_or_zero(last_comment_id) > _int_or_zero(last)):
            last = str(last_comment_id)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO comment_watch
                (max_chat_id, post_id, last_comment_id, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (*key, last, int(time.time()) + int(ttl)),
        )
        self._conn.commit()

    def set_last(self, max_chat_id, post_id, last_comment_id) -> None:
        self._conn.execute(
            "UPDATE comment_watch SET last_comment_id = ? "
            "WHERE max_chat_id = ? AND post_id = ?",
            (str(last_comment_id), str(max_chat_id), str(post_id)),
        )
        self._conn.commit()

    def unwatch(self, max_chat_id, post_id) -> None:
        self._conn.execute(
            "DELETE FROM comment_watch WHERE max_chat_id = ? AND post_id = ?",
            (str(max_chat_id), str(post_id)),
        )
        self._conn.commit()

    def active_watches(self) -> list[tuple[str, str, str | None]]:
        now = int(time.time())
        self._conn.execute("DELETE FROM comment_watch WHERE expires_at < ?", (now,))
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT max_chat_id, post_id, last_comment_id FROM comment_watch",
        ).fetchall()
        return [(r["max_chat_id"], r["post_id"], r["last_comment_id"]) for r in rows]


def comment_text(comment, author: str) -> str:
    """Telegram text for a mirrored MAX comment: author, text, attachment labels."""
    parts = [(getattr(comment, "text", None) or "").strip()]
    parts += [p.text for p in attaches.parse(comment) if p.text]
    body = "\n".join(p for p in parts if p)
    return f"💬 {author}:\n{body}" if body else f"💬 {author}"


class CommentMirror:
    def __init__(
        self,
        *,
        store: CommentStore,
        links: MessageLinkRegistry,
        token: str,
        get_client: Callable[[], Any],
        own_id: Callable[[], int | None],
        resolve_name: Callable[[Any, int], Awaitable[str]],
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        watch_ttl: int = DEFAULT_WATCH_TTL,
    ) -> None:
        self._store = store
        self._links = links
        self._token = token
        self._get_client = get_client
        self._own_id = own_id
        self._resolve_name = resolve_name
        self._poll_interval = poll_interval
        self._watch_ttl = watch_ttl
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def store(self) -> CommentStore:
        return self._store

    async def send_from_telegram(
        self, max_chat_id, post_id, text: str, *,
        reply_to_comment=None, attachments: list | None = None,
        tg_message_id: int | None = None,
    ):
        """Publish a comment on a MAX channel post and start watching its thread."""
        client = self._get_client()
        if client is None:
            raise RuntimeError("MAX is not connected")
        sent = await client.send_comment(
            int(max_chat_id), int(post_id), text or "",
            reply_to=int(reply_to_comment) if reply_to_comment else None,
            attachments=attachments,
        )
        self.record_sent(tg_message_id, max_chat_id, post_id, sent)
        return sent

    def record_sent(self, tg_message_id, max_chat_id, post_id, sent) -> None:
        """Link a comment we published to its Telegram source and watch the thread."""
        comment_id = getattr(sent, "id", None)
        if tg_message_id and comment_id is not None:
            self._store.link(tg_message_id, max_chat_id, post_id, comment_id)
        self._store.watch(max_chat_id, post_id, self._watch_ttl,
                          last_comment_id=comment_id)
        _logger.info("Sent Telegram comment to MAX channel %s post %s (comment %s)",
                     max_chat_id, post_id, comment_id)

    async def show_recent(self, max_chat_id, post_id, limit: int = 10) -> int:
        """Mirror the latest ``limit`` comments of a post and watch the thread."""
        client = self._get_client()
        if client is None:
            raise RuntimeError("MAX is not connected")
        comments = await self._fetch(client, max_chat_id, post_id)
        recent = comments[-limit:] if limit else []
        async with self._lock:
            sent = 0
            for comment in recent:
                if await self._mirror(client, max_chat_id, post_id, comment,
                                      include_own=True):
                    sent += 1
            last = comments[-1].id if comments else None
            self._store.watch(max_chat_id, post_id, self._watch_ttl,
                              last_comment_id=last)
        return sent

    def start(self) -> None:
        if self._poll_interval <= 0:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self.poll_once()
            except Exception:
                _logger.exception("Comment poll failed")

    async def poll_once(self) -> None:
        client = self._get_client()
        if client is None:
            return
        async with self._lock:
            for max_chat_id, post_id, last in self._store.active_watches():
                await self._poll_thread(client, max_chat_id, post_id, last)

    async def _poll_thread(self, client, max_chat_id, post_id, last) -> None:
        if not self._links.tg_targets_for_max(max_chat_id, post_id):
            self._store.unwatch(max_chat_id, post_id)
            return
        try:
            comments = await self._fetch(client, max_chat_id, post_id)
        except Exception as exc:
            _logger.warning("Could not fetch comments for MAX %s post %s: %s",
                            max_chat_id, post_id, exc)
            return
        last_id = _int_or_zero(last)
        newest = last_id
        for comment in comments:
            cid = _int_or_zero(getattr(comment, "id", None))
            if cid <= last_id:
                continue
            await self._mirror(client, max_chat_id, post_id, comment)
            newest = max(newest, cid)
        if newest > last_id:
            self._store.set_last(max_chat_id, post_id, newest)

    @staticmethod
    async def _fetch(client, max_chat_id, post_id) -> list:
        comments = await client.fetch_comments(
            int(max_chat_id), int(post_id), backward=FETCH_BATCH)
        return sorted(comments or [],
                      key=lambda c: _int_or_zero(getattr(c, "id", None)))

    async def _mirror(self, client, max_chat_id, post_id, comment, *,
                      include_own: bool = False) -> bool:
        sender = getattr(comment, "sender", None)
        own = sender is not None and sender == self._own_id()
        if own and not include_own:
            return False
        head = MessageLinkRegistry.head_tg_target(
            self._links.tg_targets_for_max(max_chat_id, post_id))
        if head is None:
            return False
        author = "Вы" if own else (
            await self._resolve_name(client, sender) if isinstance(sender, int)
            else "MAX")
        try:
            tg_id = await asyncio.to_thread(
                tg.send_message, self._token, head["telegram_chat_id"],
                comment_text(comment, author),
                reply_to_message_id=head["message_id"],
                message_thread_id=head.get("message_thread_id"),
            )
        except Exception as exc:
            _logger.warning("Could not mirror MAX comment %s (channel %s post %s): %s",
                            getattr(comment, "id", None), max_chat_id, post_id, exc)
            return False
        if tg_id:
            self._store.link(tg_id, max_chat_id, post_id, comment.id)
            _logger.info("Mirrored MAX comment %s (channel %s post %s) to Telegram msg %s",
                         comment.id, max_chat_id, post_id, tg_id)
        return bool(tg_id)
