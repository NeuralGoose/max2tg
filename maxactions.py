"""Telegram-command -> MAX actions: join chats/channels, find people, /dm.

Thin wrappers over PyMax's typed client methods with link parsing and defensive
error handling, so the bridge can expose /join, /find and /dm from Telegram.
Every public coroutine returns a CommandResult and never raises.

PyMax methods used (see Document 2 §4):
  join_group(link)            group invite links (max.ru/join/<hash>)
  join_channel(link)          channel/username links (max.ru/<name>)
  resolve_group_by_link(link) read-only lookup of a group invite link
  search_by_phone(phone)      find a contact by phone -> User
  get_user(id)                resolve a user by numeric id -> User | None
  get_chat_id(a, b)           deterministic 1:1 dialog id (XOR), no network
  send_message(chat_id, text) send into that dialog

Notes:
- Free-text name search (PUBLIC_SEARCH) stays unwired: the payload schema is
  unconfirmed and a bad request drops the MAX socket.
- /find by a channel/@username link is read-only-resolvable only for group
  invite (join/) links in PyMax; for channels we point the user at /join.
"""
import logging
import re
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")
_MAX_QUERY_LEN = 64


@dataclass
class CommandResult:
    """A command's Telegram reply text. (No send target is carried: a user-id is
    NOT a dialog chatId in MAX, so it must never become a send destination.)"""
    text: str
    outbound_chat_id: int | None = None
    outbound_message_id: int | str | None = None


def _short(value, limit: int = 200) -> str:
    """Clamp a third-party string (MAX error / exception) before echoing it."""
    return str(value)[:limit]


def _norm_link(raw: str) -> str | None:
    """MAX join link from a raw string: a group invite (join/<hash>), a
    max.ru/<name> link, or a bare @username (-> https://max.ru/<name>)."""
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


def _user_display(user) -> str | None:
    """Display name from a PyMax User.names (list[Name]); prefer first+last."""
    for entry in getattr(user, "names", None) or []:
        first = getattr(entry, "first_name", None) or ""
        last = getattr(entry, "last_name", None) or ""
        full = f"{first} {last}".strip()
        if full:
            return full
        name = getattr(entry, "name", None)
        if name:
            return str(name).strip()
    return None


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
    """Join a MAX channel/group by link or @username (PyMax join_group/channel)."""
    link = _norm_link(raw)
    if not link:
        return CommandResult(
            "🤔 Не похоже на ссылку. Пришлите ссылку вида max.ru/имя или @username.\n"
            "Пример: /join https://max.ru/join/AbCdEf")
    try:
        if link.startswith("join/"):
            chat = await client.join_group(link)
        else:
            chat = await client.join_channel(link)
    except Exception as exc:
        _logger.warning("join failed: %s", exc)
        return CommandResult(f"⚠️ Не удалось вступить: {_short(exc)}")
    title = (getattr(chat, "title", None) or "").strip()
    chat_id = getattr(chat, "id", None)
    name = title or (f"чат {chat_id}" if chat_id else "чат")
    return CommandResult(
        f"✅ Готово, вы вступили: {name}\n"
        "Чат появится отдельной темой, как только придёт первое сообщение.")


async def find(client, query: str) -> CommandResult:
    """Find a person by phone (search_by_phone) or numeric id (get_user), or a
    group by invite link (resolve_group_by_link). Free-text name search isn't
    available; channel/@username links are pointed to /join."""
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
            user = await client.search_by_phone(phone)
        except Exception as exc:
            _logger.info("phone search %s: %s", phone, exc)
            return CommandResult(f"🔍 По номеру {phone} никто не найден.")
        if not user:
            return CommandResult(f"🔍 По номеру {phone} никто не найден.")
        return CommandResult(
            f"🔍 Нашёл: {_user_display(user) or user.id}\n🆔 id: {user.id}")
    if s.lstrip("-").isdigit():
        try:
            user = await client.get_user(int(s))
        except Exception as exc:
            return CommandResult(f"⚠️ Ошибка поиска: {_short(exc)}")
        if not user:
            return CommandResult(f"🔍 Человек с id {s} не найден.")
        return CommandResult(f"🔍 Нашёл: {_user_display(user) or s}\n🆔 id: {s}")
    link = _norm_link(s)
    if not link:
        return CommandResult(
            "🔍 Поиск по названию (свободный текст) MAX через бота пока недоступен.\n"
            "Ищите по: телефону (+7…), @нику, ссылке max.ru/… или числовому id.")
    if not link.startswith("join/"):
        # PyMax's read-only resolve only accepts group invite (join/) links.
        return CommandResult(
            f"🔍 Канал/пользователя по ссылке смотрите через вступление: /join {s}")
    try:
        chat = await client.resolve_group_by_link(link)
    except Exception as exc:
        return CommandResult(f"⚠️ Ошибка поиска: {_short(exc)}")
    if chat is None:
        return CommandResult(f"🔍 Ничего не найдено по «{s}».")
    title = (getattr(chat, "title", None) or "").strip()
    chat_id = getattr(chat, "id", None)
    return CommandResult(f"🔍 Нашёл: {title or s}\n🆔 id: {chat_id}\nВступить: /join {s}")


async def start_dm(client, user_id: str, text: str) -> CommandResult:
    """Message a person by their numeric user_id (the id from /find). Computes the
    1:1 dialog id via PyMax get_chat_id(my_id, uid) and sends into it; the peer's
    reply then arrives as its own topic."""
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return CommandResult(
            "⚠️ Нужен числовой id (как из 🔍 /find). Пример: /dm 21243808 привет")
    body = (text or "").strip()
    if not body:
        return CommandResult("⚠️ Пустое сообщение. Пример: /dm 21243808 привет")
    if len(body) > 4000:
        return CommandResult("⚠️ Слишком длинное сообщение (макс. 4000 символов).")
    my_id = getattr(getattr(getattr(client, "me", None), "contact", None), "id", None)
    if my_id is None:
        return CommandResult("⏳ MAX ещё подключается — попробуйте через минуту.")
    try:
        chat_id = client.get_chat_id(my_id, uid)
        sent = await client.send_message(chat_id, body)
    except Exception as exc:
        _logger.warning("start_dm to %s failed: %s", user_id, exc)
        return CommandResult(f"⚠️ Не удалось отправить: {_short(exc)}")
    outbound_id = getattr(sent, "id", None) if sent is not None else None
    return CommandResult(
        f"✅ Отправлено! Диалог с человеком (id {uid}) создан — его ответ "
        "придёт отдельной темой, дальше переписывайтесь там.",
        outbound_chat_id=chat_id if outbound_id is not None else None,
        outbound_message_id=outbound_id,
    )
