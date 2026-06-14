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


DOTENV_PATH = CONFIG_PATH.parent / ".env"


def apply_dotenv(path: Path | None = None) -> None:
    """Load a local .env file into os.environ so a bare `python main.py` picks
    it up too (not only Docker/systemd). Real environment variables win."""
    path = path or DOTENV_PATH
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# config key -> environment variable name. Used both to build a full config
# from env vars and to let env vars override optional settings from config.json.
ENV_MAP = {
    "telegram_bot_token": "MAX2TG_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "MAX2TG_TELEGRAM_CHAT_ID",
    "max_login_token": "MAX2TG_MAX_TOKEN",
    "telegram_forum_chat_id": "MAX2TG_TELEGRAM_FORUM_CHAT_ID",
    "telegram_topics_enabled": "MAX2TG_TELEGRAM_TOPICS_ENABLED",
    "telegram_fallback_chat_id": "MAX2TG_TELEGRAM_FALLBACK_CHAT_ID",
    "telegram_preload_topics": "MAX2TG_TELEGRAM_PRELOAD_TOPICS",
    "telegram_seed_last_messages": "MAX2TG_TELEGRAM_SEED_LAST_MESSAGES",
    "telegram_preload_chat_count": "MAX2TG_TELEGRAM_PRELOAD_CHAT_COUNT",
    "telegram_resync_titles": "MAX2TG_TELEGRAM_RESYNC_TITLES",
    "telegram_confirm_sent": "MAX2TG_TELEGRAM_CONFIRM_SENT",
}


def _env_overrides() -> dict:
    """Collect set, non-empty MAX2TG_* env vars as config overrides."""
    return {
        key: os.environ[var]
        for key, var in ENV_MAP.items()
        if os.environ.get(var) not in (None, "")
    }


def load_from_env() -> dict | None:
    """Build config from env vars (for headless/server deploys), or None.

    MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID, MAX2TG_MAX_TOKEN.
    """
    env_map = _env_overrides()
    if not all(env_map.get(k) for k in REQUIRED_KEYS):
        return None
    return normalize_config(env_map)


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
    # Let MAX2TG_* env vars override optional settings (e.g. confirm_sent,
    # topics) even when the tokens themselves come from config.json.
    data.update(_env_overrides())
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
