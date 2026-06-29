"""Telegram send_message reply_parameters forwarding."""
import unittest
from unittest.mock import patch

import tg


class TgReplyTests(unittest.TestCase):
    @patch("tg._call_with_entity_fallback")
    def test_send_message_forwards_reply_parameters(self, mock_call):
        mock_call.return_value = {"message_id": 10}
        reply_parameters = {"message_id": 5, "quote": "quoted"}
        tg.send_message(
            "token", -1001, "hello",
            reply_parameters=reply_parameters,
        )
        params = mock_call.call_args[0][2]
        self.assertEqual(params["reply_parameters"], reply_parameters)
        self.assertNotIn("reply_to_message_id", params)

    @patch("tg._call_with_entity_fallback")
    def test_reply_parameters_preferred_over_reply_to_message_id(self, mock_call):
        mock_call.return_value = {"message_id": 10}
        tg.send_message(
            "token", -1001, "hello",
            reply_to_message_id=3,
            reply_parameters={"message_id": 5},
        )
        params = mock_call.call_args[0][2]
        self.assertEqual(params["reply_parameters"], {"message_id": 5})
        self.assertNotIn("reply_to_message_id", params)


if __name__ == "__main__":
    unittest.main()
