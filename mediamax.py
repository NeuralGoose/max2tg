"""Telegram <-> MAX media adapter over PyMax's typed send / resolve API.

Send (Telegram -> MAX): build a Photo / Video / File from the downloaded bytes
and hand it to ``client.send_message(attachments=[...])``. PyMax uploads it and
waits for MAX to finish processing the attachment internally, so the legacy
manual upload-slot opcodes (80/82/87), multipart POSTs and the
``attachment.not.ready`` retry loop are gone.

Resolve (MAX -> Telegram): turn a file/video attachment id into a temporary CDN
URL via ``client.get_file_by_id`` / ``client.get_video_by_id`` (returns
``FileRequest`` / ``VideoRequest`` whose ``.url`` is the downloadable link;
``VideoRequest`` already unwraps the best MP4 rendition into ``.url``).

Video notes (circles) are a special case: MAX returns only a rendition map
(e.g. ``{"MP4_480": "..."}``) with no ``cache`` field, which PyMax's required
``VideoRequest.cache`` rejects, so the typed call raises. For that case we fall
back to a raw ``App.invoke(VIDEO_PLAY)`` and pick the best ``MP4_<height>`` URL
ourselves, independent of the typed model.
"""
import logging
from pathlib import PurePosixPath

from pymax import File, Photo, Video
from pymax.protocol import Opcode

_logger = logging.getLogger(__name__)

# Defensive ceiling for Telegram -> MAX uploads. The content is already bounded
# by the Telegram download path, but cap here too so a malformed/huge payload is
# rejected before it is handed to PyMax (and to bound peak memory).
UPLOAD_SIZE_LIMIT = 50 * 1024 * 1024


def _safe_name(name: str | None) -> str:
    """Strip any path components from a (possibly attacker-controlled) Telegram
    filename so only a bare basename reaches MAX."""
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return base or "file"


def _build_attachment(content: bytes, filename: str, kind: str):
    """Wrap raw bytes in the right PyMax send-attachment type for the kind."""
    filename = _safe_name(filename)
    if kind == "photo":
        return Photo(content, name=filename)
    if kind == "video":
        return Video(content, name=filename)
    return File(content, name=filename)


async def send_uploaded_media(client, chat_id, content: bytes, filename: str,
                              mime_type: str | None = None, kind: str = "file",
                              text: str = "", reply_to_message_id=None):
    """Send downloaded Telegram media into a MAX chat as a typed attachment.

    ``mime_type`` is kept for call-site compatibility but unused: PyMax infers
    the type from the filename extension.
    """
    if not content:
        raise ValueError("refusing to upload empty media to MAX")
    if len(content) > UPLOAD_SIZE_LIMIT:
        raise ValueError(
            f"media too large to upload to MAX ({len(content)} bytes > "
            f"{UPLOAD_SIZE_LIMIT})")
    attachment = _build_attachment(content, filename, kind)
    return await client.send_message(
        chat_id, text or "", reply_to=reply_to_message_id,
        attachments=[attachment])


async def resolve_file_url(client, file_id, chat_id, message_id) -> str:
    """Resolve a MAX file attachment to a temporary download URL."""
    request = await client.get_file_by_id(chat_id, message_id, file_id)
    url = getattr(request, "url", None) if request is not None else None
    if not url:
        raise RuntimeError(f"file resolve returned no url for file {file_id}")
    return url


def _best_mp4_url(payload: dict) -> str | None:
    """Pick the highest-resolution ``MP4_<height>`` URL from a raw VIDEO_PLAY
    payload, falling back to ``url`` / ``EXTERNAL`` if no renditions are listed."""
    if not isinstance(payload, dict):
        return None
    best_height = -1
    best_url = None
    for key, value in payload.items():
        if (isinstance(key, str) and key.startswith("MP4_")
                and isinstance(value, str) and value):
            try:
                height = int(key.split("_", 1)[1])
            except ValueError:
                continue
            if height > best_height:
                best_height = height
                best_url = value
    if best_url:
        return best_url
    for fallback_key in ("url", "EXTERNAL"):
        value = payload.get(fallback_key)
        if isinstance(value, str) and value:
            return value
    return None


async def _resolve_video_url_raw(client, video_id, chat_id, message_id) -> str | None:
    """Resolve a video via a raw ``App.invoke(VIDEO_PLAY)`` call.

    Used when the typed ``get_video_by_id`` can't parse the server payload (e.g.
    video notes / circles whose response omits ``cache``). Builds the camelCase
    payload by hand to avoid importing PyMax's internal payload models."""
    app = getattr(client, "_app", None) or client
    response = await app.invoke(
        Opcode.VIDEO_PLAY,
        {"chatId": chat_id, "messageId": message_id, "videoId": video_id},
    )
    payload = getattr(response, "payload", None)
    return _best_mp4_url(payload) if payload else None


async def resolve_video_url(client, video_id, chat_id, message_id) -> str:
    """Resolve a MAX video attachment to a temporary (best-rendition) MP4 URL.

    Tries the typed ``get_video_by_id`` first. If it raises (some payloads, such
    as video notes, can't be parsed by PyMax's ``VideoRequest`` model), falls
    back to a raw ``VIDEO_PLAY`` resolve that parses the renditions directly."""
    try:
        request = await client.get_video_by_id(chat_id, message_id, video_id)
    except Exception as exc:
        _logger.debug(
            "typed video resolve failed for video %s (%s); using raw fallback",
            video_id, exc)
        url = await _resolve_video_url_raw(client, video_id, chat_id, message_id)
        if url:
            return url
        raise RuntimeError(
            f"video resolve returned no url for video {video_id}") from exc
    url = getattr(request, "url", None) if request is not None else None
    if not url:
        raise RuntimeError(f"video resolve returned no url for video {video_id}")
    return url
