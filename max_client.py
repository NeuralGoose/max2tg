"""MaxClient subclass that sends browser-like handshake headers and the
current web-app version.

Two reasons this subclass exists:
1. The bare vkmax client is rejected with HTTP 403: ws-api.oneme.ru requires
   an Origin and a browser User-Agent on the WebSocket handshake.
2. The PyPI vkmax (1.0.2) advertises an outdated APP_VERSION; MAX rejects
   phone auth from a stale web-app version. We send the current one.
"""
import asyncio
import json
import logging
import os
import uuid

import websockets
from vkmax.client import WS_HOST, MaxClient

_logger = logging.getLogger(__name__)

# Optional outbound proxy for the MAX websocket, e.g. when running on a foreign
# server that MAX geo-blocks. Set to a Russian SOCKS/HTTP proxy URL, e.g.
# "socks5://user:pass@host:1080" or "http://host:3128".
WS_PROXY = os.environ.get("MAX2TG_WS_PROXY") or None

# A dropped MAX response must not park an awaiting caller forever (vkmax's
# invoke_method has no timeout). The keepalive opcode is exempt — see below.
MAX_INVOKE_TIMEOUT = 60

# Keep in sync with the MAX web client; bump if phone auth starts failing.
APP_VERSION = "26.2.2"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/137.0.0.0 Safari/537.36")

HANDSHAKE_HEADERS = {
    "Origin": "https://web.max.ru",
    "User-Agent": USER_AGENT,
}


class MaxAuthError(RuntimeError):
    """Raised when MAX rejects the auth request; carries the raw payload."""


class BrowserMaxClient(MaxClient):
    async def connect(self):
        if self._connection:
            raise RuntimeError("Already connected")
        _logger.info("Connecting to %s%s...", WS_HOST,
                     f" via proxy {WS_PROXY}" if WS_PROXY else "")
        connect_kwargs = {"additional_headers": HANDSHAKE_HEADERS}
        if WS_PROXY:
            connect_kwargs["proxy"] = WS_PROXY
        self._connection = await websockets.connect(WS_HOST, **connect_kwargs)
        self._recv_task = asyncio.create_task(self._recv_loop())
        _logger.info("Connected. Receive task started.")
        return self._connection

    async def _recv_loop(self):
        # Defensive override: the reverse-engineered MAX server can push a
        # non-JSON or seq-less frame; vkmax's loop subscripts packet["seq"] and
        # json.loads() unguarded, so one bad frame would kill the whole receive
        # pipeline (and, since wait_closed() wouldn't return, block reconnect).
        try:
            async for raw in self._connection:
                try:
                    packet = json.loads(raw)
                except (ValueError, TypeError):
                    _logger.warning("Skipping unparseable MAX frame")
                    continue
                if not isinstance(packet, dict):
                    # Valid JSON but not an object (array/scalar): it can be
                    # neither a request reply nor an incoming event, so don't
                    # dispatch it to a callback that assumes a dict.
                    _logger.warning("Skipping non-object MAX frame (%s)",
                                    type(packet).__name__)
                    continue
                try:
                    seq = packet.get("seq")
                    future = self._pending.pop(seq, None)
                    if future is not None:
                        if not future.done():
                            future.set_result(packet)
                    elif self._incoming_event_callback:
                        asyncio.create_task(
                            self._incoming_event_callback(self, packet))
                except Exception:
                    _logger.exception("Error handling MAX frame")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _logger.warning("MAX receive loop ended: %s", exc)

    async def _send_hello_packet(self):
        return await self.invoke_method(
            opcode=6,
            payload={
                "userAgent": {
                    "deviceType": "WEB",
                    "locale": "ru_RU",
                    "osVersion": "Windows",
                    "deviceName": "max2tg bridge",
                    "headerUserAgent": USER_AGENT,
                    "deviceLocale": "ru-RU",
                    "appVersion": APP_VERSION,
                    "screen": "1920x1080 1.0x",
                    "timezone": "Europe/Moscow",
                },
                "deviceId": str(uuid.uuid4()),
            },
        )

    async def send_code(self, phone: str) -> str:
        """Request an SMS code; return the SMS token.

        Raises MaxAuthError with the full server payload if the response
        has no token, so callers can show why (error/captcha/etc.).
        """
        await self._send_hello_packet()
        response = await self.invoke_method(
            opcode=17,
            payload={"phone": phone, "type": "START_AUTH", "language": "ru"},
        )
        payload = response.get("payload", {})
        token = payload.get("token")
        if not token:
            raise MaxAuthError(
                "MAX не выдал токен на запрос кода. Ответ сервера:\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            )
        return token

    async def login_by_token(
        self,
        token: str,
        *,
        chats_sync: int = 0,
        contacts_sync: int = 0,
        chats_count: int = 40,
    ):
        """Log in with a saved login token (opcode 19).

        Reimplemented from vkmax because its version crashes on a logging
        line that reads profile["phone"] - a field absent from the
        token-login response, raising KeyError('phone') after a *successful*
        login.
        """
        await self._send_hello_packet()
        response = await self.invoke_method(
            opcode=19,
            payload={
                "interactive": True,
                "token": token,
                "chatsSync": chats_sync,
                "contactsSync": contacts_sync,
                "presenceSync": 0,
                "draftsSync": 0,
                "chatsCount": chats_count,
            },
        )
        payload = response.get("payload", {})
        if "error" in payload:
            raise MaxAuthError(str(payload["error"]))
        self._is_logged_in = True
        await self._start_keepalive_task()
        _logger.info("Logged in by token.")
        return response

    async def invoke_method(self, opcode: int, payload: dict):
        coro = super().invoke_method(opcode, payload)
        if opcode == 1:
            # Keepalive ping: don't time it out (a slow ping shouldn't kill the
            # keepalive loop); a hung one is cleaned up by _fail_pending().
            return await coro
        return await asyncio.wait_for(coro, timeout=MAX_INVOKE_TIMEOUT)

    def _fail_pending(self) -> None:
        """Reject all in-flight request futures so awaiting callers unblock with
        an exception instead of hanging forever once the socket is gone (the
        recv loop that would resolve them is being torn down)."""
        pending = getattr(self, "_pending", None)
        if not pending:
            return
        for future in list(pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("MAX connection closed"))
        pending.clear()

    async def disconnect(self):
        # vkmax's disconnect() raises if keepalive never started (i.e. the
        # session was never logged in); tear down whatever actually exists
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._recv_task:
            self._recv_task.cancel()
        self._fail_pending()
        if self._connection:
            await self._connection.close()
            self._connection = None
