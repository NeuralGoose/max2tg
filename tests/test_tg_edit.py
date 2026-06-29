"""Telegram edit helpers: edit_date return value and link preview parity."""
import unittest
from unittest.mock import patch

import tg


class TgEditTests(unittest.TestCase):
    @patch("tg._call_with_entity_fallback")
    def test_edit_message_text_returns_message_with_edit_date(self, mock_call):
        mock_call.return_value = {"message_id": 10, "edit_date": 1710000000}
        result = tg.edit_message_text("token", -1001, 10, "updated")
        self.assertEqual(result["edit_date"], 1710000000)
        params = mock_call.call_args[0][2]
        self.assertEqual(params["link_preview_options"], {"is_disabled": True})
        self.assertEqual(params["text"], "updated")

    @patch("tg._call_with_entity_fallback")
    def test_edit_message_text_passes_thread_and_entities(self, mock_call):
        mock_call.return_value = {"message_id": 10}
        entities = [{"type": "bold", "offset": 0, "length": 4}]
        tg.edit_message_text(
            "token", -1001, 10, "bold",
            message_thread_id=42, entities=entities,
        )
        params = mock_call.call_args[0][2]
        self.assertEqual(params["message_thread_id"], 42)
        self.assertEqual(params["entities"], entities)

    @patch("tg._call_with_entity_fallback")
    def test_edit_message_caption_returns_message(self, mock_call):
        mock_call.return_value = {"message_id": 11, "edit_date": 99}
        result = tg.edit_message_caption("token", -1001, 11, "cap")
        self.assertEqual(result["edit_date"], 99)

    @patch("tg._call_with_entity_fallback")
    def test_edit_message_caption_empty_returns_none(self, mock_call):
        result = tg.edit_message_caption("token", -1001, 11, "")
        self.assertIsNone(result)
        mock_call.assert_not_called()

    @patch("tg._call_with_entity_fallback")
    def test_send_message_uses_link_preview_options(self, mock_call):
        mock_call.return_value = {"message_id": 1}
        tg.send_message("token", 1, "hello https://example.com")
        params = mock_call.call_args[0][2]
        self.assertEqual(params["link_preview_options"], {"is_disabled": True})
        self.assertNotIn("disable_web_page_preview", params)


if __name__ == "__main__":
    unittest.main()
