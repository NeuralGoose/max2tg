"""Parse MAX message attachments into a normalized form for forwarding.

MAX attach types (from the web client): PHOTO, VIDEO, STICKER, FILE, AUDIO,
SHARE, CONTACT, LOCATION, CONTROL/WIDGET. Photos/stickers/audio usually carry
a direct CDN URL; files/videos carry only ids/tokens, so we forward them as a
described notification (with any embedded preview).
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
    file_id: int | str | None = None   # for file_resolve (WS resolve to a URL)
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
    return None


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


def _attach_type(attach: dict) -> str:
    return (attach.get("_type") or attach.get("type") or "").upper()


def _first_url(attach: dict, *keys: str) -> str | None:
    for key in keys:
        value = attach.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _parse_one(attach: dict) -> ParsedAttach | None:
    kind = _attach_type(attach)

    if kind == "PHOTO":
        url = _first_url(attach, "baseUrl", "url")
        mp4 = _first_url(attach, "mp4Url")
        if mp4:
            return ParsedAttach("animation", "🖼 GIF", mp4)
        if url:
            return ParsedAttach("photo", "🖼 Фото", url)
        return ParsedAttach("note", "🖼 Фото [не удалось получить ссылку]")

    if kind == "STICKER":
        mp4 = _first_url(attach, "mp4Url")
        url = _first_url(attach, "url", "lottieUrl")
        if mp4:
            return ParsedAttach("animation", "🩷 Стикер", mp4)
        if url:
            return ParsedAttach("sticker", "🩷 Стикер", url)
        return ParsedAttach("note", "🩷 Стикер")

    if kind == "VIDEO":
        url = _first_url(attach, "url")
        if url:
            return ParsedAttach("video", "🎞 Видео", url)
        video_id = attach.get("videoId") or attach.get("id")
        if video_id is not None:
            return ParsedAttach("video_resolve", "🎞 Видео", video_id=video_id)
        return ParsedAttach("note", "🎞 Видео — открыть в MAX")

    if kind == "AUDIO":
        url = _first_url(attach, "url")
        duration = attach.get("duration")
        suffix = f" ({duration} c)" if duration else ""
        if url:
            return ParsedAttach("voice", f"🎤 Голосовое{suffix}", url)
        return ParsedAttach("note", f"🎤 Голосовое сообщение{suffix} — открыть в MAX")

    if kind == "FILE":
        url = _first_url(attach, "url")
        name = _safe_filename(attach.get("name"))
        size_int = _to_int(attach.get("size"))
        size_label = _human_size(size_int)
        label = f"📎 {name}" + (f" ({size_label})" if size_label else "")
        if url:
            return ParsedAttach("document", label, url, filename=name, size=size_int)
        file_id = attach.get("fileId") or attach.get("id")
        if file_id is not None:
            return ParsedAttach("file_resolve", label, filename=name,
                                file_id=file_id, size=size_int)
        return ParsedAttach("note", f"{label} — открыть в MAX")

    if kind == "SHARE":
        title = attach.get("title") or ""
        url = attach.get("url") or ""
        host = attach.get("host") or ""
        parts = [p for p in (f"🔗 {title}".strip(), url, host) if p and p != "🔗"]
        return ParsedAttach("link", "\n".join(parts) or "🔗 Ссылка")

    if kind == "CONTACT":
        name = " ".join(
            p for p in (attach.get("firstName"), attach.get("lastName")) if p
        )
        phone = attach.get("phone") or ""
        return ParsedAttach("note", f"👤 Контакт: {name} {phone}".strip())

    if kind == "LOCATION":
        lat, lon = attach.get("latitude"), attach.get("longitude")
        if lat and lon:
            return ParsedAttach(
                "note", f"📍 Геопозиция: https://maps.google.com/?q={lat},{lon}")
        return ParsedAttach("note", "📍 Геопозиция")

    if kind in ("CONTROL", "WIDGET", ""):
        return None  # service/system attachments, nothing to show

    return ParsedAttach("note", f"📦 Вложение: {kind}")


def parse(message: dict) -> list[ParsedAttach]:
    raw = message.get("attaches") or message.get("attachments") or []
    if not isinstance(raw, list):
        return []
    result = []
    for attach in raw:
        if not isinstance(attach, dict):
            continue
        parsed = _parse_one(attach)
        if parsed:
            result.append(parsed)
    return result
