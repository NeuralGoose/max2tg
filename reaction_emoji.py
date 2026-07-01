"""Map MAX reaction emojis to Telegram's allowed setMessageReaction emojis."""
from __future__ import annotations

# Default global reaction set (messages.getAvailableReactions). Forum chats may
# restrict further, but these are always valid in private chats / as bot reactions.
TELEGRAM_REACTION_EMOJIS = frozenset({
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍", "🤗", "🧡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♀", "🤷",
    "🤷‍♂", "😡",
})

# MAX / Unicode variants → Telegram-supported emoji (semantic closest match).
_MAX_TO_TG_REACTION_ALIASES: dict[str, str] = {
    "❤️": "❤",
    "☺️": "😁",
    "☺": "😁",
    "🤟": "❤",   # love-you gesture
    "🤘": "🔥",   # rock on
    "💪": "👍",
    "🙌": "👏",
    "👊": "👍",
    "✌️": "👌",
    "✌": "👌",
    "🦀": "👍",
    "👣": "👀",
}


def strip_emoji_variation(emoji: str) -> str:
    return emoji.replace("\ufe0f", "")


def normalize_max_reaction_for_telegram(emoji: str | None) -> str | None:
    """Return a Telegram-allowed reaction emoji, or None if unmappable."""
    if not emoji:
        return None
    raw = emoji.strip()
    if not raw:
        return None
    if raw in TELEGRAM_REACTION_EMOJIS:
        return raw
    stripped = strip_emoji_variation(raw)
    if stripped in TELEGRAM_REACTION_EMOJIS:
        return stripped
    alias = _MAX_TO_TG_REACTION_ALIASES.get(raw)
    if alias is None:
        alias = _MAX_TO_TG_REACTION_ALIASES.get(stripped)
    if alias and alias in TELEGRAM_REACTION_EMOJIS:
        return alias
    return None
