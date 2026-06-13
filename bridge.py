"""MAX <-> Telegram bridge.

MAX -> Telegram: forwards incoming messages (text + attachments) to a Telegram
chat. Telegram -> MAX: when the user *replies* (Reply/свайп) to a forwarded
message in Telegram, the reply text is sent back to the originating MAX chat.
"""
import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path

from vkmax.client import MaxClient
from vkmax.functions.messages import reply_message as max_reply
from vkmax.functions.messages import send_message as max_send
from vkmax.functions.users import resolve_users

import attaches
import mediamax
import tg
from max_client import BrowserMaxClient, MaxAuthError

_logger = logging.getLogger(__name__)

INCOMING_MESSAGE_OPCODE = 128
RECONNECT_DELAY_SECONDS = 15
REPLY_MAP_LIMIT = 10000
# Telegram bots can upload at most 50 MB; leave headroom.
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024
ATTACH_DEBUG_LOG = Path(__file__).parent / "attaches.log"
ATTACH_DEBUG_LOG_MAX_BYTES = 5 * 1024 * 1024

# kind -> (tg function, supports_caption)
_MEDIA_SENDERS = {
    "photo": (tg.send_photo, True),
    "animation": (tg.send_animation, True),
    "video": (tg.send_video, True),
    "voice": (tg.send_voice, True),
    "audio": (tg.send_audio, True),
    "document": (tg.send_document, True),
    "sticker": (tg.send_sticker, False),
}


def _extract_own_id(login_response: dict) -> int | None:
    profile = login_response.get("payload", {}).get("profile", {})
    for candidate in (profile.get("contact", {}).get("id"), profile.get("id")):
        if isinstance(candidate, int):
            return candidate
    return None


def _contact_display_name(contact: dict) -> str | None:
    names = contact.get("names")
    if isinstance(names, list) and names and isinstance(names[0], dict):
        name = names[0].get("name")
        if name:
            return name
    first = contact.get("firstName", "")
    last = contact.get("lastName", "")
    full = f"{first} {last}".strip()
    return full or contact.get("name") or None


def _log_raw_attaches(message: dict) -> None:
    """Append raw attaches to a log so unsupported types can be refined later.

    Capped so a flood of attachment messages can't fill the disk; attach
    payloads may contain signed CDN URLs, so we keep the file small.
    """
    try:
        if (ATTACH_DEBUG_LOG.exists()
                and ATTACH_DEBUG_LOG.stat().st_size > ATTACH_DEBUG_LOG_MAX_BYTES):
            return
        with ATTACH_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.get("attaches"), ensure_ascii=False) + "\n")
    except OSError:
        pass


class MaxToTelegramBridge:
    def __init__(self, config: dict):
        self._config = config
        self._token = config["telegram_bot_token"]
        self._chat_id = config["telegram_chat_id"]
        self._own_id: int | None = None
        self._name_cache: dict[int, str] = {}
        self._client: MaxClient | None = None
        # telegram message_id -> {"chat_id", "message_id", "sender"}
        self._reply_map: "OrderedDict[int, dict]" = OrderedDict()

    # --- helpers -------------------------------------------------------------

    def _remember(self, tg_message_id: int | None, max_chat_id, max_message_id,
                  sender: str) -> None:
        if not tg_message_id:
            return
        self._reply_map[tg_message_id] = {
            "chat_id": max_chat_id,
            "message_id": max_message_id,
            "sender": sender,
        }
        while len(self._reply_map) > REPLY_MAP_LIMIT:
            self._reply_map.popitem(last=False)

    async def _resolve_sender_name(self, client: MaxClient, sender_id: int) -> str:
        if sender_id in self._name_cache:
            return self._name_cache[sender_id]
        name = str(sender_id)
        try:
            response = await resolve_users(client, [sender_id])
            for contact in response.get("payload", {}).get("contacts", []):
                display = _contact_display_name(contact)
                if display:
                    name = display
                    break
        except Exception as exc:
            _logger.warning("Could not resolve user %s: %s", sender_id, exc)
        self._name_cache[sender_id] = name
        return name

    # --- MAX -> Telegram -----------------------------------------------------

    async def _on_packet(self, client: MaxClient, packet: dict) -> None:
        try:
            if packet.get("opcode") != INCOMING_MESSAGE_OPCODE:
                return
            payload = packet.get("payload", {})
            message = payload.get("message", {})
            sender_id = message.get("sender")
            if sender_id is not None and sender_id == self._own_id:
                return  # our own outgoing message echoed back

            chat_id = payload.get("chatId")
            max_message_id = message.get("id")
            text = (message.get("text") or "").strip()
            parsed = attaches.parse(message)
            if message.get("attaches"):
                _log_raw_attaches(message)

            sender = (await self._resolve_sender_name(client, sender_id)
                      if isinstance(sender_id, int) else "неизвестный отправитель")
            header = f"MAX | {sender} (чат {chat_id})"

            await self._forward(client, header, text, parsed,
                                chat_id, max_message_id, sender)
            _logger.info("Forwarded from %s (chat %s, %d attach)",
                         sender, chat_id, len(parsed))
        except Exception:
            _logger.exception("Failed to handle packet: %s", packet)

    async def _forward(self, client, header, text, parsed,
                       chat_id, max_message_id, sender):
        resolvable = {"file_resolve", "video_resolve"}
        media = [p for p in parsed if p.kind in _MEDIA_SENDERS]
        to_resolve = [p for p in parsed if p.kind in resolvable]
        notes = [p.text for p in parsed
                 if p.kind not in _MEDIA_SENDERS and p.kind not in resolvable]

        ctx = (client, header, chat_id, max_message_id, sender)
        header_sent = False
        # A leading text message when there is text, notes, or nothing else.
        if text or notes or (not media and not to_resolve):
            body = "\n".join(part for part in [header, text, *notes] if part) or header
            msg_id = await asyncio.to_thread(tg.send_message, self._token,
                                             self._chat_id, body)
            self._remember(msg_id, chat_id, max_message_id, sender)
            header_sent = True

        for item in media:
            header_sent = await self._send_media_item(item, header_sent, ctx)
        for item in to_resolve:
            header_sent = await self._send_resolved_item(item, header_sent, ctx)

    @staticmethod
    def _caption(header, header_sent, item_text):
        return item_text if header_sent else f"{header}\n{item_text}"

    async def _send_media_item(self, item, header_sent, ctx) -> bool:
        _client, header, chat_id, max_message_id, sender = ctx
        caption = self._caption(header, header_sent, item.text)
        sender_fn, supports_caption = _MEDIA_SENDERS[item.kind]
        try:
            if supports_caption:
                msg_id = await asyncio.to_thread(
                    sender_fn, self._token, self._chat_id, item.url, caption)
            else:
                msg_id = await asyncio.to_thread(
                    sender_fn, self._token, self._chat_id, item.url)
        except Exception as exc:
            _logger.warning("Failed to send %s: %s", item.kind, exc)
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, self._chat_id,
                f"{caption} [не удалось переслать медиа]")
        self._remember(msg_id, chat_id, max_message_id, sender)
        return True

    async def _send_resolved_item(self, item, header_sent, ctx) -> bool:
        """Resolve a file/video to a temporary URL, then upload it to Telegram."""
        client, header, chat_id, max_message_id, sender = ctx
        caption = self._caption(header, header_sent, item.text)
        if item.size and item.size > TELEGRAM_UPLOAD_LIMIT:
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, self._chat_id,
                f"{caption} [слишком большой для Telegram] — открыть в MAX")
            self._remember(msg_id, chat_id, max_message_id, sender)
            return True
        try:
            if item.kind == "file_resolve":
                url = await mediamax.resolve_file_url(
                    client, item.file_id, chat_id, max_message_id)
                msg_id = await asyncio.to_thread(
                    tg.send_document, self._token, self._chat_id, url,
                    caption, item.filename)
            else:  # video_resolve
                url = await mediamax.resolve_video_url(
                    client, item.video_id, chat_id, max_message_id)
                msg_id = await asyncio.to_thread(
                    tg.send_video, self._token, self._chat_id, url, caption)
        except Exception as exc:
            _logger.warning("Failed to resolve/send %s: %s", item.kind, exc)
            msg_id = await asyncio.to_thread(
                tg.send_message, self._token, self._chat_id,
                f"{caption} — открыть в MAX")
        self._remember(msg_id, chat_id, max_message_id, sender)
        return True

    # --- Telegram -> MAX -----------------------------------------------------

    async def _send_reply_to_max(self, target: dict, text: str) -> None:
        if self._client is None:
            await asyncio.to_thread(
                tg.send_message, self._token, self._chat_id,
                "⚠️ MAX сейчас не подключён, ответ не отправлен. Повторите позже.")
            return
        chat_id = target["chat_id"]
        message_id = target.get("message_id")
        if message_id is not None:
            await max_reply(self._client, chat_id, text, message_id)
        else:
            await max_send(self._client, chat_id, text)
        await asyncio.to_thread(
            tg.send_message, self._token, self._chat_id,
            f"✅ Отправлено в MAX → {target.get('sender', 'чат')}")

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        # Only accept commands from the configured owner chat (tolerate the id
        # being stored/sent as int vs str).
        incoming_chat = message.get("chat", {}).get("id")
        if str(incoming_chat) != str(self._chat_id):
            return
        text = message.get("text")
        if not text:
            return
        reply = message.get("reply_to_message")
        target = self._reply_map.get(reply.get("message_id")) if reply else None
        if target:
            await self._send_reply_to_max(target, text)
        else:
            await asyncio.to_thread(
                tg.send_message, self._token, self._chat_id,
                "ℹ️ Чтобы ответить в MAX, сделайте «Ответить» (Reply / свайп) "
                "на пересланном сообщении и напишите текст.")

    async def _poll_telegram(self) -> None:
        """Long-poll Telegram for replies; skip the backlog on startup."""
        offset = None
        try:
            backlog = await asyncio.to_thread(tg.get_updates, self._token, None, 0)
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        except Exception as exc:
            _logger.warning("Telegram backlog drain failed: %s", exc)
        while True:
            try:
                updates = await asyncio.to_thread(tg.get_updates, self._token, offset, 25)
            except Exception as exc:
                _logger.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self._handle_update(update)
                except Exception:
                    _logger.exception("Failed to handle Telegram update")

    # --- MAX session lifecycle ----------------------------------------------

    async def _run_session(self) -> None:
        client = BrowserMaxClient()
        await client.connect()
        try:
            login_response = await client.login_by_token(self._config["max_login_token"])
            self._own_id = _extract_own_id(login_response)
            self._client = client
            await client.set_callback(self._on_packet)
            _logger.info("Bridge online (own id: %s).", self._own_id)
            print("Мост запущен. Сообщения MAX идут в Telegram; ответы — через Reply.")
            await client._connection.wait_closed()
            _logger.warning("MAX connection closed by server.")
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _max_loop(self) -> None:
        while True:
            try:
                await self._run_session()
            except MaxAuthError as exc:
                _logger.error("MAX auth failed: %s", exc)
                print("Похоже, токен MAX устарел. Удалите config.json и "
                      "пройдите настройку заново.")
            except Exception as exc:
                _logger.error("Session error: %s", exc)
            _logger.info("Reconnecting in %s seconds...", RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def run_forever(self) -> None:
        await asyncio.gather(self._max_loop(), self._poll_telegram())
