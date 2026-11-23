"""MAX bot inline keyboards → Telegram inline buttons.

Link and copy-text buttons map directly. Callback and message buttons get an
opaque ``callback_data`` token (Telegram allows only 64 bytes); the token maps
to the MAX ``callback_id`` / payload so a press can be relayed back with
``send_callback`` (or, for message buttons, by sending the button text).
Location/contact requests, chat and mini-app buttons are not shown.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

CALLBACK_PREFIX = "mx:"
BUTTON_TTL = 30 * 86400


def _field(obj, *names):
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _kind(obj) -> str:
    raw = _field(obj, "type", "_type") or ""
    return str(getattr(raw, "value", raw)).upper()


def inline_keyboard(message):
    """The ``InlineKeyboardAttachment`` of a MAX message, if any."""
    for attach in getattr(message, "attaches", None) or []:
        if _kind(attach) == "INLINE_KEYBOARD":
            return attach
    return None


class ButtonStore:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS button_callbacks (
                token TEXT PRIMARY KEY,
                max_chat_id TEXT NOT NULL,
                max_message_id TEXT NOT NULL,
                callback_id TEXT,
                button_type TEXT NOT NULL,
                payload TEXT,
                text TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._conn.execute("DELETE FROM button_callbacks WHERE created_at < ?",
                           (int(time.time()) - BUTTON_TTL,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, *, max_chat_id, max_message_id, callback_id, button_type: str,
            payload: str | None, text: str) -> str:
        token = secrets.token_urlsafe(12)
        self._conn.execute(
            """
            INSERT INTO button_callbacks (token, max_chat_id, max_message_id,
                callback_id, button_type, payload, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, str(max_chat_id), str(max_message_id),
             callback_id, button_type, payload, text, int(time.time())),
        )
        self._conn.commit()
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM button_callbacks WHERE token = ?", (token,),
        ).fetchone()
        return dict(row) if row else None


def build_reply_markup(message, store: ButtonStore, max_chat_id,
                       max_message_id) -> dict | None:
    """Telegram ``InlineKeyboardMarkup`` for a MAX message, or None."""
    attach = inline_keyboard(message)
    if attach is None:
        return None
    keyboard = _field(attach, "keyboard")
    callback_id = _field(attach, "callback_id", "callbackId")
    rows = []
    for row in _field(keyboard, "buttons") or []:
        out = []
        for button in row or []:
            text = str(_field(button, "text") or "").strip()
            if not text:
                continue
            kind = _kind(button)
            if kind == "LINK":
                url = _field(button, "url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    out.append({"text": text, "url": url})
            elif kind == "CLIPBOARD":
                payload = _field(button, "payload")
                if payload:
                    out.append({"text": text, "copy_text": {"text": str(payload)[:256]}})
            elif kind in ("CALLBACK", "MESSAGE"):
                if kind == "CALLBACK" and not callback_id:
                    continue
                token = store.add(
                    max_chat_id=max_chat_id, max_message_id=max_message_id,
                    callback_id=callback_id, button_type=kind,
                    payload=_field(button, "payload"), text=text,
                )
                out.append({"text": text, "callback_data": CALLBACK_PREFIX + token})
        if out:
            rows.append(out)
    return {"inline_keyboard": rows} if rows else None
