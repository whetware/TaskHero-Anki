from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from taskhero_anki.api import ApiError
from taskhero_anki.config import Settings
from taskhero_anki.engine import process_next_event
from taskhero_anki.storage import ReviewerProgress, StateStore
from taskhero_anki.config import DayBoundary


def _timestamp_ms(day: int, minute: int = 0) -> int:
    return int(datetime(2026, 9, day, 12, minute).timestamp() * 1000)


class _Client:
    def __init__(self, error: ApiError | None = None, habit_error: ApiError | None = None) -> None:
        self.error = error
        self.habit_error = habit_error
        self.calls = []

    def create_tracker_points(self, value, description, source_id, source_name, idempotency_key):
        self.calls.append(("points", value, description, source_id, source_name, idempotency_key))
        if self.error:
            raise self.error
        return {"trackerPoint": {"id": "remote-point", "value": value, "sourceId": source_id}}

    def complete_habit(self, habit_id, completed_at, source_name, idempotency_key):
        self.calls.append(("habit", habit_id, completed_at, source_name, idempotency_key))
        if self.habit_error or self.error:
            raise self.habit_error or self.error
        return {"task": {"id": habit_id}, "awarded": {"alreadyCompleted": False}}


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _accepted_reviews(self, count: int, day: int = 1) -> str:
        review_day = DayBoundary().day(_timestamp_ms(day))
        ids = set()
        for index in range(count):
            ids.add(_timestamp_ms(day, index))
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO review_events (
                    revlog_id, reviewed_at_ms, review_day, usn, origin, state, stable_after_ms, recorded_at_ms
                ) VALUES (?, ?, ?, -1, 'desktop', 'pending', ?, ?)
                """,
                [(revlog_id, revlog_id, review_day, revlog_id, revlog_id) for revlog_id in ids],
            )
        due = self.store.due_pending_reviews(2**63 - 1)
        self.store.finalize_reviews(due, ids)
        return review_day

    def test_partial_batches_do_not_cross_calendar_days(self) -> None:
        first = self._accepted_reviews(9, day=1)
        second = self._accepted_reviews(1, day=2)
        settings = Settings(habit_id="habit", habit_title="Study Anki")
        self.assertEqual(self.store.materialize_events(first, settings, _timestamp_ms(2)), [])
        self.assertEqual(self.store.materialize_events(second, settings, _timestamp_ms(2)), [])
        self.assertEqual(self.store.reviewer_progress(first, settings).batch_reviews, 9)
        self.assertEqual(self.store.reviewer_progress(second, settings).batch_reviews, 1)

    def test_session_activity_ignores_retired_history_but_detects_progress(self) -> None:
        self.assertFalse(self.store.has_reward_session_activity())
        self.store.exclude_reviews([1])
        self.assertFalse(self.store.has_reward_session_activity())
        day = self._accepted_reviews(10)
        self.assertTrue(self.store.has_reward_session_activity())
        settings = Settings(habit_id="habit")
        self.store.materialize_events(day, settings, _timestamp_ms(1, 20))
        event = self.store.claim_next_event(_timestamp_ms(1, 20))
        self.store.mark_sent(event, {"trackerPoint": {"id": "receipt"}}, _timestamp_ms(1, 20))
        self.store.cancel_unsent(_timestamp_ms(1, 21))
        self.assertFalse(self.store.has_reward_session_activity())
        self.assertIn((event.event_id, "sent"), self.store.event_statuses())
        self.store.materialize_events(day, settings, _timestamp_ms(1, 22))
        self.assertTrue(self.store.has_reward_session_activity())

    @unittest.skipUnless(hasattr(time, "tzset"), "requires process timezone support")
    def test_day_rules_are_independent_of_desktop_timezone_and_dst(self) -> None:
        try:
            with patch.dict(os.environ, {"TZ": "America/Toronto"}):
                time.tzset()
                cases = [
                    ("2026-09-02T03:59:59", "2026-09-01"),
                    ("2026-09-02T04:00:00", "2026-09-02"),
                    ("2026-03-08T06:59:59", "2026-03-08"),
                    ("2026-03-08T07:00:00", "2026-03-08"),
                    ("2026-11-01T05:30:00", "2026-11-01"),
                    ("2026-11-01T06:30:00", "2026-11-01"),
                ]
                for timestamp, expected in cases:
                    with self.subTest(timestamp=timestamp):
                        milliseconds = int(datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc).timestamp() * 1000)
                        self.assertEqual(DayBoundary(-240, 0).day(milliseconds), expected)
        finally:
            time.tzset()

    def test_materializes_all_batches_and_one_habit_deterministically(self) -> None:
        review_day = self._accepted_reviews(30)
        settings = Settings(
            review_batch_size=10,
            daily_batch_goal=2,
            reward_points=2,
            habit_id="habit-1",
            habit_title="Study Anki",
        )
        created = self.store.materialize_events(review_day, settings, _timestamp_ms(1, 40))
        self.assertEqual(len(created), 4)
        self.assertEqual(self.store.materialize_events(review_day, settings, _timestamp_ms(1, 41)), [])
        self.assertTrue(all(event_id.startswith("anki-") for event_id in created))
        self.assertTrue(all("habit-1" not in event_id for event_id in created))

        client = _Client()
        results = [process_next_event(self.store, client, _timestamp_ms(1, 50) + index) for index in range(4)]
        self.assertEqual([result.outcome for result in results], ["sent"] * 4)
        self.assertEqual(sorted(status for _, status in self.store.event_statuses()), ["sent"] * 4)
        self.assertEqual(sum(1 for call in client.calls if call[0] == "points"), 3)
        self.assertEqual(sum(1 for call in client.calls if call[0] == "habit"), 1)
        for call in client.calls:
            if call[0] == "points":
                self.assertEqual(call[2], "Completed 10 reviews")
                self.assertEqual(call[4], "Anki")
                self.assertEqual(call[3], call[5])
            else:
                self.assertEqual(call[3], "Anki")

    def test_reviewer_progress_tracks_pending_and_completed_batches(self) -> None:
        review_day = self._accepted_reviews(9)
        settings = Settings(10, 30, 2, "habit-1", "Study Anki")
        self.assertEqual(
            self.store.reviewer_progress(review_day, settings),
            ReviewerProgress(9, 10, 0),
        )

        pending_id = _timestamp_ms(1, 9)
        self.store.record_review(pending_id, -1, "desktop", pending_id + 10_000, pending_id)
        self.assertEqual(
            self.store.reviewer_progress(review_day, settings),
            ReviewerProgress(10, 10, 0),
        )

        self.store.finalize_reviews(self.store.due_pending_reviews(2**63 - 1), {pending_id})
        self.store.materialize_events(review_day, settings, _timestamp_ms(1, 20))
        self.assertEqual(
            self.store.reviewer_progress(review_day, settings),
            ReviewerProgress(0, 10, 1),
        )

    def test_upgrades_old_unsent_payloads_without_changing_identity_or_sent_history(self) -> None:
        day = self._accepted_reviews(20)
        settings = Settings(daily_batch_goal=2, habit_id="habit-1", habit_title="Study Anki")
        self.assertEqual(len(self.store.materialize_events(day, settings, _timestamp_ms(1, 30))), 3)
        events = [self.store.claim_next_event(_timestamp_ms(1, 31)) for _ in range(3)]
        self.assertCountEqual([event.kind for event in events], ["tracker_point", "tracker_point", "habit_completion"])
        sent_event = next(event for event in events if event.kind == "tracker_point")
        for event in events:
            if event == sent_event:
                self.store.mark_sent(event, {"trackerPoint": {"id": "sent-point"}}, _timestamp_ms(1, 32))
            elif event.kind == "habit_completion":
                self.store.mark_failed(event.event_id, "retry after reconnect", _timestamp_ms(1, 32))
            else:
                self.store.reschedule(event.event_id, _timestamp_ms(1, 35), "retry later", _timestamp_ms(1, 32))
        original_statuses = self.store.event_statuses()
        legacy_payloads = {}
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            for event_id, payload_json in connection.execute("SELECT event_id, payload_json FROM outbound_events").fetchall():
                payload = json.loads(payload_json)
                payload.pop("sourceName", None)
                payload.pop("idempotencyKey", None)
                payload.pop("onlyIfIncomplete", None)
                if "description" in payload:
                    payload["description"] = "Anki review batch"
                legacy_payloads[event_id] = payload
                connection.execute("UPDATE outbound_events SET payload_json = ? WHERE event_id = ?", (json.dumps(payload), event_id))
            sent_receipts = connection.execute("SELECT * FROM sent_events").fetchall()

        StateStore(self.store.path)
        self.assertEqual(self.store.event_statuses(), original_statuses)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM sent_events").fetchall(), sent_receipts)
            for event_id, kind, status, payload_json in connection.execute("SELECT event_id, kind, status, payload_json FROM outbound_events"):
                payload = json.loads(payload_json)
                if status == "sent":
                    self.assertEqual(payload, legacy_payloads[event_id])
                else:
                    expected = {**legacy_payloads[event_id], "sourceName": "Anki", "idempotencyKey": event_id}
                    if kind == "habit_completion":
                        expected["onlyIfIncomplete"] = True
                    self.assertEqual(payload, expected)

    def test_reward_amounts_and_retry_payloads_survive_settings_changes(self) -> None:
        for points in (1, 2, 3):
            with self.subTest(points=points):
                day = self._accepted_reviews(10, day=points)
                now = _timestamp_ms(points, 20)
                settings = Settings(reward_points=points)
                self.assertEqual(len(self.store.materialize_events(day, settings, now)), 1)
                unavailable = _Client(ApiError(503, "internal_error", "Try later"))
                self.assertEqual(process_next_event(self.store, unavailable, now).outcome, "queued")

                changed_settings = Settings(review_batch_size=20, reward_points=1 if points != 1 else 2)
                self.assertEqual(self.store.materialize_events(day, changed_settings, now + 1000), [])
                client = _Client()
                self.assertEqual(process_next_event(self.store, client, now + 300_000).outcome, "sent")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls, unavailable.calls)
                call = client.calls[0]
                self.assertEqual(call[:3], ("points", points, "Completed 10 reviews"))
                self.assertEqual(call[4], "Anki")
                self.assertEqual(call[3], call[5])

    def test_habit_completes_after_daily_batch_goal_once(self) -> None:
        review_day = self._accepted_reviews(10)
        settings = Settings(10, 2, 1, "habit-1", "Study Anki")
        client = _Client()
        self.assertEqual(len(self.store.materialize_events(review_day, settings, _timestamp_ms(1, 10))), 1)
        process_next_event(self.store, client, _timestamp_ms(1, 11))
        self.assertEqual([call[0] for call in client.calls], ["points"])

        self._accepted_reviews(15)
        self.assertEqual(self.store.materialize_events(review_day, settings, _timestamp_ms(1, 12)), [])
        self.assertEqual([call[0] for call in client.calls], ["points"])

        self._accepted_reviews(20)
        self.assertEqual(len(self.store.materialize_events(review_day, settings, _timestamp_ms(1, 14))), 2)
        while process_next_event(self.store, client, _timestamp_ms(1, 15)).outcome != "idle":
            pass
        self.assertEqual(sum(call[0] == "points" for call in client.calls), 2)
        self.assertEqual(sum(call[0] == "habit" for call in client.calls), 1)

        self._accepted_reviews(30)
        self.assertEqual(len(self.store.materialize_events(review_day, settings, _timestamp_ms(1, 16))), 1)
        while process_next_event(self.store, client, _timestamp_ms(1, 17)).outcome != "idle":
            pass
        self.assertEqual(sum(call[0] == "points" for call in client.calls), 3)
        self.assertEqual(sum(call[0] == "habit" for call in client.calls), 1)

    def test_batch_size_changes_apply_to_new_reviews_not_already_credited_reviews(self) -> None:
        day = self._accepted_reviews(20)
        self.store.materialize_events(day, Settings(20, 99, 1, "habit", "Study Anki"), _timestamp_ms(1, 40))
        self.assertEqual(self.store.materialize_events(day, Settings(10, 99, 1, "habit", "Study Anki"), _timestamp_ms(1, 41)), [])
        self._accepted_reviews(30)
        self.assertEqual(len(self.store.materialize_events(day, Settings(10, 99, 1, "habit", "Study Anki"), _timestamp_ms(1, 50))), 1)

    def test_batch_size_change_preserves_completed_daily_batch_progress(self) -> None:
        day = self._accepted_reviews(10)
        first_settings = Settings(10, 2, 1, "habit", "Study Anki")
        self.assertEqual(len(self.store.materialize_events(day, first_settings, _timestamp_ms(1, 20))), 1)

        second_settings = Settings(20, 2, 1, "habit", "Study Anki")
        self.assertEqual(self.store.materialize_events(day, second_settings, _timestamp_ms(1, 21)), [])
        self._accepted_reviews(30)
        self.assertEqual(len(self.store.materialize_events(day, second_settings, _timestamp_ms(1, 40))), 2)
        self.assertEqual(self.store.reviewer_progress(day, second_settings).daily_batches, 2)

    def test_disconnect_cancellation_cannot_be_undone_by_a_late_failure(self) -> None:
        day = self._accepted_reviews(10)
        self.store.materialize_events(day, Settings(10, 99, 1, "habit", "Study Anki"), _timestamp_ms(1, 20))
        event = self.store.claim_next_event(_timestamp_ms(1, 21))
        self.store.cancel_unsent(_timestamp_ms(1, 22))
        self.store.reschedule(event.event_id, _timestamp_ms(1, 25), "late timeout", _timestamp_ms(1, 23))
        self.store.mark_failed(event.event_id, "late auth error", _timestamp_ms(1, 24))
        self.assertEqual(self.store.event_statuses(), [(event.event_id, "cancelled")])
        self.assertFalse(self.store.has_due_event(_timestamp_ms(1, 30)))

    def test_switching_habits_does_not_complete_another_habit_for_the_same_day(self) -> None:
        day = self._accepted_reviews(10)
        self.assertEqual(
            len(self.store.materialize_events(day, Settings(10, 1, 1, "habit-a", "A"), _timestamp_ms(1, 20))),
            2,
        )
        self.assertEqual(
            self.store.materialize_events(day, Settings(10, 1, 1, "habit-b", "B"), _timestamp_ms(1, 21)),
            [],
        )

    def test_replacing_habit_retires_old_unsent_completion_without_same_day_replay(self) -> None:
        day = self._accepted_reviews(10)
        self.store.materialize_events(day, Settings(10, 1, 1, "deleted-habit", "Old"), _timestamp_ms(1, 20))
        client = _Client(habit_error=ApiError(404, "not_found", "Task not found."))
        results = [process_next_event(self.store, client, _timestamp_ms(1, 21)) for _ in range(2)]
        self.assertEqual(sorted(result.outcome for result in results), ["failed", "sent"])
        result = next(result for result in results if result.outcome == "failed")
        self.assertEqual(result.outcome, "failed")
        self.assertIn("select or create", result.message)
        self.assertEqual(self.store.queue_stats().failed, 1)

        self.assertEqual(self.store.cancel_unsent_habit_events_for_other_habit("new-habit", _timestamp_ms(1, 23)), 1)
        self.assertEqual(self.store.queue_stats().failed, 0)
        self.assertEqual(self.store.retry_failed(_timestamp_ms(1, 24)), 0)
        self.assertEqual(
            self.store.materialize_events(day, Settings(10, 1, 1, "new-habit", "New"), _timestamp_ms(1, 25)),
            [],
        )
        self.assertEqual(sorted(status for _, status in self.store.event_statuses()), ["cancelled", "sent"])

        next_day = self._accepted_reviews(10, day=2)
        self.store.materialize_events(next_day, Settings(10, 1, 1, "new-habit", "New"), _timestamp_ms(2, 20))
        client = _Client()
        while process_next_event(self.store, client, _timestamp_ms(2, 21)).outcome != "idle":
            pass
        self.assertEqual([call[1] for call in client.calls if call[0] == "habit"], ["new-habit"])

    def test_replacing_habit_cancels_pending_old_completion_not_batch_points(self) -> None:
        day = self._accepted_reviews(10)
        self.store.materialize_events(day, Settings(10, 1, 1, "old-habit", "Old"), _timestamp_ms(1, 20))
        self.assertEqual(self.store.cancel_unsent_habit_events_for_other_habit("new-habit", _timestamp_ms(1, 21)), 1)
        self.assertEqual(sorted(status for _, status in self.store.event_statuses()), ["cancelled", "pending"])
        client = _Client()
        self.assertEqual(process_next_event(self.store, client, _timestamp_ms(1, 22)).outcome, "sent")
        self.assertEqual([call[0] for call in client.calls], ["points"])

    def test_undo_removed_review_is_discarded_before_reward(self) -> None:
        revlog_id = _timestamp_ms(1)
        review_day = DayBoundary().day(revlog_id)
        self.store.record_review(revlog_id, -1, "desktop", revlog_id, revlog_id)
        due = self.store.due_pending_reviews(2**63 - 1)
        self.store.finalize_reviews(due, set())
        self.assertEqual(self.store.review_count(review_day), 0)
        settings = Settings(10, 1, 1, "habit-1", "Study Anki")
        self.assertEqual(self.store.materialize_events(review_day, settings, revlog_id), [])

    def test_undo_reconciliation_does_not_accept_other_reviews_early(self) -> None:
        first, second = _timestamp_ms(1), _timestamp_ms(1, 1)
        review_day = DayBoundary().day(first)
        self.store.record_review(first, -1, "desktop", first + 10_000, first)
        self.store.record_review(second, -1, "desktop", second + 10_000, second)
        pending = self.store.due_pending_reviews(2**63 - 1)
        affected = self.store.finalize_reviews(pending, {second}, accept_existing=False)
        self.assertEqual(affected, {review_day})
        self.assertEqual(self.store.review_count(review_day), 0)
        self.assertEqual(self.store.reviewer_progress(review_day, Settings()).batch_reviews, 1)

    def test_retry_and_permanent_failure_states(self) -> None:
        review_day = self._accepted_reviews(10)
        settings = Settings(10, 99, 1, "habit-1", "Study Anki")
        self.store.materialize_events(review_day, settings, _timestamp_ms(1, 20))

        transient = _Client(ApiError(503, "internal_error", "Try later"))
        result = process_next_event(self.store, transient, _timestamp_ms(1, 21))
        self.assertEqual(result.outcome, "queued")
        self.assertEqual(self.store.queue_stats().queued, 1)

        permanent = _Client(ApiError(401, "invalid_api_key", "Invalid key"))
        result = process_next_event(self.store, permanent, _timestamp_ms(1, 30))
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(self.store.queue_stats().failed, 1)

        self.assertEqual(self.store.retry_failed(_timestamp_ms(1, 31)), 1)
        recovered = _Client()
        self.assertEqual(process_next_event(self.store, recovered, _timestamp_ms(1, 32)).outcome, "sent")
        self.assertEqual(recovered.calls, permanent.calls)
        self.assertEqual(recovered.calls, transient.calls)
        self.assertEqual(self.store.queue_stats().failed, 0)
        self.assertEqual(process_next_event(self.store, recovered, _timestamp_ms(1, 33)).outcome, "idle")

    def test_reopen_resends_in_flight_event_with_same_payload_and_id(self) -> None:
        day = self._accepted_reviews(10)
        self.store.materialize_events(day, Settings(reward_points=3), _timestamp_ms(1, 20))
        client = _Client()
        # The server accepted this request, but Anki stopped before saving its receipt.
        with patch.object(self.store, "mark_sent", side_effect=OSError("interrupted receipt write")):
            with self.assertRaisesRegex(OSError, "interrupted receipt"):
                process_next_event(self.store, client, _timestamp_ms(1, 21))
        before = self.store.event_statuses()
        self.assertEqual([status for _, status in before], ["in_flight"])
        reopened = StateStore(self.store.path)
        self.assertEqual(reopened.event_statuses(), [(before[0][0], "pending")])
        self.assertEqual(process_next_event(reopened, client, _timestamp_ms(1, 22)).outcome, "sent")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], client.calls[1])
        self.assertEqual(reopened.event_statuses(), [(before[0][0], "sent")])
        self.assertEqual(process_next_event(reopened, client, _timestamp_ms(1, 23)).outcome, "idle")

    def test_upgrade_removes_response_bodies_and_caps_without_losing_history(self) -> None:
        day = self._accepted_reviews(25)
        settings = Settings(daily_batch_goal=2, habit_id="habit", habit_title="Study Anki")
        self.store.ensure_scan_baseline(12345, 42)
        profile_id = self.store.profile_id()
        self.assertEqual(len(self.store.materialize_events(day, settings, _timestamp_ms(1, 30))), 3)
        event = self.store.claim_next_event(_timestamp_ms(1, 31))
        self.store.mark_sent(event, {"trackerPoint": {"id": "remote-point"}}, _timestamp_ms(1, 32))
        statuses = self.store.event_statuses()
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            receipts = connection.execute("SELECT * FROM sent_events").fetchall()
            connection.execute("ALTER TABLE sent_events ADD COLUMN response_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("UPDATE sent_events SET response_json = ?", (json.dumps({"personal": "old response body"}),))
            connection.execute("ALTER TABLE reward_day_progress ADD COLUMN daily_points_cap INTEGER NOT NULL DEFAULT 1")

        reopened = StateStore(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertNotIn("response_json", [row[1] for row in connection.execute("PRAGMA table_info(sent_events)")])
            self.assertNotIn("daily_points_cap", [row[1] for row in connection.execute("PRAGMA table_info(reward_day_progress)")])
            self.assertEqual(connection.execute("SELECT * FROM sent_events").fetchall(), receipts)
        self.assertEqual(reopened.event_statuses(), statuses)
        self.assertEqual(reopened.profile_id(), profile_id)
        self.assertEqual(reopened.scan_cursors(), (12345, 42))
        self.assertEqual(reopened.reviewer_progress(day, settings), ReviewerProgress(5, 10, 2))
        self.assertEqual(reopened.materialize_events(day, settings, _timestamp_ms(1, 33)), [])
        self._accepted_reviews(30)
        self.assertEqual(len(reopened.materialize_events(day, settings, _timestamp_ms(1, 34))), 1)

    def test_sent_receipts_do_not_retain_response_bodies(self) -> None:
        day = self._accepted_reviews(10)
        self.store.materialize_events(day, Settings(), _timestamp_ms(1, 20))
        event = self.store.claim_next_event(_timestamp_ms(1, 21))
        self.store.mark_sent(event, {"trackerPoint": {"id": "remote-point"}, "personal": "synthetic private response"}, _timestamp_ms(1, 22))
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM sent_events").fetchall(), [
                (event.event_id, event.kind, "remote-point", _timestamp_ms(1, 22)),
            ])
            self.assertNotIn("synthetic private response", "\n".join(connection.iterdump()))

    def test_scan_cursors_only_advance(self) -> None:
        self.store.ensure_scan_baseline(100, 5)
        self.store.advance_scan_cursors(90, 4)
        self.assertEqual(self.store.scan_cursors(), (100, 5))
        self.store.advance_scan_cursors(110, 8)
        self.assertEqual(self.store.scan_cursors(), (110, 8))

    def test_synced_reviews_can_receive_delayed_credit_immediately(self) -> None:
        review_ids = {_timestamp_ms(2, minute) for minute in range(10)}
        review_day = DayBoundary().day(min(review_ids))
        for revlog_id in review_ids:
            self.store.record_review(revlog_id, 42, "sync", revlog_id, revlog_id)
        due = self.store.due_pending_reviews(max(review_ids))
        self.assertEqual({review.revlog_id for review in due}, review_ids)
        self.store.finalize_reviews(due, review_ids)

        settings = Settings(10, 1, 1, "habit-1", "Study Anki")
        self.assertEqual(len(self.store.materialize_events(review_day, settings, max(review_ids))), 2)

    def test_event_ids_are_isolated_by_profile(self) -> None:
        first_day = self._accepted_reviews(10)
        settings = Settings(10, 99, 1, "habit-1", "Study Anki")
        first_ids = self.store.materialize_events(first_day, settings, _timestamp_ms(1, 20))

        with tempfile.TemporaryDirectory() as directory:
            other_store = StateStore(Path(directory) / "state.sqlite3")
            review_ids = {_timestamp_ms(1, minute) for minute in range(10)}
            for revlog_id in review_ids:
                other_store.record_review(revlog_id, -1, "desktop", revlog_id, revlog_id)
            other_store.finalize_reviews(other_store.due_pending_reviews(max(review_ids)), review_ids)
            other_ids = other_store.materialize_events(first_day, settings, _timestamp_ms(1, 20))

        self.assertEqual(len(first_ids), 1)
        self.assertEqual(len(other_ids), 1)
        self.assertNotEqual(first_ids, other_ids)

    @unittest.skipUnless(os.name == "posix", "requires POSIX permission bits")
    def test_database_file_permissions_are_restricted(self) -> None:
        self.store.path.parent.chmod(0o755)
        self.store.path.chmod(0o644)
        StateStore(self.store.path)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_database_connections_are_explicitly_closed(self) -> None:
        opened = []
        original_connect = self.store._connect

        def tracked_connect():
            connection = original_connect()
            opened.append(connection)
            return connection

        self.store._connect = tracked_connect
        for _ in range(20):
            self.store.queue_stats()

        self.assertEqual(len(opened), 20)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
