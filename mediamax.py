"""Resolve downloadable URLs for MAX file/video attachments.

The web client turns an attachment id + token into a temporary CDN URL via two
WS requests (reverse-engineered from web.max.ru):
  - FILE  : opcode 88, payload {fileId, chatId, messageId}  -> payload.url
  - VIDEO : opcode 83, payload {videoId, chatId, messageId} -> payload["MP4_<h>"]
Resolved URLs carry an `expires` query param (~24h).
"""
import logging

from vkmax.client import MaxClient

_logger = logging.getLogger(__name__)

FILE_RESOLVE_OPCODE = 88
VIDEO_RESOLVE_OPCODE = 83


async def resolve_file_url(client: MaxClient, file_id: int | str,
                           chat_id: int | str, message_id: int | str) -> str:
    response = await client.invoke_method(
        opcode=FILE_RESOLVE_OPCODE,
        payload={"fileId": file_id, "chatId": chat_id, "messageId": message_id},
    )
    payload = response.get("payload", {})
    url = payload.get("url")
    if not url:
        raise RuntimeError(f"file resolve returned no url: {payload}")
    return url


async def resolve_video_url(client: MaxClient, video_id: int | str,
                            chat_id: int | str, message_id: int | str) -> str:
    """Return the highest-resolution MP4 URL for a video attachment."""
    response = await client.invoke_method(
        opcode=VIDEO_RESOLVE_OPCODE,
        payload={"videoId": video_id, "chatId": chat_id, "messageId": message_id},
    )
    payload = response.get("payload", {})
    best_url, best_height = None, -1
    for key, value in payload.items():
        if isinstance(key, str) and key.startswith("MP4_") and isinstance(value, str):
            try:
                height = int(key[4:])
            except ValueError:
                continue
            if height > best_height:
                best_height, best_url = height, value
    if not best_url:
        raise RuntimeError(f"video resolve returned no MP4 source: {payload}")
    return best_url
