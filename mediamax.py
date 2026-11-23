"""Telegram <-> MAX media adapter over PyMax's typed send / resolve API.

Send (Telegram -> MAX): build a Photo / Video / File from the downloaded bytes
and hand it to ``client.send_message(attachments=[...])``. PyMax uploads it and
waits for MAX to finish processing the attachment internally, so the legacy
manual upload-slot opcodes (80/82/87), multipart POSTs and the
``attachment.not.ready`` retry loop are gone.

Resolve (MAX -> Telegram): turn a file/video attachment id into a temporary CDN
URL via ``client.get_file_by_id`` / ``client.get_video_by_id`` (returns
``FileRequest`` / ``VideoRequest`` whose ``.url`` is the downloadable link;
``VideoRequest`` already unwraps the best MP4 rendition into ``.url``, including
for video notes, whose response is only a rendition map).
"""
from pathlib import PurePosixPath

from pymax import File, Photo, Video, VideoNote, Voice

# Defensive ceiling for Telegram -> MAX uploads. The content is already bounded
# by the Telegram download path, but cap here too so a malformed/huge payload is
# rejected before it is handed to PyMax (and to bound peak memory).
UPLOAD_SIZE_LIMIT = 50 * 1024 * 1024


def _safe_name(name: str | None) -> str:
    """Strip any path components from a (possibly attacker-controlled) Telegram
    filename so only a bare basename reaches MAX."""
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return base or "file"


def _build_attachment(content: bytes, filename: str, kind: str,
                      duration_ms: int | None = None):
    """Wrap raw bytes in the right PyMax send-attachment type for the kind.

    Voice messages and video notes need a duration (PyMax can only detect it
    with its optional ``video`` extra); without one they go as a file / video.
    """
    filename = _safe_name(filename)
    if kind == "photo":
        return Photo(content, name=filename)
    if kind == "voice":
        if duration_ms:
            return Voice(content, name=filename, duration=duration_ms)
        return File(content, name=filename)
    if kind == "video_note":
        if duration_ms:
            return VideoNote(content, name=filename, duration=duration_ms)
        return Video(content, name=filename)
    if kind == "video":
        return Video(content, name=filename)
    return File(content, name=filename)


async def send_uploaded_media(client, chat_id, content: bytes, filename: str,
                              mime_type: str | None = None, kind: str = "file",
                              text: str = "", reply_to_message_id=None,
                              duration_ms: int | None = None):
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
    attachment = _build_attachment(content, filename, kind, duration_ms)
    reply_to = int(reply_to_message_id) if reply_to_message_id is not None else None
    return await client.send_message(
        chat_id, text or "", reply_to=reply_to, attachments=[attachment])


async def resolve_file_url(client, file_id, chat_id, message_id) -> str:
    """Resolve a MAX file attachment to a temporary download URL."""
    request = await client.get_file_by_id(chat_id, message_id, file_id)
    url = getattr(request, "url", None) if request is not None else None
    if not url:
        raise RuntimeError(f"file resolve returned no url for file {file_id}")
    return url


async def resolve_video_url(client, video_id, chat_id, message_id) -> str:
    """Resolve a MAX video attachment to a temporary (best-rendition) MP4 URL,
    or the external link for videos hosted outside MAX."""
    request = await client.get_video_by_id(chat_id, message_id, video_id)
    url = getattr(request, "url", None) if request is not None else None
    if not url:
        external = getattr(request, "external", None)
        url = external if isinstance(external, str) else None
    if not url:
        raise RuntimeError(f"video resolve returned no url for video {video_id}")
    return url
