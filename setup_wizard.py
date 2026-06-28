"""Interactive first-run setup: Telegram bot token, chat id, MAX account login."""
import asyncio
import contextlib
import logging
import tempfile
import time

import tg
from config import load_partial, save_config
from max_client import MaxAuthError

_logger = logging.getLogger(__name__)

CHAT_ID_POLL_SECONDS = 120
# How long to wait for the token login to populate the profile.
VALIDATE_TIMEOUT = 60


def _ask(prompt: str) -> str:
    value = ""
    while not value:
        value = input(prompt).strip()
    return value


def _setup_telegram_token() -> str:
    print()
    print("=== Шаг 1. Бот в Telegram (бесплатно) ===")
    print("1. Откройте Telegram и найдите @BotFather")
    print("2. Отправьте ему /newbot, придумайте имя и username бота")
    print("3. BotFather пришлёт токен вида 123456789:AAE3f...")
    print()
    while True:
        token = _ask("Вставьте токен бота: ")
        try:
            bot = tg.check_token(token)
            print(f"OK: бот @{bot.get('username')} найден.")
            return token
        except Exception as exc:
            print(f"Токен не подошёл ({exc}). Попробуйте ещё раз.")


def _setup_telegram_chat_id(token: str) -> int:
    print()
    print("=== Шаг 2. Привязка вашего Telegram ===")
    print("Откройте чат с вашим новым ботом и отправьте ему /start")
    print(f"Жду сообщение (до {CHAT_ID_POLL_SECONDS} секунд)...")
    deadline = time.monotonic() + CHAT_ID_POLL_SECONDS
    offset = None
    while time.monotonic() < deadline:
        try:
            updates = tg.get_updates(token, offset)
        except Exception as exc:
            print(f"Ошибка опроса Telegram: {exc}; повтор через 3 с")
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message and "chat" in message:
                chat = message["chat"]
                name = chat.get("first_name") or chat.get("username") or chat["id"]
                print(f"OK: получен chat_id от «{name}».")
                return chat["id"]
    raise SystemExit("Не дождался /start. Запустите настройку заново.")


CONSOLE_SNIPPET = "copy(JSON.parse(localStorage.__oneme_auth).token)"

MAX_LOGIN_INSTRUCTIONS = f"""=== Шаг 3. Вход в ваш аккаунт MAX ===
Прямой вход по SMS сейчас закрыт капчей MAX, поэтому берём токен из
веб-версии (это бесплатно и делается один раз):

1. Откройте в браузере https://web.max.ru и войдите в свой аккаунт
   (введите номер, пройдите капчу, введите код из SMS) - как обычно.
2. На странице MAX нажмите F12 (откроется панель разработчика),
   перейдите на вкладку "Console" (Консоль).
3. Вставьте туда эту строку и нажмите Enter:

   {CONSOLE_SNIPPET}

   В ответ консоль выведет 'undefined' - это нормально, токен уже
   скопирован в буфер обмена.
4. Вернитесь сюда и вставьте токен (Ctrl+V или правый клик -> Вставить).
"""


def _profile_name(me) -> str | None:
    contact = getattr(me, "contact", None)
    names = getattr(contact, "names", None) or []
    if names:
        entry = names[0]
        return (getattr(entry, "name", None)
                or getattr(entry, "first_name", None))
    return None


async def _validate_token(token: str) -> str:
    """Log in with the token via PyMax to confirm it works; return it.

    Runs a throwaway WebClient (temp session, reconnect off) as a task and waits
    for its on_start to capture the profile, then cancels the task. We must not
    call ``stop()`` from inside on_start: that cancels the recv task that
    ``start()`` is awaiting in ``wait_closed()`` and surfaces as a (BaseException)
    CancelledError out of ``start()``. An invalid token never fires on_start, so
    ``me`` stays None.
    """
    from pymax import ExtraConfig, WebClient

    captured: dict = {}
    started = asyncio.Event()
    work_dir = tempfile.mkdtemp(prefix="max2tg-validate-")
    client = WebClient(
        session_name="validate.db",
        work_dir=work_dir,
        extra_config=ExtraConfig(token=token, reconnect=False),
    )

    @client.on_start()
    async def _capture(c):  # noqa: ANN001 - PyMax callback signature
        captured["me"] = c.me
        started.set()

    run = asyncio.create_task(client.start())
    waiter = asyncio.create_task(started.wait())
    try:
        # Resolve as soon as either login succeeds (on_start → started) or the
        # client gives up (run finishes, e.g. a rejected token).
        await asyncio.wait({run, waiter}, timeout=VALIDATE_TIMEOUT,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiter, run):
            task.cancel()
        for task in (waiter, run):
            with contextlib.suppress(BaseException):
                await task

    me = captured.get("me")
    if me is None:
        raise MaxAuthError("MAX не принял токен")
    print(f"OK: вход выполнен ({_profile_name(me) or 'аккаунт'}).")
    return token


async def _setup_max_login() -> str:
    print()
    print(MAX_LOGIN_INSTRUCTIONS)
    while True:
        token = _ask("Вставьте токен MAX: ")
        try:
            return await _validate_token(token)
        except Exception as exc:
            print()
            print(f"Токен не подошёл: {exc}")
            print("Убедитесь, что вы вошли на web.max.ru и скопировали токен "
                  "целиком. Попробуйте ещё раз.")
            print()


def run_setup() -> dict:
    """Walk the user through full setup; returns the saved config.

    Telegram credentials are persisted before the MAX step, so if MAX login
    fails the next run resumes straight at step 3.
    """
    existing = load_partial()
    tg_token = existing.get("telegram_bot_token")
    chat_id = existing.get("telegram_chat_id")

    if tg_token and chat_id:
        print(f"Найдены сохранённые данные Telegram (chat {chat_id}) - "
              "пропускаю шаги 1-2.")
    else:
        tg_token = _setup_telegram_token()
        chat_id = _setup_telegram_chat_id(tg_token)
        save_config({"telegram_bot_token": tg_token, "telegram_chat_id": chat_id})

    max_token = asyncio.run(_setup_max_login())

    config = {
        "telegram_bot_token": tg_token,
        "telegram_chat_id": chat_id,
        "max_login_token": max_token,
    }
    save_config(config)
    print()
    print("Настройка завершена, токены сохранены в config.json.")
    tg.send_message(tg_token, chat_id,
                    "Мост MAX -> Telegram настроен. Новые сообщения из MAX "
                    "будут приходить сюда.")
    return config
