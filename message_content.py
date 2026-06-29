"""Resolve MAX message text/attaches for forwards and build content fingerprints."""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import attaches
import formatting

_logger = logging.getLogger(__name__)

UNKNOWN_SENDER = "неизвестный отправитель"
FORWARD_ATTRIBUTION_PREFIX = "↪ "
GENERIC_FORWARD_ATTRIBUTION = f"{FORWARD_ATTRIBUTION_PREFIX}Пересланное сообщение"
FORWARD_FETCH_FAILED_FALLBACK = (
    "[не удалось загрузить текст пересланного сообщения — открыть в MAX]"
)
REPLY_QUOTE_PREFIX = "↩️ В ответ на"
REPLY_ONLY_PREFIX = "↩️ Ответ"
_QUOTE_SNIPPET_MAX = 120
_MAX_FORWARD_DEPTH = 2
_UNWRAP_MAX_DEPTH = 4
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
    reply_quote: str | None = None
    reply_parent_max_id: int | None = None
    is_reply: bool = False


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


def _message_as_inner_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    return {
        "text": getattr(message, "text", "") or "",
        "attaches": list(getattr(message, "attaches", None) or []),
        "link": _extract_link(message),
    }


def _quote_snippet(inner: dict[str, Any]) -> str:
    """Compact one-line preview of a replied-to message."""
    text = (inner.get("text") or "").strip()
    if not text:
        parsed = attaches.parse(_message_from_embedded(inner))
        if parsed and parsed[0].text:
            text = parsed[0].text.splitlines()[0]
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _QUOTE_SNIPPET_MAX:
        text = text[: _QUOTE_SNIPPET_MAX - 1].rstrip() + "…"
    return text


def _unwrap_inner(inner: dict[str, Any], depth: int = 0) -> tuple[str, list[Any]]:
    """Innermost text/attaches through nested reply/forward links."""
    if depth >= _UNWRAP_MAX_DEPTH:
        return ("", [])
    text = (inner.get("text") or "").strip()
    attaches_list = list(inner.get("attaches") or [])
    if text or attaches_list:
        return text, attaches_list
    nested = inner.get("link")
    if isinstance(nested, dict):
        nested_msg = nested.get("message")
        if isinstance(nested_msg, dict):
            return _unwrap_inner(nested_msg, depth + 1)
    return ("", [])


def _parse_reply_parent_id(link: dict[str, Any], inner: dict[str, Any] | None) -> int | None:
    parent_raw = link.get("messageId") or link.get("message_id")
    if parent_raw is None and inner is not None:
        parent_raw = inner.get("id")
    if parent_raw is None:
        return None
    return int(parent_raw)


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


async def _resolve_reply_link(
    link: dict[str, Any],
    message: Any,
    client: Any,
    *,
    text: str,
    attaches_list: list[Any],
    elements_list: list[Any],
) -> tuple[str, list[Any], list[Any], str | None, int | None, bool]:
    """Returns (text, attaches, elements, reply_quote, parent_id, is_reply)."""
    inner = link.get("message")
    inner_dict = inner if isinstance(inner, dict) else None
    parent_id = _parse_reply_parent_id(link, inner_dict)
    reply_quote: str | None = None
    is_reply = False

    if inner_dict is None and parent_id is not None:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None:
            fetched = await _fetch_forward_original(
                client, int(chat_id), parent_id,
            )
            if fetched is not None:
                inner_dict = _message_as_inner_dict(fetched)

    if inner_dict is not None:
        reply_quote = _quote_snippet(inner_dict) or None

    if _has_visible_body(text, attaches_list):
        is_reply = True
        return text, attaches_list, elements_list, reply_quote, parent_id, is_reply

    if inner_dict is not None:
        is_reply = True
        inner_text, inner_attaches = _unwrap_inner(inner_dict)
        if reply_quote:
            header = f"{REPLY_QUOTE_PREFIX}: «{reply_quote}»"
            text = f"{header}\n{inner_text}" if inner_text else header
        else:
            text = (
                f"{REPLY_ONLY_PREFIX}:\n{inner_text}"
                if inner_text
                else REPLY_ONLY_PREFIX
            )
        return text, inner_attaches, [], None, parent_id, is_reply

    if parent_id is not None:
        is_reply = True
        if reply_quote:
            text = f"{REPLY_QUOTE_PREFIX}: «{reply_quote}»"
        else:
            text = REPLY_ONLY_PREFIX
        return text, [], [], None, parent_id, is_reply

    return text, attaches_list, elements_list, None, None, False


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
    is_reply = False
    reply_quote: str | None = None
    reply_parent_max_id: int | None = None

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
        elif link_kind == "REPLY":
            (
                text,
                attaches_list,
                elements_list,
                reply_quote,
                reply_parent_max_id,
                is_reply,
            ) = await _resolve_reply_link(
                link,
                message,
                client,
                text=text,
                attaches_list=attaches_list,
                elements_list=elements_list,
            )
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
        reply_quote=reply_quote,
        reply_parent_max_id=reply_parent_max_id,
        is_reply=is_reply,
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
