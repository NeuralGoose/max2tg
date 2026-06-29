"""Resolve MAX message text/attaches for forwards and build content fingerprints."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import formatting

_logger = logging.getLogger(__name__)

UNKNOWN_SENDER = "неизвестный отправитель"
FORWARD_ATTRIBUTION_PREFIX = "↪ "
GENERIC_FORWARD_ATTRIBUTION = f"{FORWARD_ATTRIBUTION_PREFIX}Пересланное сообщение"
FORWARD_FETCH_FAILED_FALLBACK = (
    "[не удалось загрузить текст пересланного сообщения — открыть в MAX]"
)
_MAX_FORWARD_DEPTH = 2
_KNOWN_LINK_TYPES = frozenset({"FORWARD", "REPLY"})


@dataclass
class ResolvedMessage:
    text: str
    attaches: list[Any]
    author: str
    attribution: str | None
    is_forward: bool
    elements: list[Any] = field(default_factory=list)
    forward_attempted: bool = False


def _extract_link(message: Any) -> dict[str, Any] | None:
    link = getattr(message, "link", None)
    if link is None:
        extra = getattr(message, "model_extra", None) or {}
        if isinstance(extra, dict):
            link = extra.get("link")
    if link is None and hasattr(message, "model_dump"):
        dumped = message.model_dump(by_alias=True)
        if isinstance(dumped, dict):
            link = dumped.get("link")
    if link is None:
        return None
    if isinstance(link, dict):
        return link
    if hasattr(link, "model_dump"):
        return link.model_dump(by_alias=True)
    return None


def _link_kind(link: dict[str, Any]) -> str:
    return str(link.get("type") or link.get("Type") or "").upper()


def _embedded_forward_payload(link: dict[str, Any]) -> dict[str, Any] | None:
    if _link_kind(link) != "FORWARD":
        return None
    embedded = link.get("message")
    return embedded if isinstance(embedded, dict) else None


def _parse_flat_forward_link(link: dict[str, Any]) -> tuple[int, int] | None:
    if _link_kind(link) != "FORWARD":
        return None
    chat_id = link.get("chatId") or link.get("chat_id")
    message_id = link.get("messageId") or link.get("message_id")
    if chat_id is None or message_id is None:
        return None
    return int(chat_id), int(message_id)


def _parse_forward_link(link: dict[str, Any]) -> tuple[int, int] | None:
    """Backward-compatible alias for flat outbound FORWARD refs."""
    return _parse_flat_forward_link(link)


def _forward_ref_from_embedded(link: dict[str, Any],
                               embedded: dict[str, Any]) -> tuple[int, int] | None:
    chat_id = link.get("chatId") or link.get("chat_id")
    message_id = embedded.get("id") or embedded.get("messageId")
    if chat_id is None or message_id is None:
        return None
    return int(chat_id), int(message_id)


def _message_from_embedded(embedded: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=embedded.get("id"),
        chat_id=embedded.get("chatId") or embedded.get("chat_id"),
        sender=embedded.get("sender"),
        text=(embedded.get("text") or "").strip(),
        attaches=list(embedded.get("attaches") or []),
        elements=list(embedded.get("elements") or []),
        model_extra={"link": embedded.get("link")} if embedded.get("link") else {},
    )


def _has_visible_body(text: str, attaches_list: list[Any]) -> bool:
    return bool(text) or bool(attaches_list)


def log_empty_inbound_warning(message: Any) -> None:
    """Capture wrapper messages with no visible body (preprod diagnostics)."""
    link = _extract_link(message)
    extra = getattr(message, "model_extra", None) or {}
    extra_keys = list(extra.keys()) if isinstance(extra, dict) else []
    _logger.warning(
        "Empty inbound id=%s chat_id=%s sender=%s type=%r text=%r "
        "attaches=%d link=%r model_extra_keys=%s",
        getattr(message, "id", None),
        getattr(message, "chat_id", None),
        getattr(message, "sender", None),
        getattr(message, "type", None),
        getattr(message, "text", ""),
        len(getattr(message, "attaches", None) or []),
        link,
        extra_keys,
    )


def content_message(resolved: ResolvedMessage) -> SimpleNamespace:
    """Minimal message-like object for attaches.parse."""
    return SimpleNamespace(text=resolved.text, attaches=resolved.attaches)


async def _author_name(
    sender_id: int | None,
    *,
    own_id: int | None,
    chat_type: str,
    chat_title: str,
    resolve_sender_name: Callable[[int], Awaitable[str]],
) -> str:
    if sender_id is not None and sender_id == own_id:
        return "Вы"
    if isinstance(sender_id, int):
        return await resolve_sender_name(sender_id)
    if (chat_type or "").lower() == "channel" and chat_title:
        return chat_title
    return UNKNOWN_SENDER


async def _fetch_forward_original(
    client: Any,
    source_chat_id: int,
    source_message_id: int,
) -> Any | None:
    _logger.info(
        "Resolving FORWARD chat=%s msg=%s",
        source_chat_id,
        source_message_id,
    )
    try:
        return await client.get_message(source_chat_id, source_message_id)
    except Exception as exc:
        _logger.warning(
            "Could not fetch forwarded original %s/%s: %s",
            source_chat_id,
            source_message_id,
            exc,
        )
        return None


def _forward_attribution(author: str) -> str:
    if author != UNKNOWN_SENDER:
        return f"{FORWARD_ATTRIBUTION_PREFIX}{author}"
    return GENERIC_FORWARD_ATTRIBUTION


async def _resolve_forward_link(
    link: dict[str, Any],
    client: Any,
    *,
    chat_type: str,
    chat_title: str,
    own_id: int | None,
    resolve_sender_name: Callable[[int], Awaitable[str]],
    _depth: int,
) -> tuple[str, list[Any], list[Any], Any, bool] | None:
    """Returns (text, attaches, elements, sender_id, used_embedded) or None."""
    embedded = _embedded_forward_payload(link)
    if embedded is not None:
        text = (embedded.get("text") or "").strip()
        attaches_list = list(embedded.get("attaches") or [])
        elements_list = formatting.extract_elements(embedded)
        sender_id = embedded.get("sender")
        if _has_visible_body(text, attaches_list):
            return text, attaches_list, elements_list, sender_id, True
        if _depth + 1 < _MAX_FORWARD_DEPTH:
            nested = await resolve_message_content(
                _message_from_embedded(embedded),
                client,
                chat_type=chat_type,
                chat_title=chat_title,
                own_id=own_id,
                resolve_sender_name=resolve_sender_name,
                _depth=_depth + 1,
            )
            if _has_visible_body(nested.text, nested.attaches):
                return nested.text, nested.attaches, nested.elements, embedded.get("sender"), True

    forward_ref = (
        _parse_flat_forward_link(link)
        or (embedded and _forward_ref_from_embedded(link, embedded))
    )
    if forward_ref is None:
        return None

    source_chat_id, source_message_id = forward_ref
    original = await _fetch_forward_original(
        client, source_chat_id, source_message_id,
    )
    if original is not None:
        text = (getattr(original, "text", "") or "").strip()
        attaches_list = list(getattr(original, "attaches", None) or [])
        elements_list = formatting.extract_elements(original)
        sender_id = getattr(original, "sender", None)
        if (not _has_visible_body(text, attaches_list)
                and _depth + 1 < _MAX_FORWARD_DEPTH):
            nested = await resolve_message_content(
                original,
                client,
                chat_type=chat_type,
                chat_title=chat_title,
                own_id=own_id,
                resolve_sender_name=resolve_sender_name,
                _depth=_depth + 1,
            )
            if _has_visible_body(nested.text, nested.attaches):
                return nested.text, nested.attaches, nested.elements, sender_id, False
        return text, attaches_list, elements_list, sender_id, False

    return None


async def resolve_message_content(
    message: Any,
    client: Any,
    *,
    chat_type: str,
    chat_title: str,
    own_id: int | None,
    resolve_sender_name: Callable[[int], Awaitable[str]],
    _depth: int = 0,
) -> ResolvedMessage:
    """Resolve text/attaches/author, fetching FORWARD originals when needed."""
    text = (getattr(message, "text", "") or "").strip()
    attaches_list = list(getattr(message, "attaches", None) or [])
    elements_list = formatting.extract_elements(message)
    sender_id = getattr(message, "sender", None)
    is_forward = False
    forward_attempted = False

    link = _extract_link(message)
    if link:
        link_kind = _link_kind(link)
        if link_kind == "FORWARD":
            forward_attempted = True
            resolved = await _resolve_forward_link(
                link,
                client,
                chat_type=chat_type,
                chat_title=chat_title,
                own_id=own_id,
                resolve_sender_name=resolve_sender_name,
                _depth=_depth,
            )
            if resolved is not None:
                is_forward = True
                text, attaches_list, elements_list, sender_id, _used_embedded = resolved
            else:
                is_forward = True
                _logger.warning(
                    "Unrecognized FORWARD link shape on message id=%s chat_id=%s: %r",
                    getattr(message, "id", None),
                    getattr(message, "chat_id", None),
                    link,
                )
            if is_forward and not _has_visible_body(text, attaches_list):
                _logger.warning(
                    "FORWARD original empty or missing (link=%r)",
                    link,
                )
                text = FORWARD_FETCH_FAILED_FALLBACK
                elements_list = []
        elif (link_kind not in _KNOWN_LINK_TYPES
              and not _has_visible_body(text, attaches_list)):
            _logger.warning(
                "Unknown inbound link type %r on message id=%s chat_id=%s: %r",
                link_kind,
                getattr(message, "id", None),
                getattr(message, "chat_id", None),
                link,
            )

    author = await _author_name(
        sender_id,
        own_id=own_id,
        chat_type=chat_type,
        chat_title=chat_title,
        resolve_sender_name=resolve_sender_name,
    )
    attribution = _forward_attribution(author) if is_forward else None

    if not _has_visible_body(text, attaches_list):
        log_empty_inbound_warning(message)

    return ResolvedMessage(
        text=text,
        attaches=attaches_list,
        author=author,
        attribution=attribution,
        is_forward=is_forward,
        elements=elements_list,
        forward_attempted=forward_attempted,
    )


def _attach_signature(attach: Any) -> str:
    if isinstance(attach, dict):
        kind_str = str(attach.get("_type") or attach.get("type") or "").upper()
    else:
        kind = getattr(attach, "type", None)
        kind_str = str(getattr(kind, "value", kind) or "").upper()
    parts = [kind_str]
    if isinstance(attach, dict):
        for key in ("fileId", "videoId", "baseUrl", "url", "file_id", "video_id", "base_url"):
            val = attach.get(key)
            if val:
                parts.append(f"{key}={val}")
                return "|".join(parts)
    else:
        for key in ("file_id", "video_id", "base_url", "url"):
            val = getattr(attach, key, None)
            if val:
                parts.append(f"{key}={val}")
                return "|".join(parts)
        if hasattr(attach, "model_dump"):
            data = attach.model_dump(by_alias=True)
            for key in ("fileId", "videoId", "baseUrl", "url"):
                val = data.get(key)
                if val:
                    parts.append(f"{key}={val}")
                    break
    return "|".join(parts)


def message_content_fingerprint(message: Any) -> str:
    """Stable signature of visible message content (ignores stats/comments)."""
    text = (getattr(message, "text", "") or "").strip()
    attach_sigs = [
        _attach_signature(attach)
        for attach in (getattr(message, "attaches", None) or [])
    ]
    return "\n".join([text, *attach_sigs])
