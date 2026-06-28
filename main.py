"""Entry point: runs first-time setup if needed, then the MAX -> Telegram bridge."""
import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from bridge import MaxToTelegramBridge
from config import apply_dotenv, load_config, missing_env_var_names
from setup_wizard import run_setup

LOG_PATH = Path(__file__).parent / "bridge.log"

# Telegram bot token (bot<digits>:<base64ish>) and URL secrets (?token=/?sig=).
_BOT_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_URL_SECRET_RE = re.compile(
    r"([?&](?:token|sig|access_token|key|auth)=)[^&\s'\")]+", re.IGNORECASE)


class _RedactSecretsFilter(logging.Filter):
    """Scrub bot tokens and URL secrets from every record before it is written.

    requests exceptions embed the full request URL — which contains the bot
    token / signed MAX URLs — so a logged exception would otherwise leak secrets
    into bridge.log (CWE-532)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _URL_SECRET_RE.sub(
            r"\1<redacted>", _BOT_TOKEN_RE.sub("bot<redacted>", message))
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _restrict_log_perms() -> None:
    """Lock bridge.log to the current user (it can hold message content)."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                # Qualify the principal as DOMAIN\USER. A bare username is
                # ambiguous when the computer name equals the username: icacls
                # resolves it to "MACHINE\" (empty account) and, combined with
                # /inheritance:r, locks the real user out of the file entirely.
                domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME")
                principal = f"{domain}\\{user}" if domain else user
                subprocess.run(
                    ["icacls", str(LOG_PATH), "/inheritance:r",
                     "/grant:r", f"{principal}:(R,W)"],
                    check=False, capture_output=True,
                )
        else:
            LOG_PATH.chmod(0o600)
    except OSError:
        pass


def _configure() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.addFilter(_RedactSecretsFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
    )
    # PyMax can log verbose protocol detail at INFO/DEBUG - keep it quieter so
    # auth/session detail doesn't flood (and potentially leak into) bridge.log.
    logging.getLogger("pymax").setLevel(logging.WARNING)
    _restrict_log_perms()


def main() -> None:
    _configure()
    apply_dotenv()  # load a local .env file if present (any launch method)
    config = load_config()
    if config is None:
        # No config: the setup wizard needs an interactive console + browser.
        # On a server (no TTY) fail loudly instead of hanging on input().
        if not sys.stdin or not sys.stdin.isatty():
            missing = ", ".join(missing_env_var_names()) or "см. DEPLOY.md"
            print("Конфигурация не найдена. На сервере задайте переменные "
                  f"окружения {missing}, либо положите рядом готовый "
                  "config.json (см. DEPLOY.md).", file=sys.stderr)
            raise SystemExit(1)
        print("Конфигурация не найдена - запускаю мастер настройки.")
        config = run_setup()
    bridge = MaxToTelegramBridge(config)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        print("Остановлено.")


if __name__ == "__main__":
    main()
