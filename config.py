"""Load/save bridge configuration (tokens) in config.json next to the scripts."""
import json
import logging
import os
import subprocess
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

# MAX login methods. `token` (default) preserves the legacy pasted-web-token flow;
# `sms` logs in by phone + SMS code; `qr` logs in by scanning a QR. See docs/.
AUTH_METHODS = ("token", "sms", "qr")
DEFAULT_AUTH_METHOD = "token"

# Back-compat constant (token method). Prefer required_keys(method) for validation.
REQUIRED_KEYS = ("telegram_bot_token", "telegram_chat_id", "max_login_token")


def _normalize_method(value) -> str:
    method = str(value or DEFAULT_AUTH_METHOD).strip().lower()
    return method if method in AUTH_METHODS else DEFAULT_AUTH_METHOD


def required_keys(method: str | None = None) -> tuple[str, ...]:
    """Config keys that must be present for the given MAX auth method.

    Telegram credentials are always required. The MAX credential depends on the
    method: `token` needs max_login_token, `sms` needs max_phone, `qr` needs
    neither (it is fully interactive).
    """
    base = ("telegram_bot_token", "telegram_chat_id")
    method = _normalize_method(method)
    if method == "sms":
        return base + ("max_phone",)
    if method == "qr":
        return base
    return base + ("max_login_token",)


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


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_preload_chat_source(value) -> str:
    source = str(value or "login").strip().lower()
    return source if source in ("login", "fetch") else "login"


def _parse_exclude_chat_ids(value) -> frozenset[int]:
    """MAX chat ids to never bridge (default: 0 = Saved Messages / Избранное)."""
    if value is None:
        return frozenset({0})
    if isinstance(value, (list, tuple, set, frozenset)):
        ids: list[int] = []
        for item in value:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        return frozenset(ids) if ids else frozenset({0})
    raw = str(value).strip()
    if not raw:
        return frozenset({0})
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return frozenset(ids) if ids else frozenset({0})


def normalize_config(data: dict) -> dict:
    """Apply optional settings and backwards-compatible defaults."""
    result = dict(data)
    for key in ("telegram_chat_id", "telegram_forum_chat_id", "telegram_fallback_chat_id"):
        if key in result:
            result[key] = _coerce_chat_id(result[key])
    if "telegram_fallback_chat_id" not in result:
        result["telegram_fallback_chat_id"] = result.get("telegram_chat_id")
    # Admin/auth chat: private DM with the bot (QR/SMS prompts, /commands).
    # If only FALLBACK_CHAT_ID is set (common in topics mode), reuse it here.
    if not result.get("telegram_chat_id") and result.get("telegram_fallback_chat_id"):
        result["telegram_chat_id"] = result["telegram_fallback_chat_id"]
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
    result["telegram_preload_chat_source"] = _normalize_preload_chat_source(
        result.get("telegram_preload_chat_source"),
    )
    result["telegram_preload_message_depth"] = min(
        50,
        max(0, _coerce_int(result.get("telegram_preload_message_depth"), 1)),
    )
    result["telegram_preload_fetch_pages"] = max(
        1,
        _coerce_int(result.get("telegram_preload_fetch_pages"), 20),
    )
    result["telegram_preload_chat_delay_seconds"] = max(
        0.0,
        _coerce_float(result.get("telegram_preload_chat_delay_seconds"), 0.35),
    )
    result["telegram_api_min_interval_seconds"] = max(
        0.0,
        _coerce_float(result.get("telegram_api_min_interval_seconds"), 0.05),
    )
    result["telegram_preload_api_min_interval_seconds"] = max(
        0.0,
        _coerce_float(result.get("telegram_preload_api_min_interval_seconds"), 1.0),
    )
    result["telegram_resync_titles"] = _coerce_bool(
        result.get("telegram_resync_titles"),
        default=False,
    )
    result["telegram_confirm_sent"] = _coerce_bool(
        result.get("telegram_confirm_sent"),
        default=True,
    )
    result["telegram_mirror_edit_marker"] = _coerce_bool(
        result.get("telegram_mirror_edit_marker"),
        default=True,
    )
    result["telegram_exclude_chat_ids"] = _parse_exclude_chat_ids(
        result.get("telegram_exclude_chat_ids"),
    )
    result["max_auth_method"] = _normalize_method(result.get("max_auth_method"))
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
    "max_auth_method": "MAX2TG_AUTH_METHOD",
    "max_phone": "MAX2TG_MAX_PHONE",
    "telegram_forum_chat_id": "MAX2TG_TELEGRAM_FORUM_CHAT_ID",
    "telegram_topics_enabled": "MAX2TG_TELEGRAM_TOPICS_ENABLED",
    "telegram_fallback_chat_id": "MAX2TG_TELEGRAM_FALLBACK_CHAT_ID",
    "telegram_preload_topics": "MAX2TG_TELEGRAM_PRELOAD_TOPICS",
    "telegram_seed_last_messages": "MAX2TG_TELEGRAM_SEED_LAST_MESSAGES",
    "telegram_preload_chat_count": "MAX2TG_TELEGRAM_PRELOAD_CHAT_COUNT",
    "telegram_preload_chat_source": "MAX2TG_TELEGRAM_PRELOAD_CHAT_SOURCE",
    "telegram_preload_message_depth": "MAX2TG_TELEGRAM_PRELOAD_MESSAGE_DEPTH",
    "telegram_preload_fetch_pages": "MAX2TG_TELEGRAM_PRELOAD_FETCH_PAGES",
    "telegram_preload_chat_delay_seconds": "MAX2TG_TELEGRAM_PRELOAD_CHAT_DELAY_SECONDS",
    "telegram_api_min_interval_seconds": "MAX2TG_TELEGRAM_API_MIN_INTERVAL_SECONDS",
    "telegram_preload_api_min_interval_seconds": "MAX2TG_TELEGRAM_PRELOAD_API_MIN_INTERVAL_SECONDS",
    "telegram_resync_titles": "MAX2TG_TELEGRAM_RESYNC_TITLES",
    "telegram_confirm_sent": "MAX2TG_TELEGRAM_CONFIRM_SENT",
    "telegram_mirror_edit_marker": "MAX2TG_TELEGRAM_MIRROR_EDIT_MARKER",
    "telegram_exclude_chat_ids": "MAX2TG_TELEGRAM_EXCLUDE_CHAT_IDS",
}


def missing_config_keys(merged: dict | None = None) -> tuple[str, ...]:
    """Return internal config keys that are still unset for the chosen auth method."""
    if merged is None:
        merged = normalize_config({**load_partial(), **_env_overrides()})
    method = _normalize_method(merged.get("max_auth_method"))
    return tuple(k for k in required_keys(method) if not merged.get(k))


def missing_env_var_names(merged: dict | None = None) -> list[str]:
    """Map missing config keys to their MAX2TG_* environment variable names."""
    return [ENV_MAP[k] for k in missing_config_keys(merged) if k in ENV_MAP]


def _env_overrides() -> dict:
    """Collect set, non-empty MAX2TG_* env vars as config overrides."""
    return {
        key: os.environ[var]
        for key, var in ENV_MAP.items()
        if os.environ.get(var) not in (None, "")
    }


def load_from_env() -> dict | None:
    """Build config from env vars (for headless/server deploys), or None."""
    env_map = normalize_config(_env_overrides())
    method = _normalize_method(env_map.get("max_auth_method"))
    if not all(env_map.get(k) for k in required_keys(method)):
        return None
    return env_map


def load_config() -> dict | None:
    """Return a complete config by layering MAX2TG_* env vars on top of
    config.json, PER KEY.

    This avoids the all-or-nothing trap where having the three token env vars
    set would otherwise discard every optional setting (topics, confirm_sent,
    ...) stored in config.json. Env-only deploys still work: when config.json is
    absent the base is empty and the env vars supply everything.
    """
    merged = normalize_config({**load_partial(), **_env_overrides()})
    method = _normalize_method(merged.get("max_auth_method"))
    if not all(merged.get(k) for k in required_keys(method)):
        return None
    return merged


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


def _write_text_private(path: Path, payload: str) -> None:
    """Write text; on POSIX create the file pre-restricted (0o600) so plaintext
    tokens are never briefly world-readable between write and chmod (TOCTOU)."""
    if os.name != "nt":
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def _atomic_write(path: Path, payload: str) -> None:
    """Write via temp file + rename so a crash mid-write cannot leave a
    truncated/invalid config.json (which would silently load as {}). Falls back
    to an in-place write when rename fails (e.g. single-file Docker bind mount)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        _write_text_private(tmp, payload)
        tmp.replace(path)
        return
    except OSError as exc:
        _logger.warning("Atomic config save failed (%s); writing in place.", exc)
    try:
        _write_text_private(path, payload)
    except OSError as exc:
        _logger.error("Could not persist config: %s", exc)
    try:
        tmp.unlink()
    except OSError:
        pass


def save_config(config: dict) -> None:
    # Merge over whatever is already on disk so partial/intermediate saves (e.g.
    # the setup wizard saving only Telegram creds first) never clobber unrelated
    # keys like telegram_forum_chat_id or preload settings.
    merged = {**load_partial(), **config}
    _atomic_write(
        CONFIG_PATH,
        json.dumps(merged, ensure_ascii=False, indent=2),
    )
    _restrict_permissions()
