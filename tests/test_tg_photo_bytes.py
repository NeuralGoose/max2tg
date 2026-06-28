"""Stage 2: tg.send_photo_bytes uploads raw image bytes via multipart."""
import unittest
from unittest.mock import patch

import tg


class SendPhotoBytesTests(unittest.TestCase):
    def test_posts_multipart_via_call_upload(self):
        with patch.object(tg, "_call_upload",
                          return_value={"message_id": 42}) as upload:
            mid = tg.send_photo_bytes("TOK", 555, b"PNGDATA",
                                      caption="cap", filename="max_qr.png")

        self.assertEqual(mid, 42)
        args, kwargs = upload.call_args
        self.assertEqual(args[0], "TOK")
        self.assertEqual(args[1], "sendPhoto")
        files = args[2]
        self.assertEqual(files["photo"], ("max_qr.png", b"PNGDATA"))
        self.assertEqual(kwargs["chat_id"], 555)
        self.assertEqual(kwargs["caption"], "cap")

    def test_thread_id_included_when_set(self):
        with patch.object(tg, "_call_upload",
                          return_value={"message_id": 1}) as upload:
            tg.send_photo_bytes("TOK", 555, b"x", message_thread_id=99)
        _, kwargs = upload.call_args
        self.assertEqual(kwargs["message_thread_id"], 99)

    def test_caption_truncated_to_limit(self):
        long_caption = "x" * (tg.MAX_CAPTION_LEN + 50)
        with patch.object(tg, "_call_upload",
                          return_value={"message_id": 1}) as upload:
            tg.send_photo_bytes("TOK", 555, b"x", caption=long_caption)
        _, kwargs = upload.call_args
        self.assertEqual(len(kwargs["caption"]), tg.MAX_CAPTION_LEN)

    def test_no_message_id_returns_none(self):
        with patch.object(tg, "_call_upload", return_value={}):
            self.assertIsNone(tg.send_photo_bytes("TOK", 555, b"x"))


if __name__ == "__main__":
    unittest.main()
