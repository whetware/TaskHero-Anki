"""Opt-in contract test against the local TaskHero API, never production."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from taskhero_anki.api import ApiError, TaskHeroClient
from taskhero_anki.config import DayBoundary, Settings
from taskhero_anki.engine import process_next_event
from taskhero_anki.storage import StateStore


class _RecordingClient(TaskHeroClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.point_responses = []
        self.habit_responses = []

    def create_tracker_points(self, *args, **kwargs):
        response = super().create_tracker_points(*args, **kwargs)
        self.point_responses.append(response)
        return response

    def complete_habit(self, *args, **kwargs):
        response = super().complete_habit(*args, **kwargs)
        self.habit_responses.append(response)
        return response


class _LoseFirstRewardResponses(_RecordingClient):
    first_reward_id = None
    first_habit_response = None

    def create_tracker_points(self, *args, **kwargs):
        response = super().create_tracker_points(*args, **kwargs)
        if self.first_reward_id is None:
            self.first_reward_id = response["trackerPoint"]["id"]
            raise ApiError(None, "network_error", "Simulated lost response after server commit")
        return response

    def complete_habit(self, *args, **kwargs):
        response = super().complete_habit(*args, **kwargs)
        if self.first_habit_response is None:
            self.first_habit_response = response
            raise ApiError(None, "network_error", "Simulated lost habit response after server commit")
        return response


@unittest.skipUnless(os.environ.get("TASKHERO_ANKI_LOCAL_API_KEY"), "requires an isolated local API test user")
class LocalApiTest(unittest.TestCase):
    def test_durable_queue_attribution_and_lost_response_retry(self):
        client = _LoseFirstRewardResponses(
            os.environ["TASKHERO_ANKI_LOCAL_API_KEY"], "http://127.0.0.1:3000/api/v1"
        )
        boundary = DayBoundary.from_api(client.me()["dayBoundary"])
        habit = client.create_daily_habit("Anki local contract test")
        try:
            self.assertEqual(habit["difficulty"], "normal")
            self.assertEqual(habit["repsPerDayGoal"], 1)
            self.assertEqual(habit["repsCompletedToday"], 0)
            self.assertEqual(habit["status"], "open")
            with tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / "state.sqlite3")
                now_ms = int(time.time() * 1000)
                review_ids = {now_ms - 200 + offset for offset in range(10)}
                for review_id in review_ids:
                    store.record_review(review_id, -1, "desktop", review_id, review_id, boundary)
                store.finalize_reviews(store.due_pending_reviews(now_ms), review_ids)
                settings = Settings(10, 1, 1, habit["id"], habit["title"], boundary)
                day = boundary.day(now_ms)
                self.assertEqual(len(store.materialize_events(day, settings, now_ms)), 2)
                outcomes = [process_next_event(store, client, now_ms + step * 10000).outcome for step in range(5)]
                self.assertEqual(sorted(outcomes), ["idle", "queued", "queued", "sent", "sent"])
                self.assertEqual(store.queue_stats().queued, 0)
                self.assertEqual(store.queue_stats().failed, 0)
                self.assertEqual(store.materialize_events(day, settings, now_ms + 50000), [])
                with closing(sqlite3.connect(store.path)) as connection:
                    remote_ids = dict(connection.execute("SELECT kind, remote_id FROM sent_events"))
                point = client.point_responses[-1]["trackerPoint"]
                self.assertEqual(point["id"], client.first_reward_id)
                self.assertEqual(remote_ids["tracker_point"], point["id"])
                self.assertEqual(remote_ids["habit_completion"], habit["id"])
                self.assertEqual(point["sourceName"], "Anki")
                self.assertEqual(point["description"], "Completed 10 reviews")
                self.assertEqual(client.habit_responses[-1]["task"]["status"], "completed")
                self.assertEqual(client.habit_responses[-1]["task"]["repsCompletedToday"], 1)
                self.assertEqual(client.habit_responses[-1], client.first_habit_response)
                persisted_habit = next(task for task in client.list_daily_habits() if task["id"] == habit["id"])
                self.assertEqual(persisted_habit["status"], "completed")
                self.assertEqual(persisted_habit["repsCompletedToday"], 1)
                self.assertEqual(persisted_habit["difficulty"], "normal")

                # More reviews can earn another batch, but not a second daily habit rep.
                later_review_ids = {now_ms - 100 + offset for offset in range(10)}
                for review_id in later_review_ids:
                    store.record_review(review_id, -1, "desktop", review_id, review_id, boundary)
                store.finalize_reviews(store.due_pending_reviews(now_ms), later_review_ids)
                self.assertEqual(len(store.materialize_events(day, settings, now_ms + 60000)), 1)
                self.assertEqual(process_next_event(store, client, now_ms + 60001).outcome, "sent")
                persisted_habit = next(task for task in client.list_daily_habits() if task["id"] == habit["id"])
                self.assertEqual(persisted_habit["repsCompletedToday"], 1)
        finally:
            # The limited Anki token intentionally cannot delete tasks. This test
            # requires a disposable local user, which the invoking harness removes.
            with self.assertRaises(ApiError) as denied:
                client._request("DELETE", f"/tasks/{habit['id']}")
            self.assertEqual((denied.exception.status, denied.exception.code), (403, "insufficient_scope"))

    def test_delayed_batches_and_habit_completions_share_the_server_rollover_day(self):
        client = _RecordingClient(os.environ["TASKHERO_ANKI_LOCAL_API_KEY"], "http://127.0.0.1:3000/api/v1")
        boundary = DayBoundary.from_api(client.me()["dayBoundary"])
        habit = client.create_daily_habit("Anki day-boundary contract test")
        settings = Settings(10, 1, 1, habit["id"], habit["title"], boundary)
        # Three delayed batches across two native TaskHero days. The first two
        # must not be split by the computer's midnight or by request delivery time.
        day_start = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp() * 1000)
        day_start += int(boundary.rollover_offset_hours * 3_600_000 - boundary.utc_offset_minutes * 60_000)
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            for start in (day_start - 7_200_000, day_start - 3_600_000, day_start + 3_600_000):
                ids = set(range(start, start + 10))
                store.record_reviews([(i, 1) for i in ids], "sync", 0, now, boundary)
                store.finalize_reviews(store.due_pending_reviews(now), ids)
                store.materialize_events(boundary.day(start), settings, now)
            outcomes = [process_next_event(store, client, now).outcome for _ in range(6)]
            self.assertEqual(sorted(outcomes), ["idle", "sent", "sent", "sent", "sent", "sent"])
            completions = client.habit_responses
            self.assertEqual(len(completions), 2)
            self.assertTrue(all(not response["awarded"]["alreadyCompleted"] for response in completions))
