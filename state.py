"""Persistent local state for Telegram forum topics."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# Where the MAX-chat -> Telegram-topic map is stored. Override with
# MAX2TG_STATE_PATH to keep it on a persistent volume (e.g. in Docker), so
# topics survive container restarts/rebuilds instead of being recreated.
STATE_PATH = Path(os.environ.get("MAX2TG_STATE_PATH") or (Path(__file__).parent / "state.json"))

_logger = logging.getLogger(__name__)


class BridgeState:
    """Stores MAX chat -> Telegram topic mappings on disk."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._data: dict[str, Any] = {"topics": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Could not load state.json: %s", exc)
            return
        if isinstance(data, dict) and isinstance(data.get("topics"), dict):
            self._data = data

    def save(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.path)
            return
        except OSError as exc:
            # Atomic rename fails when state.json is a single bind-mounted file
            # in Docker (renaming over a mount point raises EBUSY/EXDEV). Fall
            # back to a direct in-place write so single-file mounts still persist.
            _logger.warning("Atomic state save failed (%s); writing in place.", exc)
        try:
            self.path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            _logger.error("Could not persist state: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass

    def get_topic(self, max_chat_id: int | str) -> dict[str, Any] | None:
        topic = self._data["topics"].get(str(max_chat_id))
        return topic if isinstance(topic, dict) else None

    def save_topic(
        self,
        max_chat_id: int | str,
        *,
        thread_id: int,
        title: str,
        chat_type: str,
        sender: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        existing = self.get_topic(max_chat_id) or {}
        topic = {
            **existing,
            "max_chat_id": max_chat_id,
            "telegram_thread_id": thread_id,
            "title": title,
            "chat_type": chat_type,
            "last_sender": sender,
            "updated_at": now,
        }
        if "created_at" not in topic:
            topic["created_at"] = now
        self._data["topics"][str(max_chat_id)] = topic
        self.save()
        return topic

    def mark_seeded_message(
        self,
        max_chat_id: int | str,
        *,
        max_message_id: int | str,
        telegram_message_id: int | None = None,
    ) -> None:
        topic = self.get_topic(max_chat_id)
        if not topic:
            return
        topic["last_seeded_max_message_id"] = str(max_message_id)
        if telegram_message_id:
            topic["last_seeded_telegram_message_id"] = telegram_message_id
        topic["updated_at"] = int(time.time())
        self._data["topics"][str(max_chat_id)] = topic
        self.save()

    def find_by_thread(self, thread_id: int) -> dict[str, Any] | None:
        for topic in self._data["topics"].values():
            if isinstance(topic, dict) and topic.get("telegram_thread_id") == thread_id:
                return topic
        return None

    def delete_topic(self, max_chat_id: int | str) -> bool:
        """Forget a topic (e.g. its Telegram thread was deleted) so the next
        message from that MAX chat recreates a fresh one. True if it existed."""
        if str(max_chat_id) in self._data["topics"]:
            del self._data["topics"][str(max_chat_id)]
            self.save()
            return True
        return False


def normalize_topic_title(value: str, fallback: str) -> str:
    title = " ".join((value or "").split()) or fallback
    # Telegram forum topic names are limited to 128 chars. Keep room for suffixes.
    return title[:120]
