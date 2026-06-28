"""Parse MAX message attachments into a normalized form for forwarding.

PyMax delivers attachments as typed Pydantic models on ``Message.attaches``
(PhotoAttachment, VideoAttachment, FileAttachment, StickerAttachment,
AudioAttachment, ShareAttachment, ContactAttachment, CallAttachment, and
UnknownAttachment for anything else). ``parse()`` turns those into the flat
``ParsedAttach`` items the bridge forwards to Telegram.

Photos/stickers/audio usually carry a direct CDN URL. Videos and files never
ship a ready URL, so they are emitted as ``video_resolve`` / ``file_resolve``
items the bridge resolves later via ``get_video_by_id`` / ``get_file_by_id``.

The parser is intentionally duck-typed (dispatch on ``.type``, read fields via
``getattr``) so it neither imports pymax nor breaks if a model field is missing.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ParsedAttach:
    kind: str            # photo, animation, sticker, video, document, voice,
                         # audio, link, note, file_resolve, video_resolve
    text: str            # human-readable description (caption / fallback)
    url: str | None = None
    filename: str | None = None
    file_id: int | str | None = None   # for file_resolve (resolve to a URL)
    video_id: int | str | None = None  # for video_resolve
    size: int | None = None            # bytes, when known (upload-limit checks)


def _safe_filename(name: object) -> str:
    """Strip any path components from an attacker-supplied attachment name."""
    if not isinstance(name, str) or not name.strip():
        return "файл"
    return PurePosixPath(name.replace("\\", "/")).name or "файл"


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        # MAX sometimes sends numeric fields (e.g. file size) as strings; parse
        # them so size-based checks (oversize pre-check) still apply.
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _format_duration(value) -> str:
    """Format a voice/audio duration (MAX gives milliseconds) as ' (N с)'."""
    seconds = _to_int(value)
    if not seconds:
        return ""
    if seconds > 1000:  # milliseconds
        seconds = round(seconds / 1000)
    return f" ({seconds} с)"


def _human_size(size) -> str:
    try:
        size = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def _attach_field(attach, *names: str):
    """Read a field from a PyMax model or an embedded forward attach dict."""
    if isinstance(attach, dict):
        for name in names:
            if name in attach and attach[name] is not None:
                return attach[name]
        return None
    for name in names:
        val = getattr(attach, name, None)
        if val is not None:
            return val
    return None


def _attach_kind(attach) -> str:
    """Normalized upper-case attachment type for a typed PyMax attachment.

    ``attach.type`` is an ``AttachmentType`` str-enum for known types (``.value``
    == "PHOTO") and a plain ``str`` for ``UnknownAttachment``; handle both.
    Embedded forward payloads use a raw dict with ``_type``.
    """
    if isinstance(attach, dict):
        raw = attach.get("_type") or attach.get("type") or ""
        return str(raw).upper()
    type_attr = getattr(attach, "type", "") or ""
    value = getattr(type_attr, "value", type_attr)
    return str(value).upper()


def _http_url(value) -> str | None:
    return value if isinstance(value, str) and value.startswith("http") else None


def _parse_one(attach) -> ParsedAttach | None:
    kind = _attach_kind(attach)

    if kind == "PHOTO":
        url = _http_url(_attach_field(attach, "base_url", "baseUrl"))
        if url:
            return ParsedAttach("photo", "🖼 Фото", url)
        return ParsedAttach("note", "🖼 Фото [не удалось получить ссылку]")

    if kind == "STICKER":
        url = (_http_url(_attach_field(attach, "url"))
               or _http_url(_attach_field(attach, "lottie_url", "lottieUrl")))
        if url:
            return ParsedAttach("sticker", "🩷 Стикер", url)
        return ParsedAttach("note", "🩷 Стикер")

    if kind == "VIDEO":
        video_id = _attach_field(attach, "video_id", "videoId")
        if video_id is not None:
            return ParsedAttach("video_resolve", "🎞 Видео", video_id=video_id)
        return ParsedAttach("note", "🎞 Видео — открыть в MAX")

    if kind == "AUDIO":
        url = _http_url(_attach_field(attach, "url"))
        label = f"🎤 Голосовое{_format_duration(_attach_field(attach, 'duration'))}"
        if url:
            return ParsedAttach("voice", label, url)
        return ParsedAttach("note", f"{label} — открыть в MAX")

    if kind == "FILE":
        name = _safe_filename(_attach_field(attach, "name"))
        size_int = _to_int(_attach_field(attach, "size"))
        size_label = _human_size(size_int)
        label = f"📎 {name}" + (f" ({size_label})" if size_label else "")
        file_id = _attach_field(attach, "file_id", "fileId")
        if file_id is not None:
            return ParsedAttach("file_resolve", label, filename=name,
                                file_id=file_id, size=size_int)
        return ParsedAttach("note", f"{label} — открыть в MAX")

    if kind == "SHARE":
        title = _attach_field(attach, "title") or ""
        url = _attach_field(attach, "url") or ""
        description = _attach_field(attach, "description") or ""
        parts = [p for p in (f"🔗 {title}".strip(), url, description)
                 if p and p != "🔗"]
        return ParsedAttach("link", "\n".join(parts) or "🔗 Ссылка")

    if kind == "CONTACT":
        name = _attach_field(attach, "name") or " ".join(
            p for p in (_attach_field(attach, "first_name", "firstName"),
                        _attach_field(attach, "last_name", "lastName")) if p)
        return ParsedAttach("note", f"👤 Контакт: {name}".strip())

    if kind == "CALL":
        return ParsedAttach("note", "📞 Звонок")

    # CONTROL / INLINE_KEYBOARD are service/UI attachments — nothing to show.
    if kind in ("CONTROL", "INLINE_KEYBOARD", ""):
        return None

    return ParsedAttach("note", f"📦 Вложение: {kind}")


def parse(message) -> list[ParsedAttach]:
    """Parse a PyMax ``Message``'s typed ``attaches`` into ParsedAttach items."""
    attaches = getattr(message, "attaches", None) or []
    result = []
    for attach in attaches:
        parsed = _parse_one(attach)
        if parsed:
            result.append(parsed)
    return result
