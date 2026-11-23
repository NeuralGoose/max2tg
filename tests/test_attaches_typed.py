"""attaches.parse maps PyMax typed attachments to ParsedAttach items."""
import unittest
from types import SimpleNamespace

import attaches
from pymax.types.domain.attachments.poll import PollAttachment


def _attach(type_value, **fields):
    """Duck-typed stand-in for a PyMax attachment model."""
    return SimpleNamespace(type=type_value, **fields)


def _message(attaches_list):
    return SimpleNamespace(attaches=attaches_list)


class ParseTypedTests(unittest.TestCase):
    def _one(self, attach):
        result = attaches.parse(_message([attach]))
        self.assertEqual(len(result), 1)
        return result[0]

    def test_photo_with_url(self):
        item = self._one(_attach("PHOTO", base_url="https://cdn/p.jpg"))
        self.assertEqual(item.kind, "photo")
        self.assertEqual(item.url, "https://cdn/p.jpg")

    def test_photo_dict_embedded_forward_shape(self):
        item = self._one({
            "_type": "PHOTO",
            "baseUrl": "https://i.oneme.ru/i?r=abc",
        })
        self.assertEqual(item.kind, "photo")
        self.assertEqual(item.url, "https://i.oneme.ru/i?r=abc")

    def test_photo_without_url_becomes_note(self):
        item = self._one(_attach("PHOTO", base_url=None))
        self.assertEqual(item.kind, "note")

    def test_sticker_url(self):
        item = self._one(_attach("STICKER", url="https://cdn/s.webp"))
        self.assertEqual(item.kind, "sticker")
        self.assertEqual(item.url, "https://cdn/s.webp")

    def test_sticker_falls_back_to_lottie(self):
        item = self._one(_attach("STICKER", url=None,
                                 lottie_url="https://cdn/s.json"))
        self.assertEqual(item.kind, "sticker")
        self.assertEqual(item.url, "https://cdn/s.json")

    def test_video_always_resolves(self):
        item = self._one(_attach("VIDEO", video_id=4242))
        self.assertEqual(item.kind, "video_resolve")
        self.assertEqual(item.video_id, 4242)
        self.assertFalse(item.video_note)

    def test_video_type_1_is_video_note(self):
        item = self._one(_attach("VIDEO", video_id=4242, video_type=1))
        self.assertEqual(item.kind, "video_resolve")
        self.assertTrue(item.video_note)
        self.assertIn("Видеосообщение", item.text)

    def test_embedded_forward_video_note(self):
        item = self._one({"_type": "VIDEO", "videoId": 5, "videoType": 1})
        self.assertTrue(item.video_note)

    def test_typed_poll_rendered_as_text(self):
        poll = PollAttachment.model_validate({
            "_type": "POLL",
            "title": "Куда идём?",
            "answers": [{"text": "Кино", "answerId": 1},
                        {"text": "Парк", "answerId": 2}],
            "settings": 0,
            "pollId": 9,
            "version": 1,
            "state": {"total": 3, "voterPreviewIds": [],
                      "result": [{"answerId": 1, "voteCount": 2, "votes": [],
                                  "rate": 66, "options": 0},
                                 {"answerId": 2, "voteCount": 1, "votes": [],
                                  "rate": 33, "options": 0}]},
        })
        item = self._one(poll)
        self.assertEqual(item.kind, "note")
        self.assertEqual(
            item.text, "📊 Опрос: Куда идём?\n1. Кино — 2\n2. Парк — 1")

    def test_poll_dict_without_votes(self):
        item = self._one({"_type": "POLL", "title": "Да?",
                          "answers": [{"text": "Да"}, {"text": "Нет"}]})
        self.assertEqual(item.text, "📊 Опрос: Да?\n1. Да\n2. Нет")

    def test_audio_with_url_is_voice(self):
        item = self._one(_attach("AUDIO", url="https://cdn/a.ogg", duration=5000))
        self.assertEqual(item.kind, "voice")
        self.assertEqual(item.url, "https://cdn/a.ogg")
        self.assertIn("5 с", item.text)

    def test_audio_without_url_is_note(self):
        item = self._one(_attach("AUDIO", url=None, duration=None))
        self.assertEqual(item.kind, "note")

    def test_file_resolves_with_metadata(self):
        item = self._one(_attach("FILE", file_id=99, name="report.pdf",
                                 size=2048))
        self.assertEqual(item.kind, "file_resolve")
        self.assertEqual(item.file_id, 99)
        self.assertEqual(item.filename, "report.pdf")
        self.assertEqual(item.size, 2048)
        self.assertIn("report.pdf", item.text)

    def test_file_sanitizes_path_in_name(self):
        item = self._one(_attach("FILE", file_id=1, name="../../etc/passwd",
                                 size=1))
        self.assertEqual(item.filename, "passwd")

    def test_share_becomes_link(self):
        item = self._one(_attach("SHARE", title="Хабр", url="https://habr.com",
                                 description="статья"))
        self.assertEqual(item.kind, "link")
        self.assertIn("https://habr.com", item.text)

    def test_contact_becomes_note(self):
        item = self._one(_attach("CONTACT", name=None, first_name="Иван",
                                 last_name="Петров"))
        self.assertEqual(item.kind, "note")
        self.assertIn("Иван Петров", item.text)

    def test_control_is_skipped(self):
        self.assertEqual(attaches.parse(_message([_attach("CONTROL")])), [])

    def test_inline_keyboard_is_skipped(self):
        self.assertEqual(
            attaches.parse(_message([_attach("INLINE_KEYBOARD")])), [])

    def test_call_is_note(self):
        item = self._one(_attach("CALL"))
        self.assertEqual(item.kind, "note")

    def test_unknown_type_is_generic_note(self):
        item = self._one(_attach("LOCATION"))
        self.assertEqual(item.kind, "note")
        self.assertIn("LOCATION", item.text)

    def test_empty_message_returns_empty(self):
        self.assertEqual(attaches.parse(_message([])), [])
        self.assertEqual(attaches.parse(SimpleNamespace()), [])

    def test_multiple_attaches_preserved(self):
        result = attaches.parse(_message([
            _attach("PHOTO", base_url="https://cdn/p.jpg"),
            _attach("CONTROL"),
            _attach("FILE", file_id=1, name="a.bin", size=1),
        ]))
        self.assertEqual([i.kind for i in result], ["photo", "file_resolve"])


class RealPymaxModelTests(unittest.TestCase):
    """Validate against real PyMax attachment models (field-name contract)."""

    def test_real_photo_attachment(self):
        from pymax.types.domain import PhotoAttachment

        photo = PhotoAttachment(base_url="https://cdn/p.jpg", height=100,
                                width=100, photo_id=7, photo_token="tok",
                                type="PHOTO")
        item = attaches.parse(SimpleNamespace(attaches=[photo]))[0]
        self.assertEqual(item.kind, "photo")
        self.assertEqual(item.url, "https://cdn/p.jpg")

    def test_real_file_attachment(self):
        from pymax.types.domain import FileAttachment

        file = FileAttachment(file_id=42, name="doc.pdf", size=1234,
                              token="tok", type="FILE")
        item = attaches.parse(SimpleNamespace(attaches=[file]))[0]
        self.assertEqual(item.kind, "file_resolve")
        self.assertEqual(item.file_id, 42)
        self.assertEqual(item.filename, "doc.pdf")
        self.assertEqual(item.size, 1234)


if __name__ == "__main__":
    unittest.main()
