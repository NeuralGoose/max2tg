"""Telegram-command -> MAX actions: join chats/channels, find people. (/dm pending.)

Thin wrappers over the vkmax / MAX opcodes with link parsing and defensive
response handling, so the bridge can expose /join and /find from Telegram. Every
public coroutine returns a CommandResult (never raises).

Opcodes (vkmax / verified against PyMax MaxTeamAPI):
  57  join/resolve by link (channels: https://max.ru/<name>; groups: join/<hash>)
  89  resolve by link (read-only lookup)
  75  subscribe to a chat
  46  find contact by phone (CONTACT_INFO_BY_PHONE) -> payload.contact
  32  resolve users by id (vkmax resolve_users)

NOT wired:
- Free-text name search (opcode 60 PUBLIC_SEARCH): payload schema unconfirmed; a
  bad payload makes MAX drop the socket (proto.payload), killing the live bridge.
- /dm by user_id: a MAX dialog's chatId is NOT the peer's user_id (verified live:
  resolve_users(dialogChatId) returns a different/unknown user). So send_message
  to a user_id reaches the wrong chat (or not.found). Disabled until the
  "open dialog by user_id" opcode is found. Replying to a *forwarded* message
  still works — that path uses the real dialog chatId from the bridge's reply_map.
"""
import logging
import re
from dataclasses import dataclass

from vkmax.functions.users import resolve_users

_logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")
_MAX_QUERY_LEN = 64


@dataclass
class CommandResult:
    """A command's Telegram reply text. (No send target is carried: a user-id is
    NOT a dialog chatId in MAX, so it must never become a send destination.)"""
    text: str


def _short(value, limit: int = 200) -> str:
    """Clamp a third-party string (MAX error / exception) before echoing it."""
    return str(value)[:limit]


def _norm_link(raw: str) -> str | None:
    """MAX link payload (opcode 57/89) from a raw string: a group invite
    (join/<hash>), a max.ru/<name> link, or a bare @username."""
    s = raw.strip()
    # Match a join hash only as a path segment (string start or after '/'), so a
    # query like 'max.ru/news?ref=join/x' isn't misread as a group invite.
    m = re.search(r"(?:^|/)join/([A-Za-z0-9_-]+)", s)
    if m:
        return f"join/{m.group(1)}"
    m = re.search(r"max\.ru/([A-Za-z0-9_.]+)", s)
    if m:
        return f"https://max.ru/{m.group(1)}"
    s = s.lstrip("@")
    if _USERNAME_RE.match(s):
        return f"https://max.ru/{s}"
    return None


def _display(contact: dict) -> str | None:
    names = contact.get("names")
    if isinstance(names, list):
        for n in names:
            if isinstance(n, dict):
                full = f"{n.get('firstName', '')} {n.get('lastName', '')}".strip()
                if full:
                    return full
                if n.get("name"):
                    return str(n["name"]).strip()
    return (contact.get("name") or "").strip() or None


def _chat_from_payload(payload: dict):
    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else payload
    chat_id = chat.get("id") or chat.get("chatId")
    title = (chat.get("title") or chat.get("name") or "").strip()
    return chat_id, title


def _normalize_phone(s: str) -> str | None:
    """E.164-ish phone from user input, or None if implausible. Maps a Russian
    local '8XXXXXXXXXX' to '+7XXXXXXXXXX'."""
    digits = re.sub(r"\D", "", s)
    if not 7 <= len(digits) <= 15:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return "+" + digits


async def join(client, raw: str) -> CommandResult:
    """Join a MAX channel/group/chat by link or @username (opcode 57 + subscribe)."""
    link = _norm_link(raw)
    if not link:
        return CommandResult(
            "🤔 Не похоже на ссылку. Пришлите ссылку вида max.ru/имя или @username.\n"
            "Пример: /join https://max.ru/join/AbCdEf")
    try:
        data = await client.invoke_method(opcode=57, payload={"link": link})
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        if "error" in payload:
            return CommandResult(f"⚠️ MAX не дал вступить: {_short(payload.get('error'))}")
        chat_id, title = _chat_from_payload(payload)
        if chat_id is not None:
            try:
                await client.invoke_method(
                    opcode=75, payload={"chatId": chat_id, "subscribe": True})
            except Exception as exc:
                _logger.warning("subscribe after join %s: %s", chat_id, exc)
        name = title or (f"чат {chat_id}" if chat_id else "чат")
        return CommandResult(
            f"✅ Готово, вы вступили: {name}\n"
            "Чат появится отдельной темой, как только придёт первое сообщение.")
    except Exception as exc:
        _logger.warning("join failed: %s", exc)
        return CommandResult(f"⚠️ Не удалось вступить: {_short(exc)}")


async def find(client, query: str) -> CommandResult:
    """Find a person by phone (46) or id (32), or a channel/person by @username/
    link (89). Free-text name search isn't available."""
    s = query.strip()
    if len(s) > _MAX_QUERY_LEN:
        return CommandResult("⚠️ Слишком длинный запрос для поиска.")
    phone_digits = re.sub(r"\D", "", s)
    is_phone = (re.fullmatch(r"[+\d\s()\-]+", s) is not None
                and (s.startswith("+") or len(phone_digits) >= 11
                     or (bool(re.search(r"[+\s()\-]", s)) and len(phone_digits) >= 7)))
    if is_phone:
        phone = _normalize_phone(s)
        if not phone:
            return CommandResult("🔍 Похоже на телефон, но номер неполный. Пример: +79991234567")
        try:
            data = await client.invoke_method(opcode=46, payload={"phone": phone})
            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            contact = payload.get("contact")
            if payload.get("error") or not isinstance(contact, dict):
                return CommandResult(f"🔍 По номеру {phone} никто не найден.")
            return CommandResult(
                f"🔍 Нашёл: {_display(contact) or contact.get('id')}\n🆔 id: {contact.get('id')}")
        except Exception as exc:
            return CommandResult(f"⚠️ Ошибка поиска по телефону: {_short(exc)}")
    if s.lstrip("-").isdigit():
        try:
            data = await resolve_users(client, [int(s)])
            contacts = data.get("payload", {}).get("contacts", []) if isinstance(data, dict) else []
            if not contacts:
                return CommandResult(f"🔍 Человек с id {s} не найден.")
            return CommandResult(f"🔍 Нашёл: {_display(contacts[0]) or s}\n🆔 id: {s}")
        except Exception as exc:
            return CommandResult(f"⚠️ Ошибка поиска: {_short(exc)}")
    link = _norm_link(s)
    if not link:
        return CommandResult(
            "🔍 Поиск по названию (свободный текст) MAX через бота пока недоступен.\n"
            "Ищите по: телефону (+7…), @нику, ссылке max.ru/… или числовому id.")
    try:
        data = await client.invoke_method(opcode=89, payload={"link": link})
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        chat_id, title = _chat_from_payload(payload)
        if chat_id is None:
            return CommandResult(f"🔍 Ничего не найдено по «{s}».")
        return CommandResult(f"🔍 Нашёл: {title or s}\n🆔 id: {chat_id}\nВступить: /join {s}")
    except Exception as exc:
        return CommandResult(f"⚠️ Ошибка поиска: {_short(exc)}")


async def start_dm(client, user_id: str, text: str) -> CommandResult:
    """DISABLED: a MAX dialog's chatId is not the peer user_id (verified live), so
    sending by id could reach the wrong person. Sends nothing — returns only an
    explanation — until the open-dialog-by-id opcode is found."""
    return CommandResult(
        "🚧 /dm пока отключён.\n"
        "Выяснилось, что в MAX номер диалога не равен id пользователя, поэтому "
        "отправка по id могла бы уйти не тому человеку — я это отключил ради "
        "безопасности.\n\n"
        "Чтобы написать в существующий чат MAX — ответьте (Reply/свайп) на его "
        "пересланном сообщении: это идёт точно в нужный чат.")
