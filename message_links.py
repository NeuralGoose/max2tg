"""Bidirectional MAX ↔ Telegram message link registry (SQLite + in-memory indexes)."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_logger = logging.getLogger(__name__)

LINK_ROW_LIMIT = 5000
_HEAD_ROLES = frozenset({"text", "caption"})


def _coerce_int_like(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def default_links_db_path(state_path: Path | None = None) -> Path:
    if state_path is not None:
        return state_path.with_name("links.db")
    env = os.environ.get("MAX2TG_LINKS_DB_PATH")
    if env:
        return Path(env)
    state_env = os.environ.get("MAX2TG_STATE_PATH")
    if state_env:
        return Path(state_env).with_name("links.db")
    return Path(__file__).parent / "links.db"


@dataclass(frozen=True)
class MessageLink:
    max_chat_id: str
    max_message_id: str
    telegram_chat_id: str
    telegram_message_id: int
    message_thread_id: int | None
    role: str
    origin: str
    source: str
    sender: str | None = None

    def tg_entry(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": _coerce_int_like(self.telegram_chat_id),
            "message_id": self.telegram_message_id,
            "role": self.role,
            "message_thread_id": self.message_thread_id,
        }


class MessageLinkRegistry:
    """In-memory indexes backed by SQLite for TG↔MAX message correspondence."""

    def __init__(self, db_path: Path | None = None):
        self._path = Path(db_path) if db_path is not None else default_links_db_path()
        self._by_max: dict[tuple[str, str], list[MessageLink]] = {}
        self._by_tg: dict[int, MessageLink] = {}
        self._reply_meta: dict[int, dict[str, Any]] = {}
        self._max_chrono: dict[str, list[tuple[int, str]]] = {}
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_chat_id TEXT NOT NULL,
                max_message_id TEXT NOT NULL,
                telegram_chat_id TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                message_thread_id INTEGER,
                role TEXT NOT NULL DEFAULT 'text',
                origin TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'live',
                sender TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE (max_chat_id, max_message_id, telegram_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_links_max
                ON message_links (max_chat_id, max_message_id);
            CREATE INDEX IF NOT EXISTS idx_links_tg
                ON message_links (telegram_chat_id, telegram_message_id);
            CREATE INDEX IF NOT EXISTS idx_links_max_created
                ON message_links (max_chat_id, created_at);
            """
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _row_to_link(self, row: sqlite3.Row) -> MessageLink:
        return MessageLink(
            max_chat_id=str(row["max_chat_id"]),
            max_message_id=str(row["max_message_id"]),
            telegram_chat_id=str(row["telegram_chat_id"]),
            telegram_message_id=int(row["telegram_message_id"]),
            message_thread_id=row["message_thread_id"],
            role=str(row["role"] or "text"),
            origin=str(row["origin"]),
            source=str(row["source"]),
            sender=row["sender"],
        )

    def _index_link(self, link: MessageLink) -> None:
        key = (link.max_chat_id, link.max_message_id)
        entries = self._by_max.setdefault(key, [])
        if not any(
            e.telegram_message_id == link.telegram_message_id for e in entries
        ):
            entries.append(link)
        self._by_tg[link.telegram_message_id] = link
        if link.sender and link.role in _HEAD_ROLES:
            self._reply_meta[link.telegram_message_id] = {
                "chat_id": _coerce_int_like(link.max_chat_id),
                "message_id": _coerce_int_like(link.max_message_id),
                "sender": link.sender,
                "telegram_chat_id": _coerce_int_like(link.telegram_chat_id),
                "message_thread_id": link.message_thread_id,
            }
        try:
            sort_key = int(link.max_message_id)
        except (TypeError, ValueError):
            sort_key = 0
        chrono = self._max_chrono.setdefault(link.max_chat_id, [])
        if (sort_key, link.max_message_id) not in chrono:
            chrono.append((sort_key, link.max_message_id))

    def link(
        self,
        max_chat_id,
        max_message_id,
        *,
        telegram_chat_id,
        telegram_message_id: int,
        message_thread_id: int | None = None,
        role: str = "text",
        origin: str,
        source: str = "live",
        sender: str | None = None,
    ) -> MessageLink:
        now = int(time.time())
        mc = str(max_chat_id)
        mm = str(max_message_id)
        tc = str(telegram_chat_id)
        conn = self._connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO message_links (
                max_chat_id, max_message_id, telegram_chat_id,
                telegram_message_id, message_thread_id, role, origin, source,
                sender, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mc, mm, tc, int(telegram_message_id), message_thread_id,
             role, origin, source, sender, now),
        )
        if sender:
            conn.execute(
                """
                UPDATE message_links SET sender = ?
                WHERE max_chat_id = ? AND max_message_id = ?
                  AND telegram_message_id = ?
                """,
                (sender, mc, mm, int(telegram_message_id)),
            )
        conn.commit()
        row = conn.execute(
            """
            SELECT * FROM message_links
            WHERE max_chat_id = ? AND max_message_id = ?
              AND telegram_message_id = ?
            """,
            (mc, mm, int(telegram_message_id)),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to persist message link")
        entry = self._row_to_link(row)
        self._index_link(entry)
        self._trim()
        return entry

    def hydrate(self, limit: int = LINK_ROW_LIMIT) -> int:
        self._by_max.clear()
        self._by_tg.clear()
        self._reply_meta.clear()
        self._max_chrono.clear()
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT * FROM message_links
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in reversed(rows):
            self._index_link(self._row_to_link(row))
        return len(rows)

    def import_json_mirrors(
        self,
        mirrors: Iterable[tuple[str, str, list[dict[str, Any]]]],
        *,
        default_telegram_chat_id,
    ) -> int:
        count = 0
        for max_chat_id, max_mid, entries in mirrors:
            for raw in entries:
                tg_id = raw.get("telegram_message_id")
                if tg_id is None:
                    continue
                self.link(
                    max_chat_id,
                    max_mid,
                    telegram_chat_id=(
                        raw.get("telegram_chat_id") or default_telegram_chat_id
                    ),
                    telegram_message_id=int(tg_id),
                    message_thread_id=raw.get("message_thread_id"),
                    role=raw.get("role") or "text",
                    origin="max_to_tg",
                    source="live",
                )
                count += 1
        return count

    def is_max_linked(self, max_chat_id, max_message_id) -> bool:
        key = (str(max_chat_id), str(max_message_id))
        return key in self._by_max and bool(self._by_max[key])

    def has_max_chat_links(self, max_chat_id) -> bool:
        return str(max_chat_id) in self._max_chrono

    def tg_targets_for_max(
        self, max_chat_id, max_message_id,
    ) -> list[dict[str, Any]]:
        entries = self._by_max.get(
            (str(max_chat_id), str(max_message_id)), [],
        )
        return [e.tg_entry() for e in entries]

    @staticmethod
    def head_tg_target(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not entries:
            return None
        return next(
            (e for e in entries if e.get("role") in _HEAD_ROLES),
            entries[0],
        )

    def max_target_for_tg(self, tg_message_id: int | None) -> dict | None:
        if not tg_message_id:
            return None
        link = self._by_tg.get(int(tg_message_id))
        if not link:
            return None
        return {
            "max_chat_id": _coerce_int_like(link.max_chat_id),
            "max_message_id": link.max_message_id,
        }

    def reply_target_for_tg(self, tg_message_id: int | None) -> dict | None:
        if not tg_message_id:
            return None
        meta = self._reply_meta.get(int(tg_message_id))
        if meta:
            return dict(meta)
        link = self._by_tg.get(int(tg_message_id))
        if not link:
            return None
        return {
            "chat_id": _coerce_int_like(link.max_chat_id),
            "message_id": _coerce_int_like(link.max_message_id),
            "sender": link.sender or "",
            "telegram_chat_id": _coerce_int_like(link.telegram_chat_id),
            "message_thread_id": link.message_thread_id,
        }

    def tg_message_id_for_max_parent(
        self, max_chat_id, reply_parent_max_id: int | None,
    ) -> int | None:
        if reply_parent_max_id is None:
            return None
        head = self.head_tg_target(
            self.tg_targets_for_max(max_chat_id, reply_parent_max_id),
        )
        if not head:
            return None
        return head["message_id"]

    def newest_linked_max_id(self, max_chat_id) -> str | None:
        chrono = self._max_chrono.get(str(max_chat_id))
        if not chrono:
            return None
        return max(chrono, key=lambda x: x[0])[1]

    def remove_max(self, max_chat_id, max_message_id) -> None:
        mc = str(max_chat_id)
        mm = str(max_message_id)
        links = self._by_max.pop((mc, mm), [])
        conn = self._connect()
        conn.execute(
            "DELETE FROM message_links WHERE max_chat_id = ? AND max_message_id = ?",
            (mc, mm),
        )
        conn.commit()
        for link in links:
            self._by_tg.pop(link.telegram_message_id, None)
            self._reply_meta.pop(link.telegram_message_id, None)
        chrono = self._max_chrono.get(mc)
        if chrono:
            self._max_chrono[mc] = [
                x for x in chrono if x[1] != mm
            ]

    def _trim(self) -> None:
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM message_links").fetchone()[0]
        if count <= LINK_ROW_LIMIT:
            return
        excess = count - LINK_ROW_LIMIT
        conn.execute(
            """
            DELETE FROM message_links WHERE id IN (
                SELECT id FROM message_links
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (excess,),
        )
        conn.commit()
        self.hydrate(LINK_ROW_LIMIT)
