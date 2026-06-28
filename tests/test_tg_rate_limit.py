"""Telegram Bot API 429 rate-limit handling."""
import unittest
from unittest.mock import MagicMock, patch

import tg


class RateLimitRetryTests(unittest.TestCase):
    def test_retry_after_from_rate_limit_error(self):
        err = tg.RateLimitError("limited", 32.0)
        self.assertTrue(tg.is_rate_limit_error(err))
        self.assertEqual(tg.retry_after_from_error(err), 32.0)

    def test_call_retries_after_429_then_succeeds(self):
        rate_limited = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 2",
            "parameters": {"retry_after": 2},
        }
        ok = {"ok": True, "result": {"message_id": 99}}
        response_fail = MagicMock(status_code=429)
        response_fail.json.return_value = rate_limited
        response_ok = MagicMock(status_code=200)
        response_ok.json.return_value = ok

        tg.set_api_min_interval(0)
        try:
            with patch.object(tg.requests, "post", side_effect=[response_fail, response_ok]), \
                    patch.object(tg.time, "sleep") as sleep:
                result = tg._call("TOK", "sendMessage", chat_id=1, text="hi")

            self.assertEqual(result["message_id"], 99)
            sleep.assert_called_once()
            self.assertGreaterEqual(sleep.call_args.args[0], 2.0)
        finally:
            tg.set_api_min_interval(0.05)

    def test_send_media_group_does_not_fallback_on_429(self):
        with patch.object(tg, "_call", side_effect=tg.RateLimitError("limited", 3)), \
                patch.object(tg, "_send_media_group_sequential") as sequential:
            with self.assertRaises(tg.RateLimitError):
                tg.send_media_group(
                    "TOK",
                    1,
                    [
                        {"type": "photo", "url": "https://cdn/a.jpg"},
                        {"type": "photo", "url": "https://cdn/b.jpg"},
                    ],
                )
            sequential.assert_not_called()

    def test_api_min_interval_paces_calls(self):
        tg.set_api_min_interval(0.1)
        try:
            ok = {"ok": True, "result": {"message_id": 1}}
            response = MagicMock(status_code=200)
            response.json.return_value = ok
            with patch.object(tg.requests, "post", return_value=response) as post, \
                    patch.object(tg.time, "sleep") as sleep, \
                    patch.object(tg.time, "monotonic", side_effect=[0.0, 0.0, 0.05, 0.15]):
                tg._call("TOK", "sendMessage", chat_id=1, text="a")
                tg._call("TOK", "sendMessage", chat_id=1, text="b")
            self.assertEqual(post.call_count, 2)
            self.assertGreaterEqual(sleep.call_count, 1)
        finally:
            tg.set_api_min_interval(0.05)


if __name__ == "__main__":
    unittest.main()
