"""Run with Anki's bundled Python/Qt; skipped by the dependency-free suite."""
from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import MagicMock, patch

from taskhero_anki.config import DayBoundary, Settings

HAS_ANKI = importlib.util.find_spec("aqt") is not None
if HAS_ANKI:
    from aqt.qt import QApplication, QDialogButtonBox, QLabel, QScrollArea, QWidget
    from taskhero_anki.ui import SettingsDialog


class _Controller:
    api_key = ""
    connected = False
    active = True
    settings = Settings()

    def queue_summary(self):
        return "Queued: 0    Failed: 0"

    def save_settings(self, api_key, settings):
        settings.validate()
        self.saved = (api_key, settings)


@unittest.skipUnless(HAS_ANKI, "requires Anki's bundled Python/Qt")
class SettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["taskhero-anki-tests"])

    def setUp(self):
        self.parent = QWidget()
        self.controller = _Controller()
        self.dialog = SettingsDialog(self.controller, self.parent)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.queue_timer.stop()
        self.dialog.close()
        self.parent.close()
        self.dialog.deleteLater()
        self.parent.deleteLater()
        self.app.processEvents()

    def test_explanations_are_visible_wrapped_and_accessible(self):
        fields = (self.dialog.batch_size, self.dialog.reward_points, self.dialog.daily_batch_goal)
        for field in fields:
            help_text = self.dialog.findChild(QLabel, f"{field.accessibleName()}.help")
            self.assertIsNotNone(help_text)
            self.assertTrue(help_text.isVisible())
            self.assertTrue(help_text.wordWrap())
            self.assertEqual(field.accessibleDescription(), help_text.text())
            self.assertEqual(field.toolTip(), help_text.text())
            self.assertLessEqual(help_text.heightForWidth(help_text.width()), help_text.height())

    def test_small_window_scrolls_without_hiding_save_cancel(self):
        font = self.dialog.font()
        font.setPointSize(12)
        self.dialog.setFont(font)
        self.dialog.resize(540, 480)
        self.app.processEvents()
        scroll = self.dialog.findChild(QScrollArea)
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        for button in self.dialog.buttons.buttons():
            bottom = button.mapTo(self.dialog, button.rect().bottomRight())
            self.assertLess(bottom.y(), self.dialog.height())
            self.assertGreaterEqual(button.mapTo(self.dialog, button.rect().topLeft()).y(), 0)
        help_text = self.dialog.findChild(QLabel, "Points per batch.help")
        scroll.ensureWidgetVisible(help_text)
        self.app.processEvents()
        bottom = help_text.mapTo(scroll.viewport(), help_text.rect().bottomRight())
        self.assertLessEqual(bottom.y(), scroll.viewport().height())
        self.assertLessEqual(help_text.heightForWidth(help_text.width()), help_text.height())

    def test_long_local_error_keeps_small_window_actions_reachable(self):
        self.controller.queue_summary = lambda: (
            "Queued: 0    Failed: 0\nLocal error: " + "A local database error occurred. " * 12
            + "\nCheck disk space and profile permissions, then restart Anki."
        )
        self.dialog._refresh_queue_status()
        self.dialog.resize(540, 480)
        self.app.processEvents()
        self.assertLessEqual(self.dialog.height(), 480)
        self.assertLessEqual(self.dialog.width(), 540)
        for button in self.dialog.buttons.buttons():
            self.assertTrue(button.isVisible())
            bottom = button.mapTo(self.dialog, button.rect().bottomRight())
            self.assertLess(bottom.y(), 480)

    def test_save_uses_selected_reward_settings(self):
        self.dialog.api_key.setText("local-test-key")
        self.dialog._connection_tested({"userId": "synthetic", "dayBoundary": {"utcOffsetMinutes": 0, "rolloverOffsetHours": 0}})
        self.dialog._populate_habits([{"id": "habit-1", "title": "Study Anki"}])
        self.dialog.habit_combo.setCurrentIndex(1)
        self.dialog.batch_size.setValue(12)
        self.dialog.daily_batch_goal.setValue(4)
        self.dialog.reward_points.setValue(2)
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.assertEqual(self.controller.saved, ("local-test-key", Settings(12, 4, 2, "habit-1", "Study Anki")))

    def test_connection_day_rules_are_visible_saved_and_cleared_on_token_change(self):
        self.dialog.api_key.setText("local-test-key")
        self.dialog._connection_tested({"userId": "synthetic", "dayBoundary": {"utcOffsetMinutes": -300, "rolloverOffsetHours": 4}})
        self.assertIn("04:00 (UTC−05:00)", self.dialog.day_boundary_label.text())
        self.dialog._populate_habits([{"id": "habit-1", "title": "Study Anki"}])
        self.dialog.habit_combo.setCurrentIndex(1)
        self.dialog._save()
        self.assertEqual(self.controller.saved[1].day_boundary, DayBoundary(-300, 4))
        self.dialog.api_key.setText("other-token")
        self.assertIsNone(self.dialog._day_boundary)

    def test_summary_explains_both_milestones_and_updates_live(self):
        self.dialog._populate_habits([{"id": "habit-1", "title": "Study Anki"}])
        self.dialog.habit_combo.setCurrentIndex(1)
        self.dialog.batch_size.setValue(12)
        self.dialog.reward_points.setValue(2)
        self.dialog.daily_batch_goal.setValue(4)
        summary = self.dialog.reward_summary.text()
        for detail in ("12 reviews", "2 TaskHero points", "4 completed batches", "48 reviews", "Study Anki"):
            self.assertIn(detail, summary)

        self.dialog.batch_size.setValue(15)
        self.dialog.reward_points.setValue(3)
        self.dialog.daily_batch_goal.setValue(2)
        self.dialog._populate_habits([{"id": "habit-2", "title": "Evening study"}])
        self.dialog.habit_combo.setCurrentIndex(1)
        summary = self.dialog.reward_summary.text()
        for detail in ("15 reviews", "3 TaskHero points", "2 completed batches", "30 reviews", "Evening study"):
            self.assertIn(detail, summary)
        self.assertNotIn("Study Anki", summary)

    def test_required_connection_settings_and_visible_numeric_limits(self):
        save = self.dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
        with patch("taskhero_anki.ui.showWarning") as warning:
            save.click()
            self.assertIn("integration token", warning.call_args.args[0])
            self.dialog.api_key.setText("local-test-key")
            save.click()
            self.assertIn("habit", warning.call_args.args[0])
            self.dialog._populate_habits([{"id": "h", "title": "Study Anki"}])
            self.dialog.habit_combo.setCurrentIndex(1)
            save.click()
            self.assertIn("day settings", warning.call_args.args[0])
            self.assertFalse(hasattr(self.controller, "saved"))
        for field, low, high in (
            (self.dialog.batch_size, 10, 100000),
            (self.dialog.reward_points, 1, 3),
            (self.dialog.daily_batch_goal, 1, 100000),
        ):
            field.setValue(-1)
            self.assertEqual(field.value(), low)
            field.setValue(100001)
            self.assertEqual(field.value(), high)

    def test_edited_cancel_does_not_save_and_stops_timer(self):
        self.dialog.api_key.setText("discarded-key")
        self.dialog.batch_size.setValue(999)
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        self.assertFalse(hasattr(self.controller, "saved"))
        self.assertTrue(self.dialog._closed)
        self.assertFalse(self.dialog.queue_timer.isActive())

    def test_missing_saved_habit_is_removed_after_refresh_and_key_change(self):
        self.controller.settings = Settings(habit_id="removed", habit_title="Deleted habit")
        self.dialog._populate_habits([], preserve_saved=True)
        self.assertEqual(self.dialog.habit_combo.currentData(), "removed")
        self.dialog._habits_loaded([])
        self.assertIsNone(self.dialog.habit_combo.currentData())
        self.assertIn("needs replacing", self.dialog.connection_status.text())
        self.assertTrue(self.dialog.habit_warning.isVisible())
        self.assertIn("Deleted habit", self.dialog.habit_warning.text())
        self.assertIn("will not be replayed", self.dialog.habit_warning.text())
        self.dialog._habit_created({"id": "new", "title": "Study Anki"})
        self.assertEqual(self.dialog.habit_combo.currentData(), "new")
        self.assertFalse(self.dialog.habit_warning.isVisible())
        self.dialog.api_key.setText("different-account")
        self.assertIsNone(self.dialog.habit_combo.currentData())

    def test_renamed_saved_habit_remains_selected(self):
        self.controller.settings = Settings(habit_id="same-id", habit_title="Study Anki")
        self.dialog._habits_loaded([{"id": "same-id", "title": "My renamed study habit"}])
        self.assertEqual(self.dialog.habit_combo.currentData(), "same-id")
        self.assertEqual(self.dialog.habit_combo.currentText(), "My renamed study habit")
        self.assertFalse(self.dialog.habit_warning.isVisible())

    def test_no_eligible_habits_without_previous_selection_is_not_a_missing_link_warning(self):
        self.dialog._habits_loaded([])
        self.assertIn("No eligible", self.dialog.connection_status.text())
        self.assertFalse(self.dialog.habit_warning.isVisible())

    def test_busy_dialog_disables_mutations_and_ignores_late_results(self):
        captured = {}
        def query(**kwargs):
            captured.update(kwargs)
            operation = MagicMock()
            operation.without_collection.return_value = operation
            operation.failure.return_value = operation
            captured["operation"] = operation
            return operation
        success = MagicMock()
        with patch("taskhero_anki.ui.QueryOp", side_effect=query):
            self.dialog._run_background("Loading…", lambda: [], success)
        for widget in (self.dialog.api_key, self.dialog.habit_combo, self.dialog.disconnect_button,
                       self.dialog.test_button, self.dialog.refresh_button, self.dialog.create_habit_button,
                       self.dialog.retry_button, self.dialog.buttons.button(QDialogButtonBox.StandardButton.Save)):
            self.assertFalse(widget.isEnabled())
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        captured["success"]([])
        captured["operation"].failure.call_args.args[0](RuntimeError("late"))
        success.assert_not_called()
        self.assertEqual(self.dialog.connection_status.text(), "Loading…")

    def test_background_schedule_failure_restores_buttons(self):
        with patch("taskhero_anki.ui.QueryOp", side_effect=RuntimeError("worker unavailable")):
            self.dialog._run_background("Loading…", lambda: [], lambda _: None)
        self.assertTrue(self.dialog.test_button.isEnabled())
        self.assertIn("worker unavailable", self.dialog.connection_status.text())

    def test_retry_and_disconnect_confirmation(self):
        self.controller.retry_failed = MagicMock(return_value=2)
        self.controller.disconnect = MagicMock(return_value=3)
        with patch("taskhero_anki.ui.tooltip") as tip:
            self.dialog.retry_button.click()
            self.controller.retry_failed.assert_called_once()
            self.assertIn("2 failed", tip.call_args.args[0])
        with patch("taskhero_anki.ui.askUser", return_value=False):
            self.dialog.disconnect_button.click()
            self.controller.disconnect.assert_not_called()
        self.dialog.api_key.setText("local-key")
        with patch("taskhero_anki.ui.askUser", return_value=True):
            self.dialog.disconnect_button.click()
            self.controller.disconnect.assert_called_once()
        self.assertEqual(self.dialog.api_key.text(), "")
        self.assertFalse(self.dialog.api_key.isReadOnly())
        self.assertIn("Cancelled 3", self.dialog.connection_status.text())
