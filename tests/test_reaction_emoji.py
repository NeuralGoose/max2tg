"""Tests for MAX → Telegram reaction emoji normalization."""
import unittest

from reaction_emoji import (
    normalize_max_reaction_for_telegram,
    strip_emoji_variation,
)


class ReactionEmojiTests(unittest.TestCase):
    def test_common_emojis_pass_through(self):
        self.assertEqual(normalize_max_reaction_for_telegram("👍"), "👍")
        self.assertEqual(normalize_max_reaction_for_telegram("🔥"), "🔥")

    def test_heart_variant_normalized(self):
        self.assertEqual(normalize_max_reaction_for_telegram("❤️"), "❤")

    def test_love_you_gesture_mapped(self):
        self.assertEqual(normalize_max_reaction_for_telegram("🤟"), "❤")

    def test_unknown_returns_none(self):
        self.assertIsNone(normalize_max_reaction_for_telegram("🦞"))

    def test_strip_variation_selector(self):
        self.assertEqual(strip_emoji_variation("❤️"), "❤")


if __name__ == "__main__":
    unittest.main()
