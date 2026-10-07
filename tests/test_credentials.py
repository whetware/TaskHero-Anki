from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from taskhero_anki.config import DayBoundary, Settings, SettingsStore
from taskhero_anki.credentials import CredentialStore


TEST_TOKEN = f"th-int-v1-anki-{'A' * 43}"


class CredentialStoreTest(unittest.TestCase):
    def test_save_load_and_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile" / "credentials.json"
            store = CredentialStore(path)
            store.save(f"  {TEST_TOKEN}  ")
            self.assertEqual(store.load(), TEST_TOKEN)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            store.delete()
            self.assertIsNone(store.load())

    def test_rejects_and_ignores_credentials_outside_the_anki_token_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            store = CredentialStore(path)
            for credential in ("th-api-full-account-key", "th-int-v1-anki-short"):
                with self.subTest(credential=credential), self.assertRaisesRegex(ValueError, "valid TaskHero Anki"):
                    store.save(credential)
            path.write_text(json.dumps({"api_key": "legacy-full-api-key"}), encoding="utf-8")
            self.assertIsNone(store.load())

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            settings = Settings(12, 4, 3, "habit-id", "Study Anki", DayBoundary(-300, 4))
            store.save(settings)
            self.assertEqual(store.load(), settings)

    def test_rejects_out_of_range_batch_and_reward(self) -> None:
        with self.assertRaises(ValueError):
            Settings(review_batch_size=9).validate()
        with self.assertRaises(ValueError):
            Settings(reward_points=4).validate()

    def test_clamps_risky_values_without_losing_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "review_batch_size": 1,
                        "daily_batch_goal": 4,
                        "reward_points": 10,
                        "daily_points_cap": 2,
                        "habit_id": "habit-id",
                        "habit_title": "Study Anki",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                SettingsStore(path).load(),
                Settings(10, 4, 3, "habit-id", "Study Anki", day_boundary=None),
            )

    def test_unreadable_settings_pause_rewards_until_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            self.assertIsNone(store.load().day_boundary)
            self.assertIsNone(store.load_error)
            for content in ("{broken", "[]", "null"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    self.assertIsNone(store.load().day_boundary)
                    self.assertIn("paused", store.load_error)
                    store.save(Settings())
                    self.assertIsNone(store.load_error)
                    self.assertEqual(store.load(), Settings())

    def test_invalid_day_rules_preserve_other_preferences_without_guessing_utc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            for boundary in ({"utc_offset_minutes": 9999, "rollover_offset_hours": 4}, {}, "invalid"):
                with self.subTest(boundary=boundary):
                    path.write_text(json.dumps({
                        "review_batch_size": 12, "daily_batch_goal": 4, "reward_points": 3,
                        "habit_id": "habit-id", "habit_title": "Study Anki", "day_boundary": boundary,
                    }), encoding="utf-8")
                    self.assertEqual(store.load(), Settings(12, 4, 3, "habit-id", "Study Anki", None))
                    self.assertIn("Refresh habits", store.load_error)

    def test_old_review_goal_is_not_migrated(self) -> None:
        self.assertEqual(
            Settings.from_mapping({"daily_review_goal": 999}),
            Settings(day_boundary=None),
        )


if __name__ == "__main__":
    unittest.main()
