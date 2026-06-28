"""tg.send_media_group album API."""
import unittest
from unittest.mock import patch

import tg


class SendMediaGroupTests(unittest.TestCase):
    def test_builds_payload_with_caption_on_first_item_only(self):
        with patch.object(tg, "_call", return_value=[
            {"message_id": 1}, {"message_id": 2},
        ]) as call:
            ids = tg.send_media_group(
                "TOK",
                -100222,
                [
                    {"type": "photo", "url": "https://cdn/a.jpg"},
                    {"type": "photo", "url": "https://cdn/b.jpg"},
                ],
                caption="Hello",
                message_thread_id=42,
            )

        self.assertEqual(ids, [1, 2])
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100222)
        self.assertEqual(kwargs["message_thread_id"], 42)
        media = kwargs["media"]
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0]["caption"], "Hello")
        self.assertNotIn("caption", media[1])

    def test_falls_back_to_sequential_on_api_failure(self):
        with patch.object(tg, "_call", side_effect=RuntimeError("fail")), \
                patch.object(tg, "send_photo", side_effect=[10, 11]) as photo:
            ids = tg.send_media_group(
                "TOK",
                555,
                [
                    {"type": "photo", "url": "https://cdn/a.jpg"},
                    {"type": "photo", "url": "https://cdn/b.jpg"},
                ],
                caption="Cap",
            )

        self.assertEqual(ids, [10, 11])
        photo.assert_any_call("TOK", 555, "https://cdn/a.jpg", "Cap", message_thread_id=None)
        photo.assert_any_call("TOK", 555, "https://cdn/b.jpg", None, message_thread_id=None)


if __name__ == "__main__":
    unittest.main()
