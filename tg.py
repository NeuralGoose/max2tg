"""Minimal Telegram Bot API client: text, media, and update polling.

Media is sent by URL first (Telegram fetches it server-side); if that fails
(e.g. the CDN blocks Telegram's fetcher) we download the bytes ourselves and
upload them via multipart.
"""
import ipaddress
import logging
from urllib.parse import urlparse

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
FILE_API_BASE = "https://api.telegram.org/file/bot{token}/{file_path}"
# (connect, read) timeouts so a stalled peer can't hang a worker thread forever.
REQUEST_TIMEOUT = (5, 30)
UPLOAD_TIMEOUT = (5, 120)
MAX_MESSAGE_LEN = 4096
MAX_CAPTION_LEN = 1024
# Hard cap on what we'll pull from a (potentially attacker-supplied) media URL.
DOWNLOAD_SIZE_LIMIT = 49 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024
MAX_REDIRECTS = 3
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/137.0.0.0 Safari/537.36")

_logger = logging.getLogger(__name__)


def _assert_public_url(url: str) -> None:
    """Reject non-http(s) URLs and direct requests to private/loopback IPs.

    Media URLs come from incoming (attacker-controllable) MAX messages, so this
    guards against the bridge being used as an SSRF proxy into the local network
    (e.g. http://127.0.0.1/... or http://169.254.169.254/ cloud metadata).

    We only block when the host is a *literal* private/loopback IP. We do NOT
    resolve domain names: this machine routes through a fake-ip proxy (Clash)
    that maps every domain into 198.18.0.0/15 and resolves for real upstream, so
    local DNS resolution is both meaningless and would block all legit traffic.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"blocked URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname, not a literal IP — allow (resolution happens upstream)
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise ValueError(f"blocked non-public address: {ip}")


def _call(token: str, method: str, **params) -> dict:
    url = API_BASE.format(token=token, method=method)
    response = requests.post(url, json=params, timeout=REQUEST_TIMEOUT)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    return data["result"]


def _call_upload(token: str, method: str, files: dict, **params) -> dict:
    url = API_BASE.format(token=token, method=method)
    response = requests.post(url, data=params, files=files, timeout=UPLOAD_TIMEOUT)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} upload failed: {data}")
    return data["result"]


def _download(url: str) -> bytes:
    """Fetch a remote media URL safely: validate each hop, stream, size-cap.

    Redirects are followed manually so every hop is re-validated against
    _assert_public_url (an open redirect on the CDN could otherwise point at an
    internal address).
    """
    headers = {"User-Agent": BROWSER_UA}
    for _ in range(MAX_REDIRECTS + 1):
        _assert_public_url(url)
        with requests.get(url, headers=headers, timeout=UPLOAD_TIMEOUT,
                          stream=True, allow_redirects=False) as response:
            if response.is_redirect and response.headers.get("Location"):
                url = requests.compat.urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > DOWNLOAD_SIZE_LIMIT:
                raise ValueError(f"remote file too large: {declared} bytes")
            chunks, received = [], 0
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
                received += len(chunk)
                if received > DOWNLOAD_SIZE_LIMIT:
                    raise ValueError("remote file exceeded size limit during download")
                chunks.append(chunk)
            return b"".join(chunks)
    raise ValueError("too many redirects")


def check_token(token: str) -> dict:
    """Validate bot token; returns bot info (getMe)."""
    return _call(token, "getMe")


def set_my_commands(token: str, commands: list[dict]) -> None:
    """Register the bot's command list so Telegram shows it in the '/' menu.

    commands: [{"command": "join", "description": "..."}, ...] (lowercase, no slash).
    """
    _call(token, "setMyCommands", commands=commands)


def get_updates(token: str, offset: int | None = None, timeout: int = 25) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return _call(token, "getUpdates", **params)


def get_file(token: str, file_id: str) -> dict:
    return _call(token, "getFile", file_id=file_id)


def download_file_by_id(token: str, file_id: str) -> tuple[bytes, str]:
    result = get_file(token, file_id)
    file_size = int(result.get("file_size") or 0)
    if file_size > DOWNLOAD_SIZE_LIMIT:
        raise ValueError(f"Telegram file too large: {file_size} bytes")
    file_path = result.get("file_path")
    if not file_path:
        raise ValueError("Telegram getFile returned no file_path")
    url = FILE_API_BASE.format(token=token, file_path=file_path)
    with requests.get(url, timeout=UPLOAD_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        chunks, received = [], 0
        for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
            received += len(chunk)
            if received > DOWNLOAD_SIZE_LIMIT:
                raise ValueError("Telegram file exceeded size limit during download")
            chunks.append(chunk)
    return b"".join(chunks), file_path


def create_forum_topic(token: str, chat_id: int | str, name: str) -> int:
    """Create a Telegram forum topic and return its message_thread_id."""
    result = _call(token, "createForumTopic", chat_id=chat_id, name=name)
    return result["message_thread_id"]


def edit_forum_topic(token: str, chat_id: int | str, message_thread_id: int,
                     name: str) -> None:
    _call(
        token,
        "editForumTopic",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        name=name,
    )


def send_message(token: str, chat_id: int | str, text: str,
                 reply_to_message_id: int | None = None,
                 message_thread_id: int | None = None,
                 disable_notification: bool = False) -> int | None:
    """Send plain text, splitting over Telegram's length limit.

    disable_notification=True delivers silently (no sound/alert) regardless of
    the recipient's mute settings. Returns the first chunk's message_id.
    """
    first_id: int | None = None
    for start in range(0, len(text), MAX_MESSAGE_LEN):
        chunk = text[start:start + MAX_MESSAGE_LEN]
        params = {"chat_id": chat_id, "text": chunk,
                  "disable_web_page_preview": True}
        if disable_notification:
            params["disable_notification"] = True
        if message_thread_id:
            params["message_thread_id"] = message_thread_id
        if reply_to_message_id and first_id is None:
            params["reply_to_message_id"] = reply_to_message_id
        result = _call(token, "sendMessage", **params)
        if first_id is None:
            first_id = result.get("message_id")
    return first_id


def _send_media(token: str, method: str, field: str, chat_id: int | str,
                url: str, caption: str | None, filename: str | None,
                message_thread_id: int | None = None,
                disable_notification: bool = False) -> int | None:
    """Send media by URL, falling back to download + multipart upload."""
    caption = (caption or "")[:MAX_CAPTION_LEN] or None
    try:
        params = {"chat_id": chat_id, field: url}
        if disable_notification:
            params["disable_notification"] = True
        if message_thread_id:
            params["message_thread_id"] = message_thread_id
        if caption:
            params["caption"] = caption
        result = _call(token, method, **params)
        return result.get("message_id")
    except Exception as exc:
        _logger.info("URL send failed (%s), uploading bytes instead: %s",
                     method, exc)
    content = _download(url)
    params = {"chat_id": chat_id}
    if disable_notification:
        params["disable_notification"] = True
    if message_thread_id:
        params["message_thread_id"] = message_thread_id
    if caption:
        params["caption"] = caption
    files = {field: (filename or "file", content)}
    result = _call_upload(token, method, files, **params)
    return result.get("message_id")


def send_photo(token, chat_id, url, caption=None,
               message_thread_id: int | None = None,
               disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendPhoto", "photo", chat_id, url, caption,
                       "photo.jpg", message_thread_id, disable_notification)


def send_animation(token, chat_id, url, caption=None,
                   message_thread_id: int | None = None,
                   disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendAnimation", "animation", chat_id, url,
                       caption, "animation.mp4", message_thread_id, disable_notification)


def send_video(token, chat_id, url, caption=None,
               message_thread_id: int | None = None,
               disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendVideo", "video", chat_id, url, caption,
                       "video.mp4", message_thread_id, disable_notification)


def send_voice(token, chat_id, url, caption=None,
               message_thread_id: int | None = None,
               disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendVoice", "voice", chat_id, url, caption,
                       "voice.ogg", message_thread_id, disable_notification)


def send_audio(token, chat_id, url, caption=None,
               message_thread_id: int | None = None,
               disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendAudio", "audio", chat_id, url, caption,
                       "audio.mp3", message_thread_id, disable_notification)


def send_document(token, chat_id, url, caption=None, filename=None,
                  message_thread_id: int | None = None,
                  disable_notification: bool = False) -> int | None:
    return _send_media(token, "sendDocument", "document", chat_id, url,
                       caption, filename or "file", message_thread_id, disable_notification)


def send_sticker(token, chat_id, url,
                 message_thread_id: int | None = None,
                 disable_notification: bool = False) -> int | None:
    """Stickers have no caption in Telegram; fall back to document on failure."""
    try:
        params = {"chat_id": chat_id, "sticker": url}
        if disable_notification:
            params["disable_notification"] = True
        if message_thread_id:
            params["message_thread_id"] = message_thread_id
        result = _call(token, "sendSticker", **params)
        return result.get("message_id")
    except Exception as exc:
        _logger.info("sendSticker failed, sending as document: %s", exc)
        return send_document(token, chat_id, url, filename="sticker.webp",
                             message_thread_id=message_thread_id,
                             disable_notification=disable_notification)
