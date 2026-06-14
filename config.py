"""Load/save bridge configuration (tokens) in config.json next to the scripts."""
import json
import logging
import os
import subprocess
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

REQUIRED_KEYS = ("telegram_bot_token", "telegram_chat_id", "max_login_token")


def _coerce_chat_id(value):
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return value


def _coerce_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_config(data: dict) -> dict:
    """Apply optional settings and backwards-compatible defaults."""
    result = dict(data)
    for key in ("telegram_chat_id", "telegram_forum_chat_id", "telegram_fallback_chat_id"):
        if key in result:
            result[key] = _coerce_chat_id(result[key])
    if "telegram_fallback_chat_id" not in result:
        result["telegram_fallback_chat_id"] = result.get("telegram_chat_id")
    explicit_topics = result.get("telegram_topics_enabled")
    result["telegram_topics_enabled"] = _coerce_bool(
        explicit_topics,
        default=bool(result.get("telegram_forum_chat_id")),
    )
    result["telegram_preload_topics"] = _coerce_bool(
        result.get("telegram_preload_topics"),
        default=False,
    )
    result["telegram_seed_last_messages"] = _coerce_bool(
        result.get("telegram_seed_last_messages"),
        default=result["telegram_preload_topics"],
    )
    result["telegram_preload_chat_count"] = max(
        1,
        _coerce_int(result.get("telegram_preload_chat_count"), 100),
    )
    result["telegram_resync_titles"] = _coerce_bool(
        result.get("telegram_resync_titles"),
        default=False,
    )
    result["telegram_confirm_sent"] = _coerce_bool(
        result.get("telegram_confirm_sent"),
        default=True,
    )
    return result

_logger = logging.getLogger(__name__)


def load_from_env() -> dict | None:
    """Build config from env vars (for headless/server deploys), or None.

    MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID, MAX2TG_MAX_TOKEN.
    """
    env_map = {
        "telegram_bot_token": os.environ.get("MAX2TG_TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("MAX2TG_TELEGRAM_CHAT_ID"),
        "max_login_token": os.environ.get("MAX2TG_MAX_TOKEN"),
        "telegram_forum_chat_id": os.environ.get("MAX2TG_TELEGRAM_FORUM_CHAT_ID"),
        "telegram_topics_enabled": os.environ.get("MAX2TG_TELEGRAM_TOPICS_ENABLED"),
        "telegram_fallback_chat_id": os.environ.get("MAX2TG_TELEGRAM_FALLBACK_CHAT_ID"),
        "telegram_preload_topics": os.environ.get("MAX2TG_TELEGRAM_PRELOAD_TOPICS"),
        "telegram_seed_last_messages": os.environ.get("MAX2TG_TELEGRAM_SEED_LAST_MESSAGES"),
        "telegram_preload_chat_count": os.environ.get("MAX2TG_TELEGRAM_PRELOAD_CHAT_COUNT"),
        "telegram_resync_titles": os.environ.get("MAX2TG_TELEGRAM_RESYNC_TITLES"),
        "telegram_confirm_sent": os.environ.get("MAX2TG_TELEGRAM_CONFIRM_SENT"),
    }
    if not all(env_map.get(k) for k in REQUIRED_KEYS):
        return None
    return normalize_config({k: v for k, v in env_map.items() if v is not None})


def load_config() -> dict | None:
    """Return a complete config from env vars or config.json, else None."""
    from_env = load_from_env()
    if from_env:
        return from_env
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    if not all(data.get(k) for k in REQUIRED_KEYS):
        return None
    return normalize_config(data)


def load_partial() -> dict:
    """Return whatever is in config.json (may be incomplete), or {}."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
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
