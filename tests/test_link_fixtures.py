"""Shared helpers for seeding MessageLinkRegistry in bridge tests."""


def seed_max_to_tg_link(
    bridge,
    *,
    max_chat_id="555",
    max_message_id="1",
    tg_message_id=10,
    telegram_chat_id=-100222,
    message_thread_id=None,
    role="text",
    sender=None,
    source="live",
):
    bridge._links.link(
        max_chat_id,
        max_message_id,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=tg_message_id,
        message_thread_id=message_thread_id,
        role=role,
        origin="max_to_tg",
        source=source,
        sender=sender,
    )


def seed_tg_to_max_link(
    bridge,
    *,
    tg_message_id=100,
    max_chat_id="555",
    max_message_id="42",
    telegram_chat_id=111,
    message_thread_id=None,
):
    bridge._links.link(
        max_chat_id,
        max_message_id,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=tg_message_id,
        message_thread_id=message_thread_id,
        role="text",
        origin="tg_to_max",
    )
