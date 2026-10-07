"""Regression coverage for connection boundaries, bulk ingestion and day rules."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from taskhero_anki.config import DayBoundary, Settings
from taskhero_anki.storage import StateStore

HAS_ANKI = importlib.util.find_spec("aqt") is not None
if HAS_ANKI:
    from aqt.qt import QApplication, QWidget
    from taskhero_anki.addon import AddonController


def timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


class ReviewAccountingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.now = timestamp("2026-10-02T12:00:00")
        self.day = "2026-10-02"

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, count, start=0, boundary=DayBoundary()):
        ids = [self.now + start + i for i in range(count)]
        self.store.record_reviews([(i, 1) for i in ids], "sync", 0, self.now, boundary)
        self.store.finalize_reviews(self.store.due_pending_reviews(self.now), set(ids))

    def test_bulk_ingestion_uses_one_connection_and_keeps_exclusions(self):
        ids = [self.now + i for i in range(1000)]
        self.store.exclude_reviews(ids[:10])
        with patch.object(self.store, "_connect", wraps=self.store._connect) as connect:
            self.assertEqual(self.store.record_reviews([(i, 1) for i in ids], "sync", 0, self.now, DayBoundary()), 990)
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.store.record_reviews([(i, 8) for i in ids], "sync", 0, self.now, DayBoundary()), 0)
        self.assertEqual(len(self.store.due_pending_reviews(self.now)), 990)

    def test_disconnect_does_not_carry_partial_reviews_or_daily_batches(self):
        settings = Settings(10, 3, 1, "old-habit", "Old")
        self.accept(29)
        self.store.materialize_events(self.day, settings, self.now)
        sent_ids = []
        while event := self.store.claim_next_event(self.now):
            self.store.mark_sent(event, {"trackerPoint": {"id": "synthetic"}}, self.now)
            sent_ids.append(event.event_id)
        self.assertEqual(len(sent_ids), 2)
        self.store.cancel_unsent(self.now)
        settings = Settings(10, 2, 1, "new-habit", "New")
        self.assertEqual(self.store.materialize_events(self.day, settings, self.now), [])
        self.assertEqual(self.store.reviewer_progress(self.day, settings).daily_batches, 0)
        self.assertEqual(self.store.reviewer_progress(self.day, settings).batch_reviews, 0)
        self.accept(1, start=100)
        self.assertEqual(self.store.materialize_events(self.day, settings, self.now), [])
        self.accept(9, start=101)
        self.store.materialize_events(self.day, settings, self.now)
        event = self.store.claim_next_event(self.now)
        self.assertEqual(event.kind, "tracker_point")
        self.assertNotIn(event.event_id, sent_ids)
        self.assertEqual(self.store.reviewer_progress(self.day, settings).daily_batches, 1)
        self.assertTrue(all((event_id, "sent") in self.store.event_statuses() for event_id in sent_ids))

    def test_replacement_retires_in_flight_retries_but_records_late_success(self):
        self.accept(10)
        self.store.materialize_events(self.day, Settings(10, 1, 1, "old", "Old"), self.now)
        while True:
            event = self.store.claim_next_event(self.now)
            self.assertIsNotNone(event)
            if event.kind == "habit_completion":
                break
            self.store.mark_sent(event, {"trackerPoint": {"id": "synthetic"}}, self.now)
        self.assertEqual(self.store.cancel_unsent_habit_events_for_other_habit("new", self.now), 1)
        self.store.reschedule(event.event_id, self.now, "timeout", self.now)
        self.store.mark_failed(event.event_id, "failure", self.now)
        self.assertIn((event.event_id, "cancelled"), self.store.event_statuses())
        self.store.mark_sent(event, {"task": {"id": "old"}}, self.now)
        self.assertIn((event.event_id, "sent"), self.store.event_statuses())
        self.assertEqual(self.store.materialize_events(self.day, Settings(10, 1, 1, "new", "New"), self.now), [])

    def test_habit_and_partial_batches_follow_taskhero_rollover_not_midnight(self):
        boundary = DayBoundary(0, 4)
        settings = Settings(10, 1, 1, "habit", "Study Anki", boundary)
        ids = []
        for value in ("2026-10-01T23:00:00", "2026-10-02T01:00:00", "2026-10-02T04:00:00"):
            start = timestamp(value)
            new_ids = list(range(start, start + 10))
            ids.extend(new_ids)
            self.store.record_reviews([(i, 1) for i in new_ids], "sync", 0, self.now, boundary)
            self.store.finalize_reviews(self.store.due_pending_reviews(self.now), set(ids))
            self.store.materialize_events(boundary.day(start), settings, self.now)
        events = []
        while event := self.store.claim_next_event(self.now):
            events.append(event)
            self.store.mark_sent(event, {}, self.now)
        self.assertEqual(sum(e.kind == "tracker_point" for e in events), 3)
        habits = [e for e in events if e.kind == "habit_completion"]
        self.assertEqual({e.review_day for e in habits}, {"2026-10-01", "2026-10-02"})
        self.assertEqual(len(habits), 2)
        self.assertEqual(self.store.review_count("2026-10-01"), 20)
        self.assertEqual(self.store.review_count("2026-10-02"), 10)
        for event in habits:
            instant = datetime.fromisoformat(event.payload["completedAt"].replace("Z", "+00:00"))
            self.assertEqual(boundary.day(int(instant.timestamp() * 1000)), event.review_day)

    def test_non_utc_fractional_timezone_boundary_matches_server_formula(self):
        boundary = DayBoundary(330, 4)
        self.assertEqual(boundary.day(timestamp("2026-10-01T22:29:59")), "2026-10-01")
        self.assertEqual(boundary.day(timestamp("2026-10-01T22:30:00")), "2026-10-02")
        self.assertIn("04:00 (UTC+05:30)", boundary.description())

    def test_afternoon_rollover_preserves_signed_server_day_semantics(self):
        boundary = DayBoundary(0, -11)
        self.assertEqual(boundary.day(timestamp("2026-10-01T12:59:59")), "2026-10-01")
        self.assertEqual(boundary.day(timestamp("2026-10-01T13:00:00")), "2026-10-02")
        self.assertIn("13:00 (UTC+00:00)", boundary.description())


@unittest.skipUnless(HAS_ANKI, "requires Anki's bundled Python/Qt")
class ReviewExclusionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["taskhero-anki-tests"])

    def test_sync_cannot_recredit_excluded_history_but_finds_older_mobile_reviews(self):
        self._check_sync_exclusions(existing_profile=False)

    def test_connected_development_profile_initializes_exclusions_on_upgrade(self):
        self._check_sync_exclusions(existing_profile=True)

    def _check_sync_exclusions(self, existing_profile):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as col:
            col.execute("CREATE TABLE revlog (id INTEGER PRIMARY KEY, usn INTEGER, ease INTEGER, cid INTEGER)")
            now = timestamp("2026-10-02T12:00:00")
            col.executemany("INSERT INTO revlog VALUES (?, -1, 3, 1)", [(now + i,) for i in range(10)])
            main = QWidget()
            main.pm = MagicMock()
            main.pm.profileFolder.return_value = directory
            main.col = MagicMock()
            main.col.db.scalar.side_effect = lambda sql, *args: col.execute(sql, args).fetchone()[0]
            main.col.db.all.side_effect = lambda sql, *args: col.execute(sql, args).fetchall()
            main.state = "deckBrowser"
            with patch("taskhero_anki.addon.mw", main), patch("taskhero_anki.addon._now_ms", return_value=now + 60_000):
                controller = AddonController()
                controller._start_worker = MagicMock()
                settings = Settings(habit_id="habit", habit_title="Study Anki")
                token = "th-int-v1-anki-" + "A" * 43
                if existing_profile:
                    controller.credential_store.save(token)
                    controller.settings_store.save(Settings(habit_id="habit", day_boundary=None))
                    controller.store.ensure_scan_baseline(now + 9, 0)
                    controller.shutdown()
                    controller = AddonController()
                    controller._start_worker = MagicMock()
                    self.assertTrue(controller.connected)
                    self.assertIsNone(controller.settings.day_boundary)
                controller.save_settings(token, settings)
                col.execute("UPDATE revlog SET usn = 1")
                controller.scan_new_revlog_entries()
                self.assertEqual(controller.store.event_statuses(), [])

                col.executemany("INSERT INTO revlog VALUES (?, 2, 1, 2)", [(now - 1000 + i,) for i in range(10)])
                controller.scan_new_revlog_entries()
                self.assertEqual(len(controller.store.event_statuses()), 1)
                self.assertEqual(controller.store.review_count("2026-10-02"), 10)

                controller.disconnect()
                col.executemany("INSERT INTO revlog VALUES (?, -1, 4, 3)", [(now + 1000 + i,) for i in range(10)])
                controller.save_settings("th-int-v1-anki-" + "B" * 43, settings)
                col.execute("UPDATE revlog SET usn = 3 WHERE usn = -1")
                controller.scan_new_revlog_entries()
                self.assertEqual(controller.store.review_count("2026-10-02"), 0)
                self.assertEqual(len(controller.store.event_statuses()), 1)
                controller.shutdown()
            main.close()
