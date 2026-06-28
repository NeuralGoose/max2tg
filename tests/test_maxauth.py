"""Stage 2: Telegram-backed PyMax auth providers (maxauth.py)."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

import maxauth


class AuthPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_text_from_admin_chat_and_advances_offset(self):
        batches = [[
            {"update_id": 10, "message": {"chat": {"id": 999}, "text": "spam"}},
            {"update_id": 11, "message": {"chat": {"id": 555}, "text": "123456"}},
        ]]

        def fake_get_updates(token, offset, timeout):
            return batches.pop(0) if batches else []

        with patch.object(maxauth.tg, "get_updates", side_effect=fake_get_updates):
            poll = maxauth.TelegramAuthPoll("TOK", 555)
            text = await poll.wait_for_text(timeout=5)

        self.assertEqual(text, "123456")
        # offset advanced past the last consumed update (11 -> 12)
        self.assertEqual(poll.offset, 12)

    async def test_ignores_other_chats(self):
        def fake_get_updates(token, offset, timeout):
            return [{"update_id": 1,
                     "message": {"chat": {"id": 999}, "text": "nope"}}]

        with patch.object(maxauth.tg, "get_updates", side_effect=fake_get_updates):
            poll = maxauth.TelegramAuthPoll("TOK", 555)
            text = await poll.wait_for_text(timeout=0.2)

        self.assertIsNone(text)
        self.assertEqual(poll.offset, 2)

    async def test_string_chat_id_coerced_to_int(self):
        def fake_get_updates(token, offset, timeout):
            return [{"update_id": 5,
                     "message": {"chat": {"id": 555}, "text": "ok"}}]

        with patch.object(maxauth.tg, "get_updates", side_effect=fake_get_updates):
            poll = maxauth.TelegramAuthPoll("TOK", "555")
            text = await poll.wait_for_text(timeout=5)

        self.assertEqual(text, "ok")

    async def test_drain_advances_offset_past_buffered_updates(self):
        def fake_get_updates(token, offset, timeout):
            return [{"update_id": 5}, {"update_id": 6}]

        with patch.object(maxauth.tg, "get_updates", side_effect=fake_get_updates):
            poll = maxauth.TelegramAuthPoll("TOK", 555)
            await poll.drain()

        self.assertEqual(poll.offset, 7)

    async def test_none_admin_chat_refuses_any_reply(self):
        # Without an admin chat we cannot tell the owner's reply apart from any
        # other user's, so wait_for_text must refuse rather than accept it.
        with patch.object(maxauth.tg, "get_updates") as get_updates:
            poll = maxauth.TelegramAuthPoll("TOK", None)
            text = await poll.wait_for_text(timeout=5)

        self.assertIsNone(text)
        get_updates.assert_not_called()

    async def test_get_updates_conflict_retries_instead_of_aborting(self):
        calls = {"n": 0}

        def fake_get_updates(token, offset, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Telegram getUpdates failed: 409 Conflict")
            return [{"update_id": 1, "message": {"chat": {"id": 555},
                                                 "text": "123456"}}]

        with patch.object(maxauth.tg, "get_updates", side_effect=fake_get_updates), \
                patch.object(maxauth.asyncio, "sleep", new=AsyncMock()):
            poll = maxauth.TelegramAuthPoll("TOK", 555)
            text = await poll.wait_for_text(timeout=30)

        self.assertEqual(text, "123456")
        self.assertEqual(calls["n"], 2)


class SmsCodeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_and_returns_digits_only(self):
        poll = Mock()
        poll.drain = AsyncMock()
        poll.wait_for_text = AsyncMock(return_value="Код: 12 34 56")
        with patch.object(maxauth.tg, "send_message") as send:
            provider = maxauth.TelegramSmsCodeProvider("TOK", 555, poll)
            code = await provider.get_code("+79990000000")

        self.assertEqual(code, "123456")
        send.assert_called_once()

    async def test_skips_non_numeric_then_returns_code(self):
        poll = Mock()
        poll.drain = AsyncMock()
        # A stale slash-command reply must be ignored; the digits come next.
        poll.wait_for_text = AsyncMock(side_effect=["/start", "654321"])
        with patch.object(maxauth.tg, "send_message"):
            provider = maxauth.TelegramSmsCodeProvider("TOK", 555, poll)
            code = await provider.get_code("+79990000000")

        self.assertEqual(code, "654321")
        self.assertEqual(poll.wait_for_text.await_count, 2)

    async def test_timeout_raises(self):
        poll = Mock()
        poll.drain = AsyncMock()
        poll.wait_for_text = AsyncMock(return_value=None)
        with patch.object(maxauth.tg, "send_message"):
            provider = maxauth.TelegramSmsCodeProvider("TOK", 555, poll)
            with self.assertRaises(TimeoutError):
                await provider.get_code("+7")


class PasswordProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_password_text(self):
        poll = Mock()
        poll.drain = AsyncMock()
        poll.wait_for_text = AsyncMock(return_value="  s3cret  ")
        with patch.object(maxauth.tg, "send_message") as send:
            provider = maxauth.TelegramPasswordProvider("TOK", 555, poll)
            password = await provider.get_password(hint="pet")

        self.assertEqual(password, "s3cret")
        send.assert_called_once()
        poll.drain.assert_awaited_once()

    async def test_timeout_raises(self):
        poll = Mock()
        poll.drain = AsyncMock()
        poll.wait_for_text = AsyncMock(return_value=None)
        with patch.object(maxauth.tg, "send_message"):
            provider = maxauth.TelegramPasswordProvider("TOK", 555, poll)
            with self.assertRaises(TimeoutError):
                await provider.get_password()


class QrHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_image_and_link(self):
        with patch.object(maxauth, "_render_qr_png", return_value=b"PNG"), \
             patch.object(maxauth.tg, "send_photo_bytes") as photo, \
             patch.object(maxauth.tg, "send_message") as message:
            handler = maxauth.TelegramQrHandler("TOK", 555)
            result = await handler.show_qr("https://max.ru/qr/abc")

        self.assertIsNone(result)
        photo.assert_called_once()
        message.assert_called_once()
        self.assertIn("https://max.ru/qr/abc", message.call_args.args[2])

    async def test_falls_back_to_link_when_image_fails(self):
        with patch.object(maxauth, "_render_qr_png",
                          side_effect=RuntimeError("no Pillow")), \
             patch.object(maxauth.tg, "send_photo_bytes") as photo, \
             patch.object(maxauth.tg, "send_message") as message:
            handler = maxauth.TelegramQrHandler("TOK", 555)
            await handler.show_qr("https://max.ru/qr/abc")

        photo.assert_not_called()
        message.assert_called_once()


class RenderQrTests(unittest.TestCase):
    def test_render_qr_png_produces_png_bytes(self):
        data = maxauth._render_qr_png("https://max.ru/qr/abc")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
