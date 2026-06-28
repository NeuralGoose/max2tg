"""Optional live smoke tests for the ``token`` auth method (default-off).

These are the runtime proof that a pasted web LOGIN token is accepted by PyMax
(Doc 2 §0 caveat). They are marked ``integration`` so the default mocked suite
(``-m "not integration"``) never runs them, and additionally skipped unless the
credentials are present.

Run inside the container only (no host execution):

    docker compose -f docker-compose.test.yml run --rm `
      -e MAX2TG_LIVE_TESTS=1 `
      -e MAX2TG_MAX_TOKEN=$env:MAX2TG_MAX_TOKEN `
      tests python -m pytest -m integration tests/test_live_smoke.py -q
"""
import asyncio
import contextlib
import os
import tempfile

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MAX2TG_LIVE_TESTS") != "1"
        or not os.environ.get("MAX2TG_MAX_TOKEN"),
        reason="live token smoke disabled; set MAX2TG_LIVE_TESTS=1 and MAX2TG_MAX_TOKEN",
    ),
]

LOGIN_TIMEOUT = 60


async def _login_capture(client, timeout, on_started=None):
    """Run client.start() as a task, capture client.me when on_start fires, then
    cancel the task. Calling stop() from inside on_start would cancel the recv
    task start() awaits and surface as CancelledError, so we cancel externally."""
    captured: dict = {}
    started = asyncio.Event()

    @client.on_start()
    async def _capture(c):  # noqa: ANN001 - PyMax callback signature
        captured["me"] = c.me
        if on_started is not None:
            captured["result"] = await on_started(c)
        started.set()

    run = asyncio.create_task(client.start())
    waiter = asyncio.create_task(started.wait())
    try:
        await asyncio.wait({run, waiter}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiter, run):
            task.cancel()
        for task in (waiter, run):
            with contextlib.suppress(BaseException):
                await task
    return captured


def _new_client(work_prefix):
    from pymax import ExtraConfig, WebClient

    return WebClient(
        session_name="live-smoke.db",
        work_dir=tempfile.mkdtemp(prefix=work_prefix),
        extra_config=ExtraConfig(
            token=os.environ["MAX2TG_MAX_TOKEN"], reconnect=False),
    )


def test_login_and_me():
    captured = asyncio.run(
        _login_capture(_new_client("max2tg-live-token-"), LOGIN_TIMEOUT))
    me = captured.get("me")
    assert me is not None, "token login did not populate client.me"
    assert getattr(me, "contact", None) is not None


@pytest.mark.skipif(
    not os.environ.get("MAX2TG_LIVE_MAX_CHAT_ID"),
    reason="send roundtrip needs MAX2TG_LIVE_MAX_CHAT_ID (a disposable MAX chat)",
)
def test_send_roundtrip():
    """Send one message to a known MAX chat after login."""
    chat_id = int(os.environ["MAX2TG_LIVE_MAX_CHAT_ID"])

    async def _send(c):
        return await c.send_message(chat_id, "max2tg live smoke test ✅")

    captured = asyncio.run(
        _login_capture(_new_client("max2tg-live-send-"), LOGIN_TIMEOUT,
                       on_started=_send))
    assert captured.get("me") is not None, "login failed before send"
    assert captured.get("result") is not None, "send_message returned nothing"
