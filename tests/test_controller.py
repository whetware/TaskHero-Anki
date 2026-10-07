"""Controller lifecycle tests using Anki's real Qt and temporary profile files."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from taskhero_anki.config import Settings
from taskhero_anki.engine import process_next_event
from taskhero_anki.storage import ReviewerProgress


TEST_TOKEN = f"th-int-v1-anki-{'A' * 43}"
OTHER_TEST_TOKEN = f"th-int-v1-anki-{'B' * 43}"

HAS_ANKI = importlib.util.find_spec("aqt") is not None
if HAS_ANKI:
    from aqt.qt import QApplication, QEvent, QEventLoop, QTimer, QWidget
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from taskhero_anki.addon import AddonController
    from taskhero_anki.ui import SettingsDialog


@unittest.skipUnless(HAS_ANKI, "requires Anki's bundled Python/Qt")
class ControllerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["taskhero-anki-tests"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.main = QWidget()
        self.main.pm = MagicMock()
        self.main.pm.profileFolder.return_value = self.temp.name
        self.main.col = MagicMock()
        self.main.col.db.scalar.return_value = 100
        self.main.state = "deckBrowser"
        self.patch = patch("taskhero_anki.addon.mw", self.main)
        self.patch.start()
        self.controller = AddonController()
        self.controller.tick = MagicMock()
        self.settings = Settings(habit_id="habit", habit_title="Study Anki")

    def tearDown(self):
        self.controller.shutdown()
        self.patch.stop()
        self.main.close()
        self.temp.cleanup()

    def test_save_persists_and_key_replacement_requires_disconnect(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        self.assertEqual(self.controller.credential_store.load(), TEST_TOKEN)
        self.assertEqual(self.controller.settings_store.load(), self.settings)
        with self.assertRaisesRegex(ValueError, "Disconnect"):
            self.controller.save_settings(OTHER_TEST_TOKEN, self.settings)
        self.assertEqual(self.controller.credential_store.load(), TEST_TOKEN)

    def test_start_retires_old_unsent_habit_after_interrupted_relink(self):
        self.controller.api_key = TEST_TOKEN
        self.controller.settings = Settings(habit_id="new-habit", habit_title="New")
        with (
            patch.object(self.controller.store, "cancel_unsent_habit_events_for_other_habit") as cancel,
            patch.object(self.controller, "_install_menu"),
            patch.object(self.controller, "_ensure_revlog_baseline"),
            patch.object(self.controller, "scan_new_revlog_entries"),
        ):
            self.controller.start()
        self.assertEqual(cancel.call_args.args[0], "new-habit")

    def _seed_reward_session(self, controller, now_ms):
        controller.tick = MagicMock()
        controller.save_settings(TEST_TOKEN, self.settings)
        ids = set(range(now_ms - 100, now_ms - 51))
        store = controller.store
        store.record_reviews([(i, -1) for i in ids], "desktop", now_ms, now_ms, self.settings.day_boundary)
        store.finalize_reviews(store.due_pending_reviews(now_ms), ids)
        day = self.settings.day_boundary.day(now_ms)
        self.assertEqual(len(store.materialize_events(day, self.settings, now_ms)), 5)
        sent = store.claim_next_event(now_ms)
        store.mark_sent(sent, {"trackerPoint": {"id": "synthetic-receipt"}}, now_ms)
        failed = store.claim_next_event(now_ms)
        store.mark_failed(failed.event_id, "Synthetic permanent failure", now_ms)
        store.claim_next_event(now_ms)  # Interrupted request: recovery must still require a reset.
        store.record_review(now_ms + 1, -1, "desktop", now_ms + 10_000, now_ms, self.settings.day_boundary)
        return day, sent.event_id

    def test_unavailable_credentials_require_disconnect_before_reconnecting(self):
        now_ms = 1_791_288_000_000
        for failure in ("missing", "malformed", "invalid-token", "unreadable"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.temp.name) as profile:
                self.main.pm.profileFolder.return_value = profile
                previous = AddonController()
                with patch("taskhero_anki.addon._now_ms", return_value=now_ms):
                    day, sent_id = self._seed_reward_session(previous, now_ms)
                previous.shutdown()
                credential = previous.credential_store.path
                if failure == "missing":
                    credential.unlink()
                elif failure == "malformed":
                    credential.write_text("{broken", encoding="utf-8")
                elif failure == "invalid-token":
                    credential.write_text('{"api_key":"invalid"}', encoding="utf-8")
                original_open = Path.open

                def open_without_credential(path, *args, **kwargs):
                    if path == credential:
                        raise PermissionError("synthetic unreadable credential")
                    return original_open(path, *args, **kwargs)

                with (patch.object(Path, "open", open_without_credential) if failure == "unreadable" else nullcontext()):
                    reopened = AddonController()
                reopened.tick = MagicMock()
                try:
                    self.assertFalse(reopened.connected)
                    self.assertTrue(reopened.needs_connection_reset)
                    self.assertIn("Choose Disconnect", reopened.queue_summary())
                    before = reopened.store.event_statuses()
                    replacement = Settings(habit_id="new-habit", habit_title="New account habit")
                    with patch.object(reopened.credential_store, "save") as save, self.assertRaisesRegex(ValueError, "Disconnect"):
                        reopened.save_settings(OTHER_TEST_TOKEN, replacement)
                    save.assert_not_called()
                    self.assertEqual(reopened.store.event_statuses(), before)
                    with patch("taskhero_anki.addon.QueryOp") as query:
                        reopened._start_worker()
                    query.assert_not_called()

                    self.assertEqual(reopened.disconnect(), 4)
                    self.assertFalse(reopened.needs_connection_reset)
                    with patch("taskhero_anki.addon._now_ms", return_value=now_ms):
                        reopened.save_settings(OTHER_TEST_TOKEN, replacement)
                    self.assertEqual(reopened.api_key, OTHER_TEST_TOKEN)
                    self.assertEqual(reopened.store.reviewer_progress(day, replacement), ReviewerProgress(0, 10, 0))
                    self.assertEqual(reopened.store.due_pending_reviews(2**63 - 1), [])
                    client = MagicMock()
                    self.assertEqual(process_next_event(reopened.store, client, now_ms).outcome, "idle")
                    self.assertEqual(client.mock_calls, [])
                    with closing(sqlite3.connect(reopened.store.path)) as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM review_events").fetchone()[0], 50)
                        self.assertEqual(connection.execute("SELECT event_id, remote_id FROM sent_events").fetchall(),
                                         [(sent_id, "synthetic-receipt")])
                    self.assertEqual(sorted(status for _, status in reopened.store.event_statuses()),
                                     ["cancelled"] * 4 + ["sent"])
                    reopened.store.record_review(now_ms + 100, -1, "desktop", now_ms, now_ms, replacement.day_boundary)
                    reopened.store.finalize_reviews(reopened.store.due_pending_reviews(now_ms), {now_ms + 100})
                    self.assertEqual(reopened.store.materialize_events(day, replacement, now_ms), [])
                    self.assertEqual(reopened.store.reviewer_progress(day, replacement), ReviewerProgress(1, 10, 0))
                    new_ids = set(range(now_ms + 101, now_ms + 110))
                    reopened.store.record_reviews([(i, -1) for i in new_ids], "desktop", now_ms, now_ms, replacement.day_boundary)
                    reopened.store.finalize_reviews(reopened.store.due_pending_reviews(now_ms), new_ids)
                    new_events = reopened.store.materialize_events(day, replacement, now_ms)
                    self.assertEqual(len(new_events), 1)
                    self.assertNotIn(new_events[0], {event_id for event_id, _ in before})
                    client.create_tracker_points.return_value = {"trackerPoint": {"id": "new-account-point"}}
                    self.assertEqual(process_next_event(reopened.store, client, now_ms).outcome, "sent")
                    client.create_tracker_points.assert_called_once_with(
                        1, "Completed 10 reviews", new_events[0], "Anki", new_events[0],
                    )
                    client.complete_habit.assert_not_called()
                finally:
                    reopened.shutdown()

    def test_valid_credential_restart_preserves_session_and_retry_payloads(self):
        now_ms = 1_791_288_000_000
        with patch("taskhero_anki.addon._now_ms", return_value=now_ms):
            day, _ = self._seed_reward_session(self.controller, now_ms)
        original_progress = self.controller.store.reviewer_progress(day, self.settings)
        with closing(sqlite3.connect(self.controller.store.path)) as connection:
            expected = {
                row[0]: json.loads(row[1]) for row in connection.execute(
                    "SELECT event_id, payload_json FROM outbound_events WHERE status != 'sent'"
                )
            }
        self.controller.shutdown()
        reopened = AddonController()
        try:
            self.assertEqual(reopened.api_key, TEST_TOKEN)
            self.assertFalse(reopened.needs_connection_reset)
            self.assertEqual(reopened.store.reviewer_progress(day, reopened.settings), original_progress)
            reopened.store.retry_failed(now_ms)
            client = MagicMock()
            client.create_tracker_points.return_value = {"trackerPoint": {"id": "synthetic-point"}}
            client.complete_habit.return_value = {"task": {"id": self.settings.habit_id}}
            for _ in expected:
                self.assertEqual(process_next_event(reopened.store, client, now_ms).outcome, "sent")
            self.assertEqual(process_next_event(reopened.store, client, now_ms).outcome, "idle")
            delivered = {}
            for call in client.mock_calls:
                args = call.args
                if call[0] == "create_tracker_points":
                    delivered[args[4]] = dict(value=args[0], description=args[1], sourceId=args[2],
                                              sourceName=args[3], idempotencyKey=args[4])
                else:
                    self.assertEqual(call[0], "complete_habit")
                    delivered[args[3]] = dict(habitId=args[0], completedAt=args[1], sourceName=args[2],
                                              idempotencyKey=args[3], onlyIfIncomplete=True)
            self.assertEqual(delivered, expected)
        finally:
            reopened.shutdown()

    def test_recovery_dialog_requires_confirmed_disconnect_and_allows_fresh_save(self):
        self.controller.store.record_review(200, -1, "desktop", 0, 200)
        dialog = SettingsDialog(self.controller, self.main)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertTrue(dialog.queue_status.isVisible())
            self.assertIn("Choose Disconnect", dialog.queue_status.text())
            with patch("taskhero_anki.ui.askUser", return_value=False):
                dialog.disconnect_button.click()
            self.assertTrue(self.controller.needs_connection_reset)

            def fill_connection():
                dialog.api_key.setText(OTHER_TEST_TOKEN)
                dialog._connection_tested({"dayBoundary": {"utcOffsetMinutes": 0, "rolloverOffsetHours": 0}})
                dialog._populate_habits([{"id": "new", "title": "New account habit"}])
                dialog.habit_combo.setCurrentIndex(1)

            fill_connection()
            with patch("taskhero_anki.ui.showWarning") as warning:
                dialog._save()
            self.assertIn("Disconnect", warning.call_args.args[0])
            self.assertTrue(dialog.isVisible())
            self.assertFalse(self.controller.connected)
            with patch("taskhero_anki.ui.askUser", return_value=True):
                dialog.disconnect_button.click()
            self.assertFalse(self.controller.needs_connection_reset)
            self.assertNotIn("Choose Disconnect", dialog.queue_status.text())
            fill_connection()
            dialog._save()
            self.assertEqual(self.controller.api_key, OTHER_TEST_TOKEN)
            self.assertFalse(dialog.isVisible())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_saving_new_habit_retires_old_unsent_completion(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        replacement = Settings(habit_id="new-habit", habit_title="New")
        with patch.object(self.controller.store, "cancel_unsent_habit_events_for_other_habit") as cancel:
            self.controller.save_settings(TEST_TOKEN, replacement)
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], "new-habit")
        self.assertEqual(self.controller.settings_store.load(), replacement)

    def test_reviewer_badge_renders_resizes_and_updates_without_duplicates(self):
        self.controller.api_key = TEST_TOKEN
        self.controller.settings = Settings(daily_batch_goal=3, habit_id="habit")
        self.main.state = "review"
        self.main.reviewer = MagicMock()
        view = QWebEngineView()
        view.resize(700, 120)
        view.show()
        try:
            def load(done):
                view.loadFinished.connect(done)
                view.setHtml("<html><head><style>body { margin: 0 }</style></head><body></body></html>")

            self.assertTrue(self._wait_for_web(load))
            self.main.reviewer.bottom.web.eval.side_effect = lambda script: self._wait_for_web(
                lambda done: view.page().runJavaScript(script, done)
            )
            with patch.object(self.controller.store, "reviewer_progress", return_value=ReviewerProgress(7, 10, 1)) as progress:
                self.controller.update_reviewer_status()
                wide = self._badge_at_width(view, 700)
                self.assertIn("Reviews 7/10", wide["text"])
                self.assertIn("Batches 1/3", wide["text"])
                self.assertEqual(wide["ariaLabel"], wide["text"])
                self.assertEqual(wide["role"], "status")
                self.assertEqual(wide["state"], "connected")

                view.resize(480, 120)
                compact = self._badge_at_width(view, 480, "R7/10")
                self.assertIn("B1/3", compact["text"])
                self.assertEqual(compact["ariaLabel"], wide["text"])

                progress.return_value = ReviewerProgress(0, 10, 4)
                self.controller.update_reviewer_status()
                completed = self._badge_at_width(view, 480, "R0/10")
                self.assertIn("B3/3", completed["text"])
                self.assertIn("✓", completed["text"])
                self.assertIn("Batches 3/3", completed["ariaLabel"])

                view.resize(700, 120)
                expanded = self._badge_at_width(view, 700, "Reviews 0/10")
                self.assertEqual(expanded["text"], completed["ariaLabel"])
                self.controller.api_key = None
                self.controller.update_reviewer_status()
                disconnected = self._badge_at_width(view, 700)
                self.assertEqual(disconnected["state"], "disconnected")
                self.assertIn("Disconnected", disconnected["text"])
        finally:
            view.close()
            view.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _wait_for_web(self, start, ready=lambda _value: True):
        loop = QEventLoop()
        deadline = QTimer(loop)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll = QTimer(loop)
        poll.setSingleShot(True)
        results = []
        last = []
        active = True

        def received(value):
            if not active:
                return
            last[:] = [value]
            if ready(value):
                results.append(value)
                loop.quit()
            else:
                poll.start(20)

        poll.timeout.connect(lambda: start(received))
        deadline.start(5000)
        start(received)
        if not results:
            loop.exec()
        active = False
        poll.stop()
        deadline.stop()
        self.assertTrue(results, f"Browser did not reach the expected state; last result: {last!r}")
        return results[0]

    def _badge_at_width(self, view, width, text_fragment=""):
        snapshot = """
            (() => {
                const badge = document.getElementById('taskhero-anki-status');
                if (!badge) return null;
                const rect = badge.getBoundingClientRect();
                const style = getComputedStyle(badge);
                return {
                    width: document.body.clientWidth,
                    count: document.querySelectorAll('#taskhero-anki-status').length,
                    text: badge.textContent,
                    ariaLabel: badge.getAttribute('aria-label'),
                    role: badge.getAttribute('role'),
                    state: badge.dataset.state,
                    visible: rect.width > 0 && rect.height > 0 && rect.left >= 0 && rect.top >= 0
                        && rect.right <= innerWidth && rect.bottom <= innerHeight
                        && style.display !== 'none' && style.visibility === 'visible' && style.opacity !== '0'
                };
            })()
        """
        result = self._wait_for_web(
            lambda done: view.page().runJavaScript(snapshot, done),
            lambda value: value is not None and value["width"] == width and text_fragment in value["text"],
        )
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["visible"])
        return result

    def test_closed_profile_and_in_flight_reconnect_are_rejected(self):
        self.controller._worker_busy = True
        with self.assertRaisesRegex(ValueError, "in-flight"):
            self.controller.save_settings(TEST_TOKEN, self.settings)
        self.controller._worker_busy = False
        self.controller.shutdown()
        with self.assertRaisesRegex(ValueError, "profile has closed"):
            self.controller.save_settings(TEST_TOKEN, self.settings)
        self.assertIsNone(self.controller.credential_store.load())

    def test_failed_settings_write_rolls_back_new_credential(self):
        with patch.object(self.controller.settings_store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.controller.save_settings(TEST_TOKEN, self.settings)
        self.assertIsNone(self.controller.credential_store.load())
        self.assertFalse(self.controller.connected)

    def test_invalid_saved_day_rules_pause_accounting_and_sending_until_saved(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        self.controller.store.record_review(200, -1, "desktop", 0, 200)
        self.controller.settings_store.path.write_text(json.dumps({
            "review_batch_size": 12, "habit_id": "habit", "habit_title": "Study Anki",
            "day_boundary": {"utc_offset_minutes": 9999, "rollover_offset_hours": 4},
        }), encoding="utf-8")
        reopened = AddonController()
        try:
            self.assertTrue(reopened.connected)
            self.assertIsNone(reopened.settings.day_boundary)
            self.assertEqual(reopened.settings.review_batch_size, 12)
            self.assertEqual(reopened.settings.habit_id, "habit")
            self.assertIn("Rewards are paused", reopened.queue_summary())
            with patch.object(reopened.store, "record_reviews") as record, patch("taskhero_anki.addon.QueryOp") as query:
                reopened.scan_new_revlog_entries()
                reopened.record_local_review(MagicMock(id=1))
                reopened.tick()
                record.assert_not_called()
                query.assert_not_called()
            self.assertEqual([review.revlog_id for review in reopened.store.due_pending_reviews(2**63 - 1)], [200])
            self.assertEqual(reopened.status_text()[1], "failed")
            with patch.object(reopened, "tick"):
                reopened.save_settings(TEST_TOKEN, self.settings)
            self.assertNotIn("paused", reopened.queue_summary())
            self.assertEqual(reopened.settings.day_boundary, self.settings.day_boundary)
        finally:
            reopened.shutdown()

    def test_worker_failure_is_visible_in_settings_without_exposing_token(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        self.controller._worker_busy = True
        self.controller._worker_lock.acquire()
        self.controller._worker_failed(OSError(f"disk full; synthetic context {TEST_TOKEN}"))
        summary = self.controller.queue_summary()
        self.assertIn("disk full", summary)
        self.assertIn("restart Anki", summary)
        self.assertIn("support@taskheroics.com", summary)
        self.assertNotIn(TEST_TOKEN, summary)
        self.assertEqual(self.controller.status_text()[1], "failed")

    def test_disconnected_scans_advance_without_recording_reviews(self):
        self.main.col.db.all.return_value = [(200, 2, 3), (300, 3, 0)]
        self.controller.scan_new_revlog_entries()
        self.assertEqual(self.controller.store.scan_cursors(), (300, 3))
        self.assertEqual(self.controller.store.due_pending_reviews(2**63 - 1), [])
        self.controller.record_local_review(MagicMock())
        self.main.col.db.first.assert_not_called()

    def test_sync_counts_all_answer_ratings_but_not_rescheduling(self):
        self.controller.api_key = TEST_TOKEN
        self.controller.settings = self.settings
        with closing(sqlite3.connect(":memory:")) as collection:
            collection.execute("CREATE TABLE revlog (id INTEGER, usn INTEGER, ease INTEGER, cid INTEGER)")
            collection.executemany("INSERT INTO revlog VALUES (?, ?, ?, 1)", [
                (200, 2, 1), (300, 3, 2), (400, 4, 3), (500, 5, 4), (600, 6, 0),
            ])
            self.main.col.db.all.side_effect = lambda sql, *args: collection.execute(sql, args).fetchall()
            self.controller.scan_new_revlog_entries()
            self.assertEqual(self.controller.store.scan_cursors(), (600, 6))
            pending = self.controller.store.due_pending_reviews(2**63 - 1)
            self.assertEqual([review.revlog_id for review in pending], [200, 300, 400, 500])

            # A synced mobile review may have an older timestamp but a newer USN.
            collection.execute("INSERT INTO revlog VALUES (100, 7, 1, 2)")
            collection.execute("INSERT INTO revlog VALUES (700, 8, 0, 2)")
            self.controller.scan_new_revlog_entries()
            self.controller.scan_new_revlog_entries()
            pending = self.controller.store.due_pending_reviews(2**63 - 1)
            self.assertEqual({review.revlog_id for review in pending}, {100, 200, 300, 400, 500})
            self.assertEqual(self.controller.store.scan_cursors(), (700, 8))

    def test_local_review_ignores_rescheduling_and_other_cards(self):
        self.controller.api_key = TEST_TOKEN
        self.controller.settings = self.settings
        with closing(sqlite3.connect(":memory:")) as collection:
            collection.execute("CREATE TABLE revlog (id INTEGER, usn INTEGER, ease INTEGER, cid INTEGER)")
            collection.executemany("INSERT INTO revlog VALUES (?, 1, ?, ?)", [
                (200, 3, 1), (300, 0, 1), (400, 4, 2),
            ])
            self.main.col.db.first.side_effect = lambda sql, *args: collection.execute(sql, args).fetchone()
            self.controller.record_local_review(MagicMock(id=1))
            pending = self.controller.store.due_pending_reviews(2**63 - 1)
            self.assertEqual([review.revlog_id for review in pending], [200])

    def test_first_connection_sets_baseline_and_other_profiles_stay_disconnected(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        self.assertEqual(self.controller.store.scan_cursors(), (100, 100))
        original_id = self.controller.store.profile_id()
        self.main.pm.profileFolder.return_value = str(Path(self.temp.name) / "second")
        other = AddonController()
        try:
            self.assertFalse(other.connected)
            self.assertEqual(other.settings, Settings(day_boundary=None))
            self.assertNotEqual(other.store.profile_id(), original_id)
        finally:
            other.shutdown()

    def test_disconnect_stops_even_if_credential_removal_fails(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        with patch.object(self.controller.credential_store, "delete", side_effect=PermissionError("readonly")):
            with self.assertRaisesRegex(OSError, "stopped for this session"):
                self.controller.disconnect()
        self.assertFalse(self.controller.connected)
        self.controller.disconnect()
        self.assertIsNone(self.controller.credential_store.load())

    def test_schedule_failure_releases_worker_lock(self):
        self.controller.save_settings(TEST_TOKEN, self.settings)
        with patch.object(self.controller.store, "has_due_event", return_value=True), patch("taskhero_anki.addon.QueryOp") as query:
            operation = query.return_value
            operation.without_collection.return_value = operation
            operation.failure.return_value = operation
            operation.run_in_background.side_effect = RuntimeError("schedule failed")
            with self.assertRaisesRegex(RuntimeError, "schedule failed"):
                self.controller._start_worker()
        self.assertFalse(self.controller._worker_busy)
        self.assertFalse(self.controller._worker_lock.locked())
