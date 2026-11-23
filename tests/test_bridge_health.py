"""MAX connection health: outage alerts, recovery notes, outdated-client warning."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import bridge as bridge_module
from bridge import MaxToTelegramBridge
from state import BridgeState


class MaxHealthTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, tmp):
        state_path = Path(tmp) / "state.json"
        bridge = MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
            "links_db_path": str(state_path.with_name("links.db")),
        })
        bridge._state = BridgeState(state_path)
        bridge._notify_admin = AsyncMock()
        return bridge

    async def test_short_outage_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(bridge_module, "MAX_DOWN_ALERT_SECONDS", 0.05):
            bridge = self._bridge(tmp)
            await bridge._on_max_disconnect(TimeoutError("handshake"), True, 5)
            await bridge._note_max_up()
            await asyncio.sleep(0.1)
            bridge._notify_admin.assert_not_awaited()

    async def test_persistent_outage_alerts_once_then_announces_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(bridge_module, "MAX_DOWN_ALERT_SECONDS", 0.05):
            bridge = self._bridge(tmp)
            await bridge._on_max_disconnect(TimeoutError("first"), True, 5)
            await bridge._on_max_disconnect(TimeoutError("latest"), True, 5)
            await asyncio.sleep(0.1)
            bridge._notify_admin.assert_awaited_once()
            alert = bridge._notify_admin.await_args.args[0]
            self.assertIn("MAX недоступен", alert)
            self.assertIn("TimeoutError: latest", alert)

            await bridge._note_max_up()
            self.assertEqual(bridge._notify_admin.await_count, 2)
            self.assertIn("восстановлено",
                          bridge._notify_admin.await_args.args[0])
            self.assertIsNone(bridge._max_down_since)

    async def test_outdated_client_warns_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = Mock()
            client.is_update_available = Mock(return_value=True)
            await bridge._warn_if_max_client_outdated(client)
            await bridge._warn_if_max_client_outdated(client)
            bridge._notify_admin.assert_awaited_once()
            self.assertIn("maxapi-python",
                          bridge._notify_admin.await_args.args[0])

    async def test_current_client_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            client = Mock()
            client.is_update_available = Mock(return_value=False)
            await bridge._warn_if_max_client_outdated(client)
            bridge._notify_admin.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
