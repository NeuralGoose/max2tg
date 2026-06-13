"""Entry point: runs first-time setup if needed, then the MAX -> Telegram bridge."""
import asyncio
import logging
import sys
from pathlib import Path

from bridge import MaxToTelegramBridge
from config import load_config
from setup_wizard import run_setup

LOG_PATH = Path(__file__).parent / "bridge.log"


def _configure() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    # vkmax logs every packet at INFO, including auth tokens - keep it quieter
    logging.getLogger("vkmax").setLevel(logging.WARNING)


def main() -> None:
    _configure()
    config = load_config()
    if config is None:
        # No config: the setup wizard needs an interactive console + browser.
        # On a server (no TTY) fail loudly instead of hanging on input().
        if not sys.stdin or not sys.stdin.isatty():
            print("Конфигурация не найдена. На сервере задайте переменные "
                  "окружения MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID, "
                  "MAX2TG_MAX_TOKEN, либо положите рядом готовый config.json "
                  "(см. DEPLOY.md).", file=sys.stderr)
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
