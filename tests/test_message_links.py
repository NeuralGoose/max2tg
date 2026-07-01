"""Unit tests for MessageLinkRegistry (SQLite-backed TG↔MAX links)."""
import tempfile
import unittest
from pathlib import Path

from message_links import MessageLinkRegistry


class MessageLinkRegistryTests(unittest.TestCase):
    def _registry(self, tmp: str) -> MessageLinkRegistry:
        return MessageLinkRegistry(Path(tmp) / "links.db")

    def test_link_and_lookup_max_to_tg(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = self._registry(tmp)
            reg.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                message_thread_id=42,
                role="text",
                origin="max_to_tg",
                sender="Иван",
            )
            entries = reg.tg_targets_for_max("555", "1")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["message_id"], 10)
            head = reg.head_tg_target(entries)
            self.assertEqual(head["message_id"], 10)
            reply = reg.reply_target_for_tg(10)
            self.assertEqual(reply["chat_id"], 555)
            self.assertEqual(reply["sender"], "Иван")

    def test_tg_to_max_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = self._registry(tmp)
            reg.link(
                "555", "77",
                telegram_chat_id=111,
                telegram_message_id=99,
                role="text",
                origin="tg_to_max",
            )
            target = reg.max_target_for_tg(99)
            self.assertEqual(target["max_chat_id"], 555)
            self.assertEqual(target["max_message_id"], "77")
            entries = reg.tg_targets_for_max("555", "77")
            head = reg.head_tg_target(entries)
            self.assertEqual(head["message_id"], 99)

    def test_hydrate_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "links.db"
            reg = MessageLinkRegistry(db)
            reg.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                role="text",
                origin="max_to_tg",
            )
            reg.close()
            reg2 = MessageLinkRegistry(db)
            count = reg2.hydrate()
            self.assertEqual(count, 1)
            self.assertTrue(reg2.is_max_linked("555", "1"))

    def test_import_json_mirrors(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = self._registry(tmp)
            mirrors = [
                ("555", "1", [
                    {"telegram_message_id": 10, "role": "text",
                     "message_thread_id": 42},
                ]),
            ]
            imported = reg.import_json_mirrors(
                mirrors, default_telegram_chat_id=-100222,
            )
            self.assertEqual(imported, 1)
            self.assertEqual(
                reg.tg_targets_for_max("555", "1")[0]["message_id"], 10,
            )

    def test_remove_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = self._registry(tmp)
            reg.link(
                "555", "1",
                telegram_chat_id=-100222,
                telegram_message_id=10,
                role="text",
                origin="max_to_tg",
            )
            reg.remove_max("555", "1")
            self.assertFalse(reg.is_max_linked("555", "1"))
            self.assertIsNone(reg.max_target_for_tg(10))


if __name__ == "__main__":
    unittest.main()
