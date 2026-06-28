"""Optional live QR-login smoke test (default-off, interactive).

Exercises the ``qr`` auth method: a ``WebClient`` driven by the Telegram QR
handler. The QR image is posted to the configured Telegram admin chat and must
be scanned manually in the MAX mobile app, so this is inherently interactive.

Marked ``integration`` + ``slow`` so the default mocked suite never runs it;
also skipped unless ``MAX2TG_LIVE_QR=1`` and Telegram creds are present. Run
inside the container only:

    docker compose -f docker-compose.test.yml run --rm `
      -e MAX2TG_LIVE_TESTS=1 -e MAX2TG_LIVE_QR=1 `
      -e MAX2TG_TELEGRAM_BOT_TOKEN=$env:MAX2TG_TELEGRAM_BOT_TOKEN `
      -e MAX2TG_TELEGRAM_CHAT_ID=$env:MAX2TG_TELEGRAM_CHAT_ID `
      tests python -m pytest -m integration tests/test_live_qr.py -q -s
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
        or os.environ.get("MAX2TG_LIVE_QR") != "1"
        or not os.environ.get("MAX2TG_TELEGRAM_BOT_TOKEN")
        or not os.environ.get("MAX2TG_TELEGRAM_CHAT_ID"),
        reason="live qr disabled; needs MAX2TG_LIVE_TESTS=1, MAX2TG_LIVE_QR=1, "
               "MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID",
    ),
]

LOGIN_TIMEOUT = 300  # the human has to scan the posted QR in the MAX app


async def _qr_login():
    from max_client import build_max_client
    from maxauth import TelegramAuthPoll

    bot_token = os.environ["MAX2TG_TELEGRAM_BOT_TOKEN"]
    admin_chat_id = int(os.environ["MAX2TG_TELEGRAM_CHAT_ID"])
    poll = TelegramAuthPoll(bot_token, admin_chat_id)
    config = {
        "max_auth_method": "qr",
        "max_work_dir": tempfile.mkdtemp(prefix="max2tg-live-qr-"),
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


def test_qr_login():
    me = asyncio.run(_qr_login())
    assert me is not None, "qr login did not populate client.me"
    assert getattr(me, "contact", None) is not None
