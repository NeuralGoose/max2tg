"""Stage 2: auth-method config parsing and method-aware required keys."""
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import json


@contextlib.contextmanager
def _env(values: dict):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _base(**extra):
    data = {"telegram_bot_token": "t", "telegram_chat_id": 555}
    data.update(extra)
    return data


class AuthMethodNormalizeTests(unittest.TestCase):
    def test_default_method_is_token(self):
        cfg = config.normalize_config(_base(max_login_token="x"))
        self.assertEqual(cfg["max_auth_method"], "token")

    def test_explicit_methods_preserved(self):
        for method in ("token", "sms", "qr"):
            cfg = config.normalize_config(_base(max_auth_method=method))
            self.assertEqual(cfg["max_auth_method"], method)

    def test_method_is_lowercased(self):
        cfg = config.normalize_config(_base(max_auth_method="SMS"))
        self.assertEqual(cfg["max_auth_method"], "sms")

    def test_invalid_method_falls_back_to_token(self):
        cfg = config.normalize_config(_base(max_auth_method="banana"))
        self.assertEqual(cfg["max_auth_method"], "token")


class RequiredKeysTests(unittest.TestCase):
    def test_token_requires_login_token(self):
        keys = config.required_keys("token")
        self.assertIn("max_login_token", keys)
        self.assertNotIn("max_phone", keys)

    def test_sms_requires_phone_not_token(self):
        keys = config.required_keys("sms")
        self.assertIn("max_phone", keys)
        self.assertNotIn("max_login_token", keys)

    def test_qr_requires_only_telegram(self):
        keys = config.required_keys("qr")
        self.assertEqual(set(keys), {"telegram_bot_token", "telegram_chat_id"})

    def test_default_matches_token(self):
        self.assertEqual(config.required_keys(), config.required_keys("token"))


class EnvMapTests(unittest.TestCase):
    def test_auth_env_vars_mapped(self):
        self.assertEqual(config.ENV_MAP["max_auth_method"], "MAX2TG_AUTH_METHOD")
        self.assertEqual(config.ENV_MAP["max_phone"], "MAX2TG_MAX_PHONE")


class LoadConfigMethodTests(unittest.TestCase):
    def _load_with(self, env):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.json"  # no file -> env-only
            with patch.object(config, "CONFIG_PATH", missing), _env(env):
                return config.load_config()

    def test_sms_loads_with_phone(self):
        loaded = self._load_with({
            "MAX2TG_TELEGRAM_BOT_TOKEN": "b",
            "MAX2TG_TELEGRAM_CHAT_ID": "555",
            "MAX2TG_AUTH_METHOD": "sms",
            "MAX2TG_MAX_PHONE": "+79990000000",
        })
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["max_auth_method"], "sms")
        self.assertEqual(loaded["max_phone"], "+79990000000")

    def test_sms_without_phone_is_incomplete(self):
        loaded = self._load_with({
            "MAX2TG_TELEGRAM_BOT_TOKEN": "b",
            "MAX2TG_TELEGRAM_CHAT_ID": "555",
            "MAX2TG_AUTH_METHOD": "sms",
        })
        self.assertIsNone(loaded)

    def test_qr_loads_without_max_credential(self):
        loaded = self._load_with({
            "MAX2TG_TELEGRAM_BOT_TOKEN": "b",
            "MAX2TG_TELEGRAM_CHAT_ID": "555",
            "MAX2TG_AUTH_METHOD": "qr",
        })
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["max_auth_method"], "qr")

    def test_token_without_login_token_is_incomplete(self):
        loaded = self._load_with({
            "MAX2TG_TELEGRAM_BOT_TOKEN": "b",
            "MAX2TG_TELEGRAM_CHAT_ID": "555",
        })
        self.assertIsNone(loaded)

    def test_topics_mode_accepts_fallback_without_chat_id(self):
        loaded = self._load_with({
            "MAX2TG_TELEGRAM_BOT_TOKEN": "b",
            "MAX2TG_TELEGRAM_FALLBACK_CHAT_ID": "76140639",
            "MAX2TG_TELEGRAM_FORUM_CHAT_ID": "-1004455856169",
            "MAX2TG_AUTH_METHOD": "qr",
        })
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["telegram_chat_id"], 76140639)
        self.assertEqual(loaded["telegram_forum_chat_id"], -1004455856169)


class SaveConfigMergeTests(unittest.TestCase):
    def test_save_merges_over_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "telegram_bot_token": "t",
                "telegram_forum_chat_id": -100,
                "telegram_preload_topics": True,
            }), encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path):
                # Wizard-style partial save of only Telegram creds.
                config.save_config({"telegram_bot_token": "new",
                                    "telegram_chat_id": 555})
                saved = json.loads(path.read_text(encoding="utf-8"))
            # Updated keys win; unrelated keys survive.
            self.assertEqual(saved["telegram_bot_token"], "new")
            self.assertEqual(saved["telegram_chat_id"], 555)
            self.assertEqual(saved["telegram_forum_chat_id"], -100)
            self.assertTrue(saved["telegram_preload_topics"])

    def test_save_is_atomic_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_PATH", path):
                config.save_config({"telegram_bot_token": "t"})
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
