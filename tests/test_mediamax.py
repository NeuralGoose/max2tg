"""Stage 6: mediamax over PyMax typed send (Photo/Video/File) + resolve."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import mediamax
from pymax import File, Photo, Video
from pymax.types.domain.attachments.video import VideoRequest


class SendUploadedMediaTests(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        client = Mock()
        client.send_message = AsyncMock(return_value=None)
        return client

    async def test_photo_builds_photo_attachment(self):
        client = self._client()
        await mediamax.send_uploaded_media(
            client, 555, b"img", "telegram-photo.jpg", "image/jpeg",
            kind="photo", text="cap", reply_to_message_id=7)
        args, kwargs = client.send_message.await_args
        self.assertEqual(args[0], 555)
        self.assertEqual(args[1], "cap")
        self.assertEqual(kwargs["reply_to"], 7)
        attachment = kwargs["attachments"][0]
        self.assertIsInstance(attachment, Photo)
        self.assertEqual(attachment.raw, b"img")
        self.assertEqual(attachment.name, "telegram-photo.jpg")

    async def test_video_builds_video_attachment(self):
        client = self._client()
        await mediamax.send_uploaded_media(
            client, 555, b"vid", "telegram-video.mp4", "video/mp4", kind="video")
        attachment = client.send_message.await_args.kwargs["attachments"][0]
        self.assertIsInstance(attachment, Video)
        self.assertEqual(attachment.raw, b"vid")

    async def test_default_kind_builds_file_attachment(self):
        client = self._client()
        await mediamax.send_uploaded_media(
            client, 555, b"doc", "report.pdf", "application/pdf")
        attachment = client.send_message.await_args.kwargs["attachments"][0]
        self.assertIsInstance(attachment, File)
        self.assertEqual(attachment.name, "report.pdf")

    async def test_no_reply_defaults_to_none_and_empty_text(self):
        client = self._client()
        await mediamax.send_uploaded_media(
            client, 555, b"x", "f.bin", kind="file")
        args, kwargs = client.send_message.await_args
        self.assertEqual(args[1], "")
        self.assertIsNone(kwargs["reply_to"])


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_file_url(self):
        client = Mock()
        client.get_file_by_id = AsyncMock(
            return_value=SimpleNamespace(url="https://cdn/file?exp=1"))
        url = await mediamax.resolve_file_url(client, 99, 555, 4242)
        self.assertEqual(url, "https://cdn/file?exp=1")
        client.get_file_by_id.assert_awaited_once_with(555, 4242, 99)

    async def test_resolve_file_url_missing_raises(self):
        client = Mock()
        client.get_file_by_id = AsyncMock(return_value=None)
        with self.assertRaises(RuntimeError):
            await mediamax.resolve_file_url(client, 99, 555, 4242)

    async def test_resolve_video_url(self):
        client = Mock()
        client.get_video_by_id = AsyncMock(
            return_value=SimpleNamespace(url="https://cdn/video.mp4"))
        url = await mediamax.resolve_video_url(client, 7, 555, 4242)
        self.assertEqual(url, "https://cdn/video.mp4")
        client.get_video_by_id.assert_awaited_once_with(555, 4242, 7)

    async def test_resolve_video_url_missing_raises(self):
        client = Mock()
        client.get_video_by_id = AsyncMock(
            return_value=SimpleNamespace(url=None))
        with self.assertRaises(RuntimeError):
            await mediamax.resolve_video_url(client, 7, 555, 4242)

    async def test_video_note_renditions_resolve_via_typed_model(self):
        """Video notes return only a rendition map (no ``cache``); PyMax's
        VideoRequest must still pick the best MP4 URL."""
        request = VideoRequest.model_validate(
            {"MP4_480": "https://v/480.mp4", "MP4_240": "https://v/240.mp4"})
        client = Mock()
        client.get_video_by_id = AsyncMock(return_value=request)
        url = await mediamax.resolve_video_url(client, 7, 555, 4242)
        self.assertEqual(url, "https://v/480.mp4")

    async def test_external_video_falls_back_to_external_link(self):
        request = VideoRequest.model_validate({"EXTERNAL": "https://ext/v"})
        client = Mock()
        client.get_video_by_id = AsyncMock(return_value=request)
        url = await mediamax.resolve_video_url(client, 7, 555, 4242)
        self.assertEqual(url, "https://ext/v")


if __name__ == "__main__":
    unittest.main()
