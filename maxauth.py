"""Telegram-backed PyMax auth providers for headless (Docker) login.

PyMax's stock auth providers read the SMS code / show the QR on the console
(`input`, `getpass`, ASCII QR), which is useless for a bot running in a
container. These drive the interactive step through the Telegram admin chat:

  - ``TelegramSmsCodeProvider``  : posts a prompt, reads the code from a reply
  - ``TelegramPasswordProvider`` : posts a 2FA prompt, reads the password
  - ``TelegramQrHandler``        : posts the QR (image + link) for scanning

A single ``TelegramAuthPoll`` owns one Telegram ``getUpdates`` offset cursor so
the interactive auth step and the bridge's main Telegram loop never consume each
other's updates (Telegram advances a single offset per bot). Run interactive
auth first, then hand ``poll.offset`` to the main loop.
"""
import asyncio
import io
import logging
import re

import tg

_logger = logging.getLogger(__name__)

# How long to wait (seconds) for the admin to reply with a code / password.
SMS_CODE_TIMEOUT = 180
PASSWORD_TIMEOUT = 180
# Per-getUpdates long-poll timeout (seconds).
POLL_TIMEOUT = 25


def _coerce_chat_id(value):
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return value


class TelegramAuthPoll:
    """Owns a single Telegram getUpdates offset so only one reader is active.

    During interactive auth the providers call :meth:`wait_for_text`; afterwards
    the bridge's main loop should continue from :attr:`offset` so no update is
    lost or double-read.
    """

    def __init__(self, bot_token: str, admin_chat_id, *, start_offset=None,
                 poll_timeout: int = POLL_TIMEOUT):
        self._token = bot_token
        self._chat_id = _coerce_chat_id(admin_chat_id)
        self._offset = start_offset
        self._poll_timeout = poll_timeout

    @property
    def offset(self):
        return self._offset

    async def drain(self) -> None:
        """Advance the offset past any currently buffered updates so the next
        :meth:`wait_for_text` only sees messages sent from now on (skips a stale
        /start or an old reply)."""
        updates = await asyncio.to_thread(
            tg.get_updates, self._token, self._offset, 0)
        for update in updates:
            uid = update.get("update_id")
            if uid is not None:
                self._offset = uid + 1

    async def wait_for_text(self, *, timeout: float) -> str | None:
        """Return the next non-empty text message from the admin chat, or None
        if none arrives within ``timeout`` seconds."""
        if self._chat_id is None:
            # Without an admin chat we cannot tell the owner's reply from any
            # other user's, so refuse rather than accept an arbitrary message as
            # the SMS code / 2FA password.
            _logger.error("No admin chat id configured; cannot read auth reply "
                          "safely. Set MAX2TG_TELEGRAM_CHAT_ID.")
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                updates = await asyncio.to_thread(
                    tg.get_updates, self._token, self._offset, self._poll_timeout)
            except Exception as exc:
                # 409 Conflict (another getUpdates consumer) or a transient
                # network error: back off briefly and retry instead of aborting
                # the whole login, mirroring the bridge's main poll loop.
                if "409" in str(exc) or "conflict" in str(exc).lower():
                    _logger.warning("Auth poll getUpdates conflict (409); "
                                    "retrying shortly.")
                else:
                    _logger.warning("Auth poll getUpdates failed: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                uid = update.get("update_id")
                if uid is not None:
                    self._offset = uid + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if chat.get("id") != self._chat_id:
                    continue
                text = (message.get("text") or "").strip()
                if text:
                    return text
        return None


class TelegramSmsCodeProvider:
    """PyMax ``SmsCodeProvider`` that reads the SMS code from a Telegram reply."""

    def __init__(self, bot_token: str, admin_chat_id, poll: TelegramAuthPoll,
                 *, timeout: float = SMS_CODE_TIMEOUT):
        self._token = bot_token
        self._chat_id = _coerce_chat_id(admin_chat_id)
        self._poll = poll
        self._timeout = timeout

    async def get_code(self, phone: str) -> str:
        await asyncio.to_thread(
            tg.send_message, self._token, self._chat_id,
            f"🔐 Вход в MAX: пришёл код по SMS на {phone}?\n"
            "Ответьте на это сообщение кодом из SMS.")
        # Ignore anything already buffered (stale /start, old replies); only a
        # reply sent after the prompt should count as the code.
        await self._poll.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            text = await self._poll.wait_for_text(timeout=remaining)
            if text is None:
                break
            code = re.sub(r"\D", "", text)
            if code:
                return code
            # Non-numeric reply (e.g. a slash command) — keep waiting for digits.
        raise TimeoutError("SMS code was not provided via Telegram in time")


class TelegramPasswordProvider:
    """PyMax ``PasswordProvider`` (2FA) that reads the password from a reply."""

    def __init__(self, bot_token: str, admin_chat_id, poll: TelegramAuthPoll,
                 *, timeout: float = PASSWORD_TIMEOUT):
        self._token = bot_token
        self._chat_id = _coerce_chat_id(admin_chat_id)
        self._poll = poll
        self._timeout = timeout

    async def get_password(self, hint: str | None = None) -> str:
        prompt = ("🔐 Вход в MAX: введите пароль двухфакторной защиты "
                  "ответом в этот чат.")
        if hint:
            prompt += f"\nПодсказка: {hint}"
        await asyncio.to_thread(tg.send_message, self._token, self._chat_id, prompt)
        # Only a reply sent after the prompt should count (ignore stale backlog).
        await self._poll.drain()
        text = await self._poll.wait_for_text(timeout=self._timeout)
        if not text:
            raise TimeoutError("2FA password was not provided via Telegram in time")
        return text.strip()


def _render_qr_png(data: str) -> bytes:
    """Render a QR code for ``data`` as PNG bytes (requires qrcode + Pillow)."""
    import qrcode
    image = qrcode.make(data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TelegramQrHandler:
    """PyMax ``QrHandler`` that posts the login QR to the Telegram admin chat.

    Sends the QR as an image (best effort) and always posts the raw link as a
    tappable fallback. Polling/confirmation is handled by PyMax's ``QrAuthFlow``.
    """

    def __init__(self, bot_token: str, admin_chat_id):
        self._token = bot_token
        self._chat_id = _coerce_chat_id(admin_chat_id)

    async def show_qr(self, qr_url: str) -> None:
        caption = ("🔐 Вход в MAX: отсканируйте QR в приложении MAX "
                   "(Настройки → Устройства → Подключить).")
        try:
            png = await asyncio.to_thread(_render_qr_png, qr_url)
            await asyncio.to_thread(
                tg.send_photo_bytes, self._token, self._chat_id, png,
                caption, "max_qr.png")
        except Exception as exc:
            _logger.warning("Could not send QR image, sending link only: %s", exc)
        await asyncio.to_thread(
            tg.send_message, self._token, self._chat_id,
            f"Ссылка для входа (если QR не сканируется):\n{qr_url}")
