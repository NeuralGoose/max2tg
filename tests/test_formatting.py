"""Unit tests for MAX ↔ Telegram formatting conversion."""
import unittest

from formatting import (
    FormattedText,
    build_delivery_formatted,
    clip_entities,
    extract_elements,
    max_elements_to_telegram,
    shift_entities,
    split_entities_for_chunk,
    split_text_utf16,
    telegram_entities_to_markdown,
    telegram_message_markdown,
    utf16_len,
    utf16_slice,
)


class Utf16Tests(unittest.TestCase):
    def test_ascii_len(self):
        self.assertEqual(utf16_len("hello"), 5)

    def test_emoji_len(self):
        self.assertEqual(utf16_len("a😀b"), 4)

    def test_utf16_slice_with_emoji(self):
        text = "a😀b"
        self.assertEqual(utf16_slice(text, 1, 2), "😀")


class MaxToTelegramTests(unittest.TestCase):
    def test_basic_mapping(self):
        text = "Hello bold and site"
        elements = [
            {"type": "STRONG", "from": 6, "length": 4},
            {
                "type": "LINK",
                "from": 15,
                "length": 4,
                "attributes": {"url": "https://example.com"},
            },
        ]
        tg = max_elements_to_telegram(text, elements)
        self.assertEqual(
            tg,
            [
                {"type": "bold", "offset": 6, "length": 4},
                {
                    "type": "text_link",
                    "offset": 15,
                    "length": 4,
                    "url": "https://example.com",
                },
            ],
        )

    def test_heading_and_quote_map(self):
        text = "Title\nQuote"
        elements = [
            {"type": "HEADING", "from": 0, "length": 5},
            {"type": "QUOTE", "from": 6, "length": 5},
        ]
        tg = max_elements_to_telegram(text, elements)
        self.assertEqual(tg[0]["type"], "bold")
        self.assertEqual(tg[1]["type"], "blockquote")

    def test_skips_invalid_and_animoji(self):
        text = "hello"
        elements = [
            {"type": "ANIMOJI", "attributes": {}},
            {"type": "STRONG", "from": 0, "length": 99},
            {"type": "LINK", "from": 0, "length": 3, "attributes": {}},
        ]
        self.assertEqual(max_elements_to_telegram(text, elements), [])


class FormattedTextTests(unittest.TestCase):
    def test_with_prefix_shifts_entities(self):
        base = FormattedText.from_max("bold", [{"type": "STRONG", "from": 0, "length": 4}])
        prefixed = base.with_prefix("Ivan:\n")
        self.assertEqual(prefixed.text, "Ivan:\nbold")
        self.assertEqual(prefixed.entities[0]["offset"], utf16_len("Ivan:\n"))

    def test_split_caption_clips_entities(self):
        text = "0123456789"
        entities = [{"type": "bold", "offset": 0, "length": 4}]
        formatted = FormattedText(text, entities)
        caption, overflow = formatted.split_caption(6)
        self.assertEqual(caption.text, "012345")
        self.assertEqual(caption.entities, [{"type": "bold", "offset": 0, "length": 4}])
        self.assertEqual(overflow.text, "6789")
        self.assertEqual(overflow.entities, [])

    def test_build_delivery_non_topic(self):
        base = FormattedText.from_max("hi", [{"type": "STRONG", "from": 0, "length": 2}])
        body = build_delivery_formatted(
            base,
            [],
            in_topic=False,
            sender="Ivan",
            is_channel=False,
            attribution="↪ Ivan",
            header="MAX | Ivan (chat 1)",
        )
        self.assertTrue(body.text.startswith("MAX | Ivan (chat 1)\n↪ Ivan\nhi"))
        self.assertEqual(body.entities[0]["offset"], utf16_len("MAX | Ivan (chat 1)\n↪ Ivan\n"))

    def test_build_delivery_topic_sender_prefix(self):
        base = FormattedText.from_max("hi", [{"type": "STRONG", "from": 0, "length": 2}])
        body = build_delivery_formatted(
            base,
            [],
            in_topic=True,
            sender="Ivan",
            is_channel=False,
            attribution=None,
            header="unused",
        )
        self.assertEqual(body.text, "Ivan:\nhi")
        self.assertEqual(body.entities[0]["offset"], utf16_len("Ivan:\n"))


class TelegramToMaxTests(unittest.TestCase):
    def test_entities_to_markdown(self):
        text = "Hello bold"
        entities = [{"type": "bold", "offset": 6, "length": 4}]
        self.assertEqual(
            telegram_entities_to_markdown(text, entities),
            "Hello **bold**",
        )

    def test_text_link_to_markdown(self):
        text = "visit site"
        entities = [{
            "type": "text_link",
            "offset": 6,
            "length": 4,
            "url": "https://example.com",
        }]
        self.assertEqual(
            telegram_entities_to_markdown(text, entities),
            "visit [site](https://example.com)",
        )

    def test_blockquote_to_markdown(self):
        text = "quoted"
        entities = [{"type": "blockquote", "offset": 0, "length": 6}]
        self.assertEqual(
            telegram_entities_to_markdown(text, entities),
            "> quoted",
        )

    def test_non_overlapping_entities(self):
        text = "bold then italic"
        entities = [
            {"type": "bold", "offset": 0, "length": 4},
            {"type": "italic", "offset": 10, "length": 6},
        ]
        result = telegram_entities_to_markdown(text, entities)
        self.assertEqual(result, "**bold** then _italic_")

    def test_telegram_message_markdown_from_update(self):
        message = {
            "text": "Hello",
            "entities": [{"type": "bold", "offset": 0, "length": 5}],
        }
        self.assertEqual(telegram_message_markdown(message), "**Hello**")


class SplitHelpersTests(unittest.TestCase):
    def test_split_text_utf16(self):
        text = "a" * 4094 + "😀"
        chunks = split_text_utf16(text, 4096)
        self.assertEqual(len(chunks), 1)
        long_text = "a" * 4096 + "b"
        chunks = split_text_utf16(long_text, 4096)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "a" * 4096)

    def test_split_entities_for_chunk(self):
        entities = [{"type": "bold", "offset": 10, "length": 3}]
        shifted = split_entities_for_chunk(entities, 10, 5)
        self.assertEqual(shifted, [{"type": "bold", "offset": 0, "length": 3}])

    def test_shift_and_clip(self):
        entities = [{"type": "bold", "offset": 2, "length": 2}]
        self.assertEqual(
            shift_entities(entities, 5),
            [{"type": "bold", "offset": 7, "length": 2}],
        )
        self.assertEqual(
            clip_entities([{"type": "bold", "offset": 0, "length": 5}], 4),
            [],
        )


class ExtractElementsTests(unittest.TestCase):
    def test_extract_from_dict(self):
        message = {
            "text": "x",
            "elements": [{"type": "STRONG", "from": 0, "length": 1}],
        }
        self.assertEqual(len(extract_elements(message)), 1)


if __name__ == "__main__":
    unittest.main()
