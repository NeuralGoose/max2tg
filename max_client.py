"""MAX client factory.

Builds the right PyMax client for the configured auth method (token / sms / qr)
and wires in the Telegram-backed interactive auth providers from ``maxauth``.

- ``token`` -> ``WebClient`` seeded with the saved web LOGIN token (no prompts)
- ``qr``    -> ``WebClient`` with Telegram QR handler + Telegram 2FA password
- ``sms``   -> ``Client`` (TCP) driven by Telegram SMS/password providers

The session is persisted to a SQLite store under ``work_dir`` so a restart does
not force a fresh login.
"""
import logging
import os
from pathlib import Path

from pymax import Client, ExtraConfig, PyMaxError, WebClient

_logger = logging.getLogger(__name__)

# Optional outbound proxy for the MAX connection, e.g. when running on a foreign
# server that MAX geo-blocks. Set to a Russian SOCKS/HTTP proxy URL, e.g.
# "socks5://user:pass@host:1080" or "http://host:3128".
WS_PROXY = os.environ.get("MAX2TG_WS_PROXY") or None


def _default_work_dir() -> str:
    """Where PyMax keeps its SQLite session. Inside the container /data is a
    mounted volume so the session survives restarts. Off Docker /data usually
    doesn't exist (or isn't writable), so fall back to a local ./data dir next
    to the scripts instead of failing a bare `python main.py` run."""
    data = Path("/data")
    try:
        if data.is_dir() and os.access(data, os.W_OK):
            return str(data)
    except OSError:
        pass
    return str(Path(__file__).resolve().parent / "data")


# Defaults for the PyMax session store (SQLite).
DEFAULT_WORK_DIR = _default_work_dir()
DEFAULT_SESSION_DB = "max.db"

# Back-compat alias: setup_wizard raises/catches MaxAuthError on a bad login.
# PyMax raises PyMaxError (ApiError is a subclass) for login/API failures.
MaxAuthError = PyMaxError


def _build_extra_config(config: dict, *, token: str | None = None) -> ExtraConfig:
    """Build a PyMax ExtraConfig from bridge config, carrying over the proxy."""
    kwargs: dict = {"reconnect": True}
    if token:
        kwargs["token"] = token
    if WS_PROXY:
        kwargs["proxy"] = WS_PROXY
    return ExtraConfig(**kwargs)


def build_max_client(config: dict, *, bot_token: str | None = None,
                     admin_chat_id=None, poll=None):
    """Construct the right PyMax client for the configured auth method.

    ``bot_token``/``admin_chat_id``/``poll`` are only needed for the interactive
    methods (sms/qr); ``poll`` is the shared TelegramAuthPoll for sms/qr replies.
    """
    method = (config.get("max_auth_method") or "token").strip().lower()
    work_dir = config.get("max_work_dir") or os.environ.get(
        "MAX2TG_WORK_DIR") or DEFAULT_WORK_DIR
    session_name = config.get("max_session_db") or os.environ.get(
        "MAX2TG_SESSION_DB") or DEFAULT_SESSION_DB

    if method == "token":
        token = config.get("max_login_token")
        return WebClient(
            session_name=session_name,
            work_dir=work_dir,
            extra_config=_build_extra_config(config, token=token),
        )

    if method == "qr":
        from maxauth import (TelegramAuthPoll, TelegramPasswordProvider,
                             TelegramQrHandler)
        from pymax import QrAuthFlow

        if poll is None:
            poll = TelegramAuthPoll(bot_token, admin_chat_id)
        auth_flow = QrAuthFlow(
            TelegramQrHandler(bot_token, admin_chat_id),
            TelegramPasswordProvider(bot_token, admin_chat_id, poll),
        )
        return WebClient(
            session_name=session_name,
            work_dir=work_dir,
            extra_config=_build_extra_config(config),
            auth_flow=auth_flow,
        )

    if method == "sms":
        from maxauth import (TelegramAuthPoll, TelegramPasswordProvider,
                             TelegramSmsCodeProvider)

        if poll is None:
            # Mirror the qr branch: the SMS/password providers need a poll to
            # read the reply; without one they would crash on poll.drain().
            poll = TelegramAuthPoll(bot_token, admin_chat_id)
        return Client(
            phone=config["max_phone"],
            session_name=session_name,
            work_dir=work_dir,
            extra_config=_build_extra_config(config),
            sms_code_provider=TelegramSmsCodeProvider(
                bot_token, admin_chat_id, poll),
            password_provider=TelegramPasswordProvider(
                bot_token, admin_chat_id, poll),
        )

    raise ValueError(f"unknown MAX2TG_AUTH_METHOD: {method!r}")
