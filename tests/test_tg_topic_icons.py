"""Telegram forum topic icon API helpers."""
import unittest
from unittest.mock import MagicMock, patch

import tg


class ForumTopicIconTests(unittest.TestCase):
    def test_get_forum_topic_icon_sticker_ids_caches(self):
        tg._forum_icon_sticker_ids_cache.clear()
        stickers = [{"custom_emoji_id": "111"}, {"custom_emoji_id": "222"}]
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": True, "result": stickers}
        with patch.object(tg.requests, "post", return_value=response) as post:
            first = tg.get_forum_topic_icon_sticker_ids("TOK")
            second = tg.get_forum_topic_icon_sticker_ids("TOK")
        self.assertEqual(first, ["111", "222"])
        self.assertEqual(second, ["111", "222"])
        self.assertEqual(post.call_count, 1)

    def test_create_forum_topic_passes_icon_params(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "ok": True,
            "result": {"message_thread_id": 42},
        }
        with patch.object(tg.requests, "post", return_value=response) as post:
            thread_id = tg.create_forum_topic(
                "TOK", -1001, "Topic",
                icon_color=7322096,
                icon_custom_emoji_id="999",
            )
        self.assertEqual(thread_id, 42)
        payload = post.call_args.kwargs.get("json") or post.call_args[1].get("json")
        self.assertEqual(payload["icon_color"], 7322096)
        self.assertEqual(payload["icon_custom_emoji_id"], "999")

    def test_edit_forum_topic_icon_only(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": True, "result": True}
        with patch.object(tg.requests, "post", return_value=response) as post:
            tg.edit_forum_topic(
                "TOK", -1001, 42, icon_custom_emoji_id="888",
            )
        payload = post.call_args.kwargs.get("json") or post.call_args[1].get("json")
        self.assertEqual(payload["icon_custom_emoji_id"], "888")
        self.assertNotIn("name", payload)


if __name__ == "__main__":
    unittest.main()
