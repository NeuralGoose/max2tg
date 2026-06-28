"""Minimal Telegram Bot API client: text, media, and update polling.

Media is sent by URL first (Telegram fetches it server-side); if that fails
(e.g. the CDN blocks Telegram's fetcher) we download the bytes ourselves and
upload them via multipart.
"""
import ipaddress
import logging
import re
import threading
import time
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

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.5
_RATE_LIMIT_MAX_ATTEMPTS = 5
_RATE_LIMIT_JITTER = 0.2

_api_lock = threading.Lock()
_api_last_call = 0.0
_api_min_interval = 0.05

_RETRY_AFTER_RE = re.compile(r"retry_after['\"]?\s*:\s*(\d+)", re.IGNORECASE)


class RateLimitError(RuntimeError):
    """Telegram Bot API returned 429 Too Many Requests."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


def set_api_min_interval(seconds: float) -> None:
    """Minimum spacing between outbound Bot API calls (process-wide)."""
    global _api_min_interval
    _api_min_interval = max(0.0, float(seconds))


def is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    msg = str(exc).lower()
    return "error_code': 429" in msg or '"error_code": 429' in msg or (
        "too many requests" in msg and "429" in msg
    )


def retry_after_from_error(exc: Exception) -> float | None:
    if isinstance(exc, RateLimitError):
        return exc.retry_after
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        return float(match.group(1))
    return None


def _pace_api_call() -> None:
    global _api_last_call
    if _api_min_interval <= 0:
        return
    with _api_lock:
        now = time.monotonic()
        wait = _api_min_interval - (now - _api_last_call)
        if wait > 0:
            time.sleep(wait)
        _api_last_call = time.monotonic()


def _retry_after_seconds(response: requests.Response, data: dict) -> float | None:
    if response.status_code == 429 or data.get("error_code") == 429:
        params = data.get("parameters") or {}
        retry_after = params.get("retry_after")
        if retry_after is not None:
            return float(retry_after)
        return 1.0
    return None


def _raise_api_error(method: str, data: dict, *, upload: bool = False) -> None:
    suffix = " upload" if upload else ""
    wait = None
    if data.get("error_code") == 429:
        params = data.get("parameters") or {}
        if params.get("retry_after") is not None:
            wait = float(params["retry_after"])
        else:
            wait = 1.0
        raise RateLimitError(
            f"Telegram API {method}{suffix} failed: {data}", wait,
        )
    raise RuntimeError(f"Telegram API {method}{suffix} failed: {data}")


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(token in msg for token in (
            "502", "503", "504", "bad gateway", "gateway timeout", "timeout",
        ))
    return False


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
    last_exc: Exception | None = None
    for attempt in range(_RATE_LIMIT_MAX_ATTEMPTS):
        _pace_api_call()
        try:
            response = requests.post(url, json=params, timeout=REQUEST_TIMEOUT)
            try:
                data = response.json()
            except ValueError:
                # Non-JSON body (e.g. a 5xx HTML gateway page). Treat transient
                # server errors as retryable instead of raising a JSONDecodeError
                # that _is_retryable_error would not recognize.
                if (response.status_code in (500, 502, 503, 504)
                        and attempt < _RATE_LIMIT_MAX_ATTEMPTS - 1):
                    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise RuntimeError(
                    f"Telegram API {method} returned HTTP "
                    f"{response.status_code} with a non-JSON body")
            if not data.get("ok"):
                wait = _retry_after_seconds(response, data)
                if wait is not None and attempt < _RATE_LIMIT_MAX_ATTEMPTS - 1:
                    _logger.info(
                        "Telegram %s rate limited, sleeping %.1fs (attempt %d)",
                        method, wait + _RATE_LIMIT_JITTER, attempt + 1,
                    )
                    time.sleep(wait + _RATE_LIMIT_JITTER)
                    continue
                _raise_api_error(method, data)
            return data["result"]
        except RateLimitError:
            raise
        except Exception as exc:
            if _is_retryable_error(exc) and attempt < _RETRY_ATTEMPTS - 1:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Telegram API {method} failed after retries")


def _call_upload(token: str, method: str, files: dict, **params) -> dict:
    url = API_BASE.format(token=token, method=method)
    last_exc: Exception | None = None
    for attempt in range(_RATE_LIMIT_MAX_ATTEMPTS):
        _pace_api_call()
        try:
            response = requests.post(
                url, data=params, files=files, timeout=UPLOAD_TIMEOUT,
            )
            try:
                data = response.json()
            except ValueError:
                if (response.status_code in (500, 502, 503, 504)
                        and attempt < _RATE_LIMIT_MAX_ATTEMPTS - 1):
                    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise RuntimeError(
                    f"Telegram API {method} upload returned HTTP "
                    f"{response.status_code} with a non-JSON body")
            if not data.get("ok"):
                wait = _retry_after_seconds(response, data)
                if wait is not None and attempt < _RATE_LIMIT_MAX_ATTEMPTS - 1:
                    _logger.info(
                        "Telegram %s upload rate limited, sleeping %.1fs",
                        method, wait + _RATE_LIMIT_JITTER,
                    )
                    time.sleep(wait + _RATE_LIMIT_JITTER)
                    continue
                _raise_api_error(method, data, upload=True)
            return data["result"]
        except RateLimitError:
            raise
        except Exception as exc:
            if _is_retryable_error(exc) and attempt < _RETRY_ATTEMPTS - 1:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Telegram API {method} upload failed after retries")


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


_forum_icon_sticker_ids_cache: dict[str, list[str]] = {}


def get_forum_topic_icon_sticker_ids(token: str) -> list[str]:
    """Return allowed custom_emoji_id values for forum topic icons."""
    cached = _forum_icon_sticker_ids_cache.get(token)
    if cached is not None:
        return cached
    stickers = _call(token, "getForumTopicIconStickers")
    ids: list[str] = []
    for sticker in stickers or []:
        if not isinstance(sticker, dict):
            continue
        emoji_id = sticker.get("custom_emoji_id")
        if emoji_id is not None:
            ids.append(str(emoji_id))
    _forum_icon_sticker_ids_cache[token] = ids
    return ids


def create_forum_topic(
    token: str,
    chat_id: int | str,
    name: str,
    *,
    icon_color: int | None = None,
    icon_custom_emoji_id: str | None = None,
) -> int:
    """Create a Telegram forum topic and return its message_thread_id."""
    params: dict = {"chat_id": chat_id, "name": name}
    if icon_color is not None:
        params["icon_color"] = icon_color
    if icon_custom_emoji_id:
        params["icon_custom_emoji_id"] = icon_custom_emoji_id
    result = _call(token, "createForumTopic", **params)
    return result["message_thread_id"]


def edit_forum_topic(
    token: str,
    chat_id: int | str,
    message_thread_id: int,
    name: str | None = None,
    *,
    icon_custom_emoji_id: str | None = None,
) -> None:
    params: dict = {
        "chat_id": chat_id,
        "message_thread_id": message_thread_id,
    }
    if name is not None:
        params["name"] = name
    # Only set a non-empty emoji id. An empty string would tell Telegram to
    # remove the icon, which is never what the bridge intends here.
    if icon_custom_emoji_id:
        params["icon_custom_emoji_id"] = icon_custom_emoji_id
    _call(token, "editForumTopic", **params)


def send_message(token: str, chat_id: int | str, text: str,
                 reply_to_message_id: int | None = None,
                 message_thread_id: int | None = None) -> int | None:
    """Send plain text, splitting over Telegram's length limit.

    Returns the message_id of the first chunk (used for reply mapping).
    """
    first_id: int | None = None
    for start in range(0, len(text), MAX_MESSAGE_LEN):
        chunk = text[start:start + MAX_MESSAGE_LEN]
        params = {"chat_id": chat_id, "text": chunk,
                  "disable_web_page_preview": True}
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
                message_thread_id: int | None = None) -> int | None:
    """Send media by URL, falling back to download + multipart upload."""
    caption = (caption or "")[:MAX_CAPTION_LEN] or None
    try:
        params = {"chat_id": chat_id, field: url}
        if message_thread_id:
            params["message_thread_id"] = message_thread_id
        if caption:
            params["caption"] = caption
        result = _call(token, method, **params)
        return result.get("message_id")
    except RateLimitError:
        # Already rate-limited: do NOT download + re-upload (extra API traffic
        # that makes the throttling worse). Let the caller back off and retry.
        raise
    except Exception as exc:
        _logger.info("URL send failed (%s), uploading bytes instead: %s",
                     method, exc)
    content = _download(url)
    params = {"chat_id": chat_id}
    if message_thread_id:
        params["message_thread_id"] = message_thread_id
    if caption:
        params["caption"] = caption
    files = {field: (filename or "file", content)}
    result = _call_upload(token, method, files, **params)
    return result.get("message_id")


def send_photo(token, chat_id, url, caption=None,
               message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendPhoto", "photo", chat_id, url, caption,
                       "photo.jpg", message_thread_id)


def send_photo_bytes(token: str, chat_id: int | str, content: bytes,
                     caption: str | None = None, filename: str = "photo.png",
                     message_thread_id: int | None = None) -> int | None:
    """Upload a photo from in-memory bytes (multipart), e.g. a generated QR code.

    Unlike send_photo (which sends by URL), this posts the raw bytes directly, so
    it works for images the bridge produces itself.
    """
    caption = (caption or "")[:MAX_CAPTION_LEN] or None
    params: dict = {"chat_id": chat_id}
    if message_thread_id:
        params["message_thread_id"] = message_thread_id
    if caption:
        params["caption"] = caption
    files = {"photo": (filename, content)}
    result = _call_upload(token, "sendPhoto", files, **params)
    return result.get("message_id")


def send_animation(token, chat_id, url, caption=None,
                   message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendAnimation", "animation", chat_id, url,
                       caption, "animation.mp4", message_thread_id)


def send_video(token, chat_id, url, caption=None,
               message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendVideo", "video", chat_id, url, caption,
                       "video.mp4", message_thread_id)


def send_voice(token, chat_id, url, caption=None,
               message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendVoice", "voice", chat_id, url, caption,
                       "voice.ogg", message_thread_id)


def send_audio(token, chat_id, url, caption=None,
               message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendAudio", "audio", chat_id, url, caption,
                       "audio.mp3", message_thread_id)


def send_document(token, chat_id, url, caption=None, filename=None,
                  message_thread_id: int | None = None) -> int | None:
    return _send_media(token, "sendDocument", "document", chat_id, url,
                       caption, filename or "file", message_thread_id)


def send_sticker(token, chat_id, url,
                 message_thread_id: int | None = None) -> int | None:
    """Stickers have no caption in Telegram; fall back to document on failure."""
    try:
        params = {"chat_id": chat_id, "sticker": url}
        if message_thread_id:
            params["message_thread_id"] = message_thread_id
        result = _call(token, "sendSticker", **params)
        return result.get("message_id")
    except RateLimitError:
        # Don't amplify a 429 with an extra send_document attempt; let the
        # caller back off and retry.
        raise
    except Exception as exc:
        _logger.info("sendSticker failed, sending as document: %s", exc)
        return send_document(token, chat_id, url, filename="sticker.webp",
                             message_thread_id=message_thread_id)


def _send_media_group_sequential(
    token: str,
    chat_id: int | str,
    items: list[dict],
    caption: str | None,
    message_thread_id: int | None,
) -> list[int]:
    """Fall back to individual sends when sendMediaGroup fails."""
    ids: list[int] = []
    for index, item in enumerate(items):
        item_caption = caption if index == 0 else None
        kind = item.get("type", "photo")
        url = item["url"]
        if kind == "video":
            msg_id = send_video(token, chat_id, url, item_caption,
                                message_thread_id=message_thread_id)
        else:
            msg_id = send_photo(token, chat_id, url, item_caption,
                                message_thread_id=message_thread_id)
        if msg_id is not None:
            ids.append(msg_id)
    return ids


def send_media_group(
    token: str,
    chat_id: int | str,
    items: list[dict],
    caption: str | None = None,
    message_thread_id: int | None = None,
) -> list[int]:
    """Send 2–10 photos/videos as a Telegram album. Caption only on the first item.

    Each item: {"type": "photo"|"video", "url": "https://..."}.
    Returns message_ids in album order; empty list on total failure.
    """
    if len(items) < 2:
        raise ValueError("send_media_group requires at least 2 items")
    caption = (caption or "")[:MAX_CAPTION_LEN] or None
    media_payload: list[dict] = []
    for index, item in enumerate(items):
        kind = item.get("type", "photo")
        if kind not in ("photo", "video"):
            kind = "photo"
        entry: dict = {"type": kind, "media": item["url"]}
        if index == 0 and caption:
            entry["caption"] = caption
        media_payload.append(entry)
    params: dict = {"chat_id": chat_id, "media": media_payload}
    if message_thread_id:
        params["message_thread_id"] = message_thread_id
    try:
        result = _call(token, "sendMediaGroup", **params)
        ids = [msg["message_id"] for msg in result if msg.get("message_id")]
        if ids:
            return ids
    except Exception as exc:
        if is_rate_limit_error(exc):
            raise
        _logger.info("sendMediaGroup failed, sending sequentially: %s", exc)
    return _send_media_group_sequential(
        token, chat_id, items, caption, message_thread_id,
    )
