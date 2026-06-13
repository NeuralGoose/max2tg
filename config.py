"""Load/save bridge configuration (tokens) in config.json next to the scripts."""
import json
import logging
import os
import subprocess
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

REQUIRED_KEYS = ("telegram_bot_token", "telegram_chat_id", "max_login_token")

_logger = logging.getLogger(__name__)


def load_from_env() -> dict | None:
    """Build config from env vars (for headless/server deploys), or None.

    MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID, MAX2TG_MAX_TOKEN.
    """
    env_map = {
        "telegram_bot_token": os.environ.get("MAX2TG_TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("MAX2TG_TELEGRAM_CHAT_ID"),
        "max_login_token": os.environ.get("MAX2TG_MAX_TOKEN"),
    }
    if not all(env_map.values()):
        return None
    chat_id = env_map["telegram_chat_id"]
    if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit():
        env_map["telegram_chat_id"] = int(chat_id)
    return env_map


def load_config() -> dict | None:
    """Return a complete config from env vars or config.json, else None."""
    from_env = load_from_env()
    if from_env:
        return from_env
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not all(data.get(k) for k in REQUIRED_KEYS):
        return None
    return data


def load_partial() -> dict:
    """Return whatever is in config.json (may be incomplete), or {}."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _restrict_permissions() -> None:
    """Restrict config.json to the current user (it stores plaintext tokens)."""
    if os.name != "nt":
        # On Linux/containers use file mode instead of icacls.
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError as exc:
            _logger.warning("Could not chmod config.json: %s", exc)
        return
    username = os.environ.get("USERNAME")
    if not username:
        return
    try:
        subprocess.run(
            ["icacls", str(CONFIG_PATH), "/inheritance:r",
             "/grant:r", f"{username}:(R,W)"],
            check=False, capture_output=True,
        )
    except OSError as exc:
        _logger.warning("Could not restrict config.json permissions: %s", exc)


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _restrict_permissions()
