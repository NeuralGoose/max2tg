"""Persistent local state for Telegram forum topics."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

def _default_state_path() -> Path:
    """Resolve the state path at call time so a path supplied via a .env file
    (loaded into os.environ by config.apply_dotenv() at startup) is honored.

    Reading the env var at import time was too early: main.py imports bridge ->
    state before calling apply_dotenv(), so a .env-only MAX2TG_STATE_PATH was
    silently ignored and the wrong state.json was used.
    """
    return Path(os.environ.get("MAX2TG_STATE_PATH")
                or (Path(__file__).parent / "state.json"))


# Where the MAX-chat -> Telegram-topic map is stored. Override with
# MAX2TG_STATE_PATH to keep it on a persistent volume (e.g. in Docker), so
# topics survive container restarts/rebuilds instead of being recreated.
# Kept for backwards compatibility; BridgeState resolves the path lazily.
STATE_PATH = _default_state_path()

_logger = logging.getLogger(__name__)

DELIVERED_IDS_LIMIT = 500
PENDING_PRELOAD_LIMIT = 200


class BridgeState:
    """Stores MAX chat -> Telegram topic mappings on disk."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _default_state_path()
        self._data: dict[str, Any] = {"topics": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.error("Could not read state.json (%s); continuing with "
                          "in-memory state.", exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._backup_corrupt(raw, exc)
            return
        if not isinstance(data, dict):
            _logger.error("state.json is not a JSON object; ignoring it.")
            return
        # Tolerate a malformed/missing topics map without discarding the rest of
        # the file (e.g. pending_preload_chat_ids, delivered) so queued work and
        # fallback-mode dedup are not silently lost.
        if not isinstance(data.get("topics"), dict):
            _logger.error("state.json has no valid 'topics' map; resetting "
                          "topics only and keeping other state.")
            data["topics"] = {}
        self._data = data

    def _backup_corrupt(self, raw: str, exc: Exception) -> None:
        """Preserve a corrupt state.json (rather than silently overwriting it)
        and log loudly, so topic/dedup loss is diagnosable."""
        backup = self.path.with_name(self.path.name + ".corrupt")
        try:
            backup.write_text(raw, encoding="utf-8")
            _logger.error("state.json is corrupt (%s); backed up to %s and "
                          "starting fresh. Topics will be recreated.", exc, backup)
        except OSError as berr:
            _logger.error("state.json is corrupt (%s) and the backup also failed "
                          "(%s); starting fresh.", exc, berr)

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

    def is_delivered(self, max_chat_id: int | str, message_id: int | str) -> bool:
        mid = str(message_id)
        topic = self.get_topic(max_chat_id)
        if topic:
            ids = topic.get("delivered_max_message_ids") or []
            if mid in {str(x) for x in ids}:
                return True
            if str(topic.get("last_seeded_max_message_id")) == mid:
                return True
        # Chats without a topic (fallback / legacy single-chat mode) track
        # delivery in a separate top-level map so a restart does not re-forward
        # every message again.
        return mid in set(self._delivered_no_topic(max_chat_id))

    def mark_delivered(self, max_chat_id: int | str, message_id: int | str) -> None:
        topic = self.get_topic(max_chat_id)
        mid = str(message_id)
        if topic is None:
            self._mark_delivered_no_topic(max_chat_id, mid)
            return
        ids = [str(x) for x in (topic.get("delivered_max_message_ids") or [])]
        if mid not in ids:
            ids.append(mid)
            if len(ids) > DELIVERED_IDS_LIMIT:
                ids = ids[-DELIVERED_IDS_LIMIT:]
            topic["delivered_max_message_ids"] = ids
        topic["last_delivered_max_message_id"] = mid
        topic["last_seeded_max_message_id"] = mid
        topic["updated_at"] = int(time.time())
        self._data["topics"][str(max_chat_id)] = topic
        self.save()

    def _delivered_no_topic(self, max_chat_id: int | str) -> list[str]:
        store = self._data.get("delivered") or {}
        return [str(x) for x in (store.get(str(max_chat_id)) or [])]

    def _mark_delivered_no_topic(self, max_chat_id: int | str, mid: str) -> None:
        store = self._data.setdefault("delivered", {})
        ids = [str(x) for x in (store.get(str(max_chat_id)) or [])]
        if mid in ids:
            return
        ids.append(mid)
        if len(ids) > DELIVERED_IDS_LIMIT:
            ids = ids[-DELIVERED_IDS_LIMIT:]
        store[str(max_chat_id)] = ids
        self.save()

    def delivered_no_topic_map(self) -> dict[str, list[str]]:
        """Top-level (chat id -> delivered message ids) for chats without a
        topic; used to hydrate the in-memory dedup cache on startup."""
        store = self._data.get("delivered") or {}
        return {str(k): [str(x) for x in (v or [])] for k, v in store.items()}

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
        self.mark_delivered(max_chat_id, max_message_id)
        if telegram_message_id:
            topic = self.get_topic(max_chat_id) or topic
            topic["last_seeded_telegram_message_id"] = telegram_message_id
            topic["updated_at"] = int(time.time())
            self._data["topics"][str(max_chat_id)] = topic
            self.save()

    def record_message_mirror(
        self,
        max_chat_id: int | str,
        max_message_id: int | str,
        *,
        telegram_chat_id,
        telegram_message_id: int,
        message_thread_id: int | None = None,
        role: str,
    ) -> None:
        topic = self.get_topic(max_chat_id)
        if not topic:
            return
        mirrors = topic.setdefault("message_mirrors", {})
        mid = str(max_message_id)
        entries = mirrors.setdefault(mid, [])
        for entry in entries:
            if entry.get("telegram_message_id") == telegram_message_id:
                return
        entries.append({
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
            "message_thread_id": message_thread_id,
            "role": role,
        })
        mirror_ids = list(mirrors.keys())
        if len(mirror_ids) > DELIVERED_IDS_LIMIT:
            for old_mid in mirror_ids[: len(mirror_ids) - DELIVERED_IDS_LIMIT]:
                mirrors.pop(old_mid, None)
        topic["message_mirrors"] = mirrors
        self._data["topics"][str(max_chat_id)] = topic
        self.save()

    def iter_message_mirrors(
        self,
    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """(max_chat_id, max_message_id, mirror entries) for all topics."""
        out: list[tuple[str, str, list[dict[str, Any]]]] = []
        for chat_id, topic in self._data.get("topics", {}).items():
            if not isinstance(topic, dict):
                continue
            mirrors = topic.get("message_mirrors") or {}
            if not isinstance(mirrors, dict):
                continue
            for max_mid, entries in mirrors.items():
                if isinstance(entries, list) and entries:
                    out.append((str(chat_id), str(max_mid), entries))
        return out

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

    def get_pending_preload_chat_ids(self) -> list[str]:
        raw = self._data.get("pending_preload_chat_ids") or []
        return [str(x) for x in raw if x is not None]

    def add_pending_preload_chat(self, max_chat_id: int | str) -> None:
        sid = str(max_chat_id)
        ids = self.get_pending_preload_chat_ids()
        if sid in ids:
            return
        ids.append(sid)
        if len(ids) > PENDING_PRELOAD_LIMIT:
            ids = ids[-PENDING_PRELOAD_LIMIT:]
        self._data["pending_preload_chat_ids"] = ids
        self.save()

    def remove_pending_preload_chat(self, max_chat_id: int | str) -> None:
        sid = str(max_chat_id)
        ids = [x for x in self.get_pending_preload_chat_ids() if x != sid]
        self._data["pending_preload_chat_ids"] = ids
        self.save()


def normalize_topic_title(value: str, fallback: str) -> str:
    title = " ".join((value or "").split()) or fallback
    # Telegram forum topic names are limited to 128 chars. Keep room for suffixes.
    return title[:120]
