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
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Drop an inline comment ("  # ...") from an UNQUOTED value; the
            # space guard avoids mangling tokens that legitimately contain '#'.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        os.environ.setdefault(key, value)


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
    """Return a complete config by layering MAX2TG_* env vars on top of
    config.json, PER KEY.

    This avoids the all-or-nothing trap where having the three token env vars
    set would otherwise discard every optional setting (topics, confirm_sent,
    ...) stored in config.json. Env-only deploys still work: when config.json is
    absent the base is empty and the env vars supply everything.
    """
    base = load_partial()  # {} if config.json is missing or unreadable
    merged = {**base, **_env_overrides()}
    if not all(merged.get(k) for k in REQUIRED_KEYS):
        return None
    return normalize_config(merged)


def load_partial() -> dict:
    """Return whatever is in config.json (may be incomplete), or {}."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        # Distinguish a corrupt/unreadable config.json from a genuinely absent
        # one (which returns {} above) so headless deploys are diagnosable.
        _logger.warning("Could not read config.json: %s", exc)
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
    username = os.environ.get("USERNAME") or ""
    if not username:
        try:
            username = os.getlogin()
        except OSError:
            return
    # Qualify the principal as DOMAIN\USER. A bare username is ambiguous when the
    # computer name equals the username: icacls resolves it to "MACHINE\" (empty
    # account) and, with /inheritance:r, locks the real user out of the file.
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME")
    principal = f"{domain}\\{username}" if domain else username
    try:
        subprocess.run(
            ["icacls", str(CONFIG_PATH), "/inheritance:r",
             "/grant:r", f"{principal}:(R,W)"],
            check=False, capture_output=True,
        )
    except OSError as exc:
        _logger.warning("Could not restrict config.json permissions: %s", exc)


def save_config(config: dict) -> None:
    if os.name != "nt" and not CONFIG_PATH.exists():
        # Create the token file already-restricted (0o600) so it is never even
        # briefly world-readable between write and chmod (TOCTOU).
        try:
            os.close(os.open(CONFIG_PATH, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            pass
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _restrict_permissions()
