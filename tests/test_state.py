"""BridgeState persistence: lazy path, corrupt recovery, no-topic dedup."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from state import BridgeState


class StatePathTests(unittest.TestCase):
    def test_path_resolved_lazily_from_env(self):
        # A path set after import (e.g. via .env loaded at startup) must still be
        # honored, since main.py applies .env after importing state.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "custom_state.json"
            with patch.dict(os.environ, {"MAX2TG_STATE_PATH": str(target)}):
                bridge_state = BridgeState()
            self.assertEqual(bridge_state.path, target)

    def test_explicit_path_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit.json"
            with patch.dict(os.environ, {"MAX2TG_STATE_PATH": str(Path(tmp) / "env.json")}):
                bridge_state = BridgeState(explicit)
            self.assertEqual(bridge_state.path, explicit)


class StateCorruptTests(unittest.TestCase):
    def test_corrupt_file_is_backed_up_and_state_starts_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not valid json", encoding="utf-8")
            bridge_state = BridgeState(path)
            # State starts fresh but the bad file is preserved for diagnosis.
            self.assertEqual(bridge_state.get_topic(1), None)
            backup = path.with_name(path.name + ".corrupt")
            self.assertTrue(backup.exists())
            self.assertIn("not valid", backup.read_text(encoding="utf-8"))

    def test_malformed_topics_keeps_other_top_level_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({
                "topics": "oops-not-a-dict",
                "pending_preload_chat_ids": ["7", "8"],
            }), encoding="utf-8")
            bridge_state = BridgeState(path)
            # Topics reset to empty, but queued preload work survives.
            self.assertEqual(bridge_state.get_topic(1), None)
            self.assertEqual(
                set(bridge_state.get_pending_preload_chat_ids()), {"7", "8"})


class NoTopicDedupTests(unittest.TestCase):
    def test_delivered_persisted_for_chats_without_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            s1 = BridgeState(path)
            self.assertFalse(s1.is_delivered(555, 1))
            s1.mark_delivered(555, 1)
            self.assertTrue(s1.is_delivered(555, 1))
            # Survives a reload (restart).
            s2 = BridgeState(path)
            self.assertTrue(s2.is_delivered(555, 1))
            self.assertIn("555", s2.delivered_no_topic_map())


if __name__ == "__main__":
    unittest.main()
