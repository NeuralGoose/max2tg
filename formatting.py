"""Convert rich text between MAX elements and Telegram Bot API entities."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MAX_TO_TG = {
    "STRONG": "bold",
    "EMPHASIZED": "italic",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "strikethrough",
    "MONOSPACED": "code",
    "CODE": "pre",
    "LINK": "text_link",
    "HEADING": "bold",
    "QUOTE": "blockquote",
}

_TG_TO_MD = {
    "bold": ("**", "**"),
    "italic": ("_", "_"),
    "underline": ("__", "__"),
    "strikethrough": ("~~", "~~"),
    "code": ("`", "`"),
    "pre": ("```", "```"),
}

# Inner styles open/close before outer when spans share a boundary (Telegram nesting).
_TG_STYLE_ORDER = {
    "italic": 0,
    "bold": 1,
    "underline": 2,
    "strikethrough": 3,
    "code": 4,
    "pre": 5,
}


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (Telegram/MAX offset scheme)."""
    return len(text.encode("utf-16-le")) // 2


def utf16_index_to_py(text: str, utf16_index: int) -> int:
    """Map a UTF-16 code-unit index to a Python string index."""
    units = 0
    for index, char in enumerate(text):
        if units >= utf16_index:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def py_index_to_utf16(text: str, py_index: int) -> int:
    """Map a Python string index to a UTF-16 code-unit index."""
    units = 0
    for char in text[:py_index]:
        units += 2 if ord(char) > 0xFFFF else 1
    return units


def utf16_slice(text: str, offset: int, length: int) -> str:
    start = utf16_index_to_py(text, offset)
    end = utf16_index_to_py(text, offset + length)
    return text[start:end]


def _element_dict(element: Any) -> dict[str, Any]:
    if isinstance(element, dict):
        data = dict(element)
    elif hasattr(element, "model_dump"):
        data = element.model_dump(by_alias=True)
    else:
        data = {
            "type": getattr(element, "type", None),
            "from": getattr(element, "from_", None),
            "length": getattr(element, "length", None),
            "attributes": getattr(element, "attributes", None),
        }
    attrs = data.get("attributes")
    if attrs is not None and hasattr(attrs, "model_dump"):
        data["attributes"] = attrs.model_dump(by_alias=True)
    return data


def extract_elements(message: Any) -> list[dict[str, Any]]:
    """Read MAX ``elements`` from a PyMax model, dict, or embedded payload."""
    if message is None:
        return []
    if isinstance(message, dict):
        raw = message.get("elements") or []
    else:
        raw = getattr(message, "elements", None) or []
        if not raw:
            extra = getattr(message, "model_extra", None) or {}
            if isinstance(extra, dict):
                raw = extra.get("elements") or []
    result: list[dict[str, Any]] = []
    for item in raw:
        data = _element_dict(item)
        if data.get("type"):
            result.append(data)
    return result


def max_elements_to_telegram(
    text: str,
    elements: list[Any],
) -> list[dict[str, Any]]:
    """Map MAX element spans to Telegram ``entities`` for plain ``text``."""
    if not text or not elements:
        return []
    text_len = utf16_len(text)
    tg_entities: list[dict[str, Any]] = []
    for raw in elements:
        data = _element_dict(raw)
        kind = str(data.get("type") or "").upper()
        if kind == "ANIMOJI":
            continue
        tg_type = _MAX_TO_TG.get(kind)
        if tg_type is None:
            continue
        offset = data.get("from")
        length = data.get("length")
        if offset is None or length is None:
            continue
        try:
            offset = int(offset)
            length = int(length)
        except (TypeError, ValueError):
            continue
        if offset < 0 or length <= 0 or offset + length > text_len:
            continue
        entity: dict[str, Any] = {
            "type": tg_type,
            "offset": offset,
            "length": length,
        }
        if tg_type == "text_link":
            attrs = data.get("attributes") or {}
            url = attrs.get("url") if isinstance(attrs, dict) else None
            if not url:
                continue
            entity["url"] = str(url)
        tg_entities.append(entity)
    tg_entities.sort(key=lambda item: (item["offset"], -item["length"]))
    return tg_entities


def shift_entities(entities: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    if not entities or offset <= 0:
        return [dict(item) for item in entities]
    shifted: list[dict[str, Any]] = []
    for entity in entities:
        item = dict(entity)
        item["offset"] = int(item["offset"]) + offset
        shifted.append(item)
    return shifted


def clip_entities(
    entities: list[dict[str, Any]],
    max_utf16: int,
) -> list[dict[str, Any]]:
    """Keep only entities fully contained in ``[0, max_utf16)``."""
    clipped: list[dict[str, Any]] = []
    for entity in entities:
        offset = int(entity["offset"])
        length = int(entity["length"])
        if offset < 0 or length <= 0 or offset + length > max_utf16:
            continue
        clipped.append(dict(entity))
    return clipped


def split_text_utf16(text: str, max_units: int) -> list[tuple[str, int, int]]:
    """Split ``text`` into chunks of at most ``max_units`` UTF-16 code units."""
    if max_units <= 0:
        raise ValueError("max_units must be positive")
    if utf16_len(text) <= max_units:
        return [(text, 0, utf16_len(text))]
    chunks: list[tuple[str, int, int]] = []
    start_py = 0
    start_utf16 = 0
    units = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units and units + char_units > max_units:
            chunk = text[start_py:index]
            chunks.append((chunk, start_utf16, units))
            start_py = index
            start_utf16 += units
            units = 0
        units += char_units
    if start_py < len(text) or not chunks:
        chunk = text[start_py:]
        chunks.append((chunk, start_utf16, utf16_len(chunk)))
    return chunks


def split_entities_for_chunk(
    entities: list[dict[str, Any]],
    chunk_start_utf16: int,
    chunk_len_utf16: int,
) -> list[dict[str, Any]]:
    """Rebase entities onto a UTF-16 slice of the full text."""
    chunk_end = chunk_start_utf16 + chunk_len_utf16
    result: list[dict[str, Any]] = []
    for entity in entities:
        start = int(entity["offset"])
        end = start + int(entity["length"])
        if end <= chunk_start_utf16 or start >= chunk_end:
            continue
        clipped_start = max(start, chunk_start_utf16)
        clipped_end = min(end, chunk_end)
        item = dict(entity)
        item["offset"] = clipped_start - chunk_start_utf16
        item["length"] = clipped_end - clipped_start
        if item["length"] > 0:
            result.append(item)
    return result


@dataclass(frozen=True)
class FormattedText:
    text: str
    entities: list[dict[str, Any]]

    @classmethod
    def plain(cls, text: str) -> FormattedText:
        return cls(text, [])

    @classmethod
    def from_max(cls, text: str, elements: list[Any]) -> FormattedText:
        return cls(text, max_elements_to_telegram(text, elements))

    def with_prefix(self, prefix: str) -> FormattedText:
        if not prefix:
            return self
        return FormattedText(
            prefix + self.text,
            shift_entities(self.entities, utf16_len(prefix)),
        )

    def append_plain_suffix(self, suffix: str) -> FormattedText:
        if not suffix:
            return self
        return FormattedText(self.text + suffix, list(self.entities))

    def clip(self, max_utf16: int) -> tuple[FormattedText, FormattedText | None]:
        if utf16_len(self.text) <= max_utf16:
            return self, None
        chunks = split_text_utf16(self.text, max_utf16)
        first_text, first_start, first_len = chunks[0]
        first_entities = split_entities_for_chunk(
            self.entities, first_start, first_len,
        )
        first = FormattedText(first_text, first_entities)
        if len(chunks) == 1:
            return first, None
        overflow_parts: list[str] = []
        for chunk_text, _start, _length in chunks[1:]:
            overflow_parts.append(chunk_text)
        overflow = FormattedText("\n".join(overflow_parts), [])
        return first, overflow

    def split_caption(self, max_utf16: int) -> tuple[FormattedText, FormattedText | None]:
        """Prefer splitting at a newline near the caption limit."""
        if utf16_len(self.text) <= max_utf16:
            return self, None
        py_limit = utf16_index_to_py(self.text, max_utf16)
        cut = self.text[:py_limit]
        newline = cut.rfind("\n")
        min_newline = utf16_index_to_py(self.text, int(max_utf16 * 0.7))
        if newline > min_newline:
            cut = cut[:newline]
        cut_utf16 = utf16_len(cut)
        caption = FormattedText(cut, clip_entities(self.entities, cut_utf16))
        overflow_text = self.text[len(cut):].lstrip("\n")
        overflow = FormattedText.plain(overflow_text) if overflow_text else None
        return caption, overflow


def build_delivery_formatted(
    formatted_text: FormattedText,
    notes: list[str],
    *,
    in_topic: bool,
    sender: str,
    is_channel: bool,
    attribution: str | None,
    header: str,
    reply_quote: str | None = None,
) -> FormattedText:
    """Compose bridge delivery body/caption with formatting only on MAX text."""
    notes_part = "\n".join(part for part in notes if part)
    content = formatted_text.text
    if notes_part:
        suffix = ("\n" if content else "") + notes_part
        formatted_text = formatted_text.append_plain_suffix(suffix)

    quote_header = (
        f"↩️ В ответ на: «{reply_quote}»" if reply_quote else None
    )

    if in_topic:
        content = formatted_text.text
        prefix = ""
        if quote_header:
            if content:
                prefix = f"{quote_header}\n\n"
            else:
                return FormattedText.plain(quote_header)
        if attribution:
            if content:
                prefix = f"{prefix}{attribution}\n\n" if prefix else f"{attribution}\n\n"
            elif not prefix:
                return FormattedText.plain(attribution)
            else:
                return FormattedText.plain(f"{prefix.rstrip()}\n{attribution}")
        if is_channel:
            if content:
                return formatted_text.with_prefix(prefix) if prefix else formatted_text
            return FormattedText.plain(sender)
        if content:
            prefix = f"{sender}:\n" + prefix
            return formatted_text.with_prefix(prefix)
        if prefix:
            return FormattedText.plain(f"{prefix.rstrip()}\n{sender}:")
        return FormattedText.plain(f"{sender}:")

    parts = [part for part in (header, quote_header, attribution) if part]
    prefix = "\n".join(parts)
    if prefix:
        prefix += "\n"
    body_parts = [
        part for part in (header, quote_header, attribution, formatted_text.text)
        if part
    ]
    body = "\n".join(body_parts)
    if not prefix:
        return FormattedText(body, list(formatted_text.entities))
    return formatted_text.with_prefix(prefix)


def _marker_pair(entity_type: str) -> tuple[str, str] | None:
    return _TG_TO_MD.get(entity_type)


def _style_priority(entity_type: str) -> int:
    return _TG_STYLE_ORDER.get(entity_type, 99)


def _validate_telegram_entities(
    text: str,
    entities: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not text or not entities:
        return []
    text_len = utf16_len(text)
    valid: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        try:
            offset = int(entity["offset"])
            length = int(entity["length"])
        except (KeyError, TypeError, ValueError):
            continue
        if offset < 0 or length <= 0 or offset + length > text_len:
            continue
        valid.append(entity)
    return valid


def _insert_at_original_utf16(text: str, result: str, utf16_pos: int, marker: str) -> str:
    """Insert ``marker`` at a UTF-16 boundary of the original ``text``."""
    py_idx = utf16_index_to_py(text, utf16_pos)
    return result[:py_idx] + marker + result[py_idx:]


def _py_at_original_utf16(
    text: str,
    utf16_pos: int,
    applied: list[tuple[int, int]],
) -> int:
    """Map an original UTF-16 boundary into the current result string."""
    py = utf16_index_to_py(text, utf16_pos)
    for pos, delta in applied:
        if pos < utf16_pos:
            py += delta
    return py


def _edit_sort_key(
    item: tuple[int, int, int, str, int | None],
) -> tuple[int, int, int]:
    utf16_pos, kind, priority, _payload, _span_len = item
    if kind == 0:
        # Close markers at the same boundary: outer style before inner.
        return (-utf16_pos, kind, -priority)
    if kind == 1:
        # Open markers at the same boundary: inner style before outer.
        return (-utf16_pos, kind, priority)
    return (-utf16_pos, kind, priority)


def telegram_entities_to_markdown(
    text: str,
    entities: list[dict[str, Any]] | None,
) -> str:
    """Convert Telegram entities on ``text`` into MAX-compatible markdown."""
    valid = _validate_telegram_entities(text, entities)
    if not valid:
        return text

    edits: list[tuple[int, int, int, str, int | None]] = []

    for entity in valid:
        etype = str(entity.get("type") or "")
        offset = int(entity["offset"])
        length = int(entity["length"])
        end = offset + length
        priority = _style_priority(etype)

        if etype == "text_link":
            url = entity.get("url")
            if not url:
                continue
            segment = utf16_slice(text, offset, length)
            edits.append((
                offset, 2, priority,
                f"[{segment}]({url})",
                length,
            ))
        elif etype == "blockquote":
            segment = utf16_slice(text, offset, length)
            wrapped = "\n".join(
                f"> {line}" if line else ">"
                for line in segment.splitlines()
            )
            edits.append((offset, 2, priority, wrapped, length))
        elif pair := _marker_pair(etype):
            left, right = pair
            edits.append((end, 0, priority, right, None))
            edits.append((offset, 1, priority, left, None))

    # Higher UTF-16 positions first; per-kind nesting order via _edit_sort_key.
    edits.sort(key=_edit_sort_key)

    result = text
    applied: list[tuple[int, int]] = []
    for utf16_pos, kind, _priority, payload, span_len in edits:
        start_py = _py_at_original_utf16(text, utf16_pos, applied)
        if kind == 2:
            end_py = _py_at_original_utf16(text, utf16_pos + int(span_len), applied)
            result = result[:start_py] + payload + result[end_py:]
            applied.append((utf16_pos, len(payload) - (end_py - start_py)))
        else:
            result = result[:start_py] + payload + result[start_py:]
            applied.append((utf16_pos, len(payload)))

    return result


def telegram_message_markdown(message: dict[str, Any]) -> str:
    """Extract formatted text/caption from a Telegram Bot API message dict."""
    if message.get("text"):
        text = str(message["text"])
        entities = message.get("entities") or []
    elif message.get("caption"):
        text = str(message["caption"])
        entities = message.get("caption_entities") or []
    else:
        return ""
    return telegram_entities_to_markdown(text, entities).strip()


_QUOTE_SNIPPET_MAX = 120


def _truncate_quote_snippet(text: str) -> str:
    snippet = re.sub(r"\s+", " ", text).strip()
    if len(snippet) > _QUOTE_SNIPPET_MAX:
        snippet = snippet[: _QUOTE_SNIPPET_MAX - 1].rstrip() + "…"
    return snippet


def _telegram_reply_media_note(reply_to: dict[str, Any]) -> str:
    if reply_to.get("sticker"):
        sticker = reply_to["sticker"]
        emoji = sticker.get("emoji") or ""
        return f"[Telegram sticker {emoji}]".strip()
    if reply_to.get("document"):
        document = reply_to["document"]
        name = document.get("file_name") or "file"
        return f"[Telegram file: {name}]"
    if reply_to.get("photo"):
        return "[Telegram photo]"
    if reply_to.get("video"):
        video = reply_to["video"]
        name = video.get("file_name") or "video"
        return f"[Telegram video: {name}]"
    if reply_to.get("animation"):
        animation = reply_to["animation"]
        name = animation.get("file_name") or "animation"
        return f"[Telegram animation: {name}]"
    if reply_to.get("voice"):
        return "[Telegram voice message]"
    if reply_to.get("audio"):
        audio = reply_to["audio"]
        name = audio.get("file_name") or audio.get("title") or "audio"
        return f"[Telegram audio: {name}]"
    if reply_to.get("video_note"):
        return "[Telegram video message]"
    return ""


def _telegram_quote_snippet(message: dict[str, Any]) -> str:
    quote = message.get("quote")
    if isinstance(quote, dict):
        text = (quote.get("text") or "").strip()
        if text:
            return _truncate_quote_snippet(text)
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict):
        md = telegram_message_markdown(reply_to)
        if md:
            return _truncate_quote_snippet(md.splitlines()[0])
        media_note = _telegram_reply_media_note(reply_to)
        if media_note:
            return _truncate_quote_snippet(media_note)
    return ""


def telegram_outgoing_with_quote(message: dict[str, Any]) -> str:
    """Telegram outbound text with quoted parent context for MAX."""
    body = telegram_message_markdown(message)
    snippet = _telegram_quote_snippet(message)
    if snippet:
        header = f"↩️ В ответ на: «{snippet}»"
        return f"{header}\n{body}" if body else header
    return body
