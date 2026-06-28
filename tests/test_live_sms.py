"""Optional live SMS-login smoke test (default-off, interactive).

Exercises the ``sms`` auth method end to end: a TCP ``Client`` driven by the
Telegram-backed SMS/password providers. The login code is supplied by replying
in the configured Telegram admin chat, so this is inherently interactive.

Marked ``integration`` + ``slow`` so the default mocked suite never runs it;
also skipped unless the credentials are present. Run inside the container only:

    docker compose -f docker-compose.test.yml run --rm `
      -e MAX2TG_LIVE_TESTS=1 `
      -e MAX2TG_MAX_PHONE=$env:MAX2TG_MAX_PHONE `
      -e MAX2TG_TELEGRAM_BOT_TOKEN=$env:MAX2TG_TELEGRAM_BOT_TOKEN `
      -e MAX2TG_TELEGRAM_CHAT_ID=$env:MAX2TG_TELEGRAM_CHAT_ID `
      tests python -m pytest -m integration tests/test_live_sms.py -q -s
"""
import asyncio
import contextlib
import os
import tempfile

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MAX2TG_LIVE_TESTS") != "1"
        or not os.environ.get("MAX2TG_MAX_PHONE")
        or not os.environ.get("MAX2TG_TELEGRAM_BOT_TOKEN")
        or not os.environ.get("MAX2TG_TELEGRAM_CHAT_ID"),
        reason="live sms disabled; needs MAX2TG_LIVE_TESTS=1, MAX2TG_MAX_PHONE, "
               "MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID",
    ),
]

LOGIN_TIMEOUT = 300  # the human has to read the SMS and reply in Telegram


async def _sms_login():
    from max_client import build_max_client
    from maxauth import TelegramAuthPoll

    bot_token = os.environ["MAX2TG_TELEGRAM_BOT_TOKEN"]
    admin_chat_id = int(os.environ["MAX2TG_TELEGRAM_CHAT_ID"])
    # The SMS provider drains the backlog after posting its prompt, so a stale
    # /start is ignored and only a numeric reply typed afterwards is accepted.
    poll = TelegramAuthPoll(bot_token, admin_chat_id)
    config = {
        "max_auth_method": "sms",
        "max_phone": os.environ["MAX2TG_MAX_PHONE"],
        "max_work_dir": tempfile.mkdtemp(prefix="max2tg-live-sms-"),
    }
    client = build_max_client(
        config, bot_token=bot_token, admin_chat_id=admin_chat_id, poll=poll)

    captured: dict = {}
    started = asyncio.Event()

    @client.on_start()
    async def _capture(c):  # noqa: ANN001 - PyMax callback signature
        captured["me"] = c.me
        started.set()

    # Cancel start() externally once login succeeds; calling stop() inside
    # on_start would surface as CancelledError out of start().
    run = asyncio.create_task(client.start())
    waiter = asyncio.create_task(started.wait())
    try:
        await asyncio.wait({run, waiter}, timeout=LOGIN_TIMEOUT,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiter, run):
            task.cancel()
        for task in (waiter, run):
            with contextlib.suppress(BaseException):
                await task
    return captured.get("me")


def test_sms_login():
    me = asyncio.run(_sms_login())
    assert me is not None, "sms login did not populate client.me"
    assert getattr(me, "contact", None) is not None
