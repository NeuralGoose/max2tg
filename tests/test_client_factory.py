"""Stage 2: build_max_client picks the right PyMax client per auth method."""
import tempfile
import unittest
from unittest.mock import Mock

import max_client
from pymax import Client, WebClient


def _cfg(method, tmp, **extra):
    data = {
        "telegram_bot_token": "t",
        "telegram_chat_id": 555,
        "max_auth_method": method,
        "max_work_dir": tmp,
    }
    data.update(extra)
    return data


class BuildMaxClientTests(unittest.TestCase):
    def test_token_builds_webclient_with_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = max_client.build_max_client(
                _cfg("token", tmp, max_login_token="TKN"))
        self.assertIsInstance(client, WebClient)
        self.assertEqual(client.extra_config.token, "TKN")

    def test_qr_builds_webclient_without_token(self):
        from pymax.auth.qr import QrAuthFlow

        from maxauth import TelegramPasswordProvider, TelegramQrHandler

        with tempfile.TemporaryDirectory() as tmp:
            client = max_client.build_max_client(
                _cfg("qr", tmp), bot_token="b", admin_chat_id=555)
        self.assertIsInstance(client, WebClient)
        self.assertIsNone(client.extra_config.token)
        flow = client._auth_flow
        self.assertIsInstance(flow, QrAuthFlow)
        self.assertIsInstance(flow.qr_provider, TelegramQrHandler)
        self.assertIsInstance(flow.password_provider, TelegramPasswordProvider)

    def test_sms_builds_tcp_client_with_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = max_client.build_max_client(
                _cfg("sms", tmp, max_phone="+79990000000"),
                bot_token="b", admin_chat_id=555, poll=Mock())
        self.assertIsInstance(client, Client)
        self.assertEqual(client.phone, "+79990000000")

    def test_unknown_method_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                max_client.build_max_client(_cfg("nope", tmp))


if __name__ == "__main__":
    unittest.main()
