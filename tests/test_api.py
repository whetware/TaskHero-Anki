from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from unittest.mock import patch
from email.message import Message
from urllib.request import Request

from taskhero_anki.api import ADDON_VERSION, ApiError, TaskHeroClient, _RejectRedirects


TEST_TOKEN = f"th-int-v1-anki-{'A' * 43}"


class _Handler(BaseHTTPRequestHandler):
    requests: List[Dict[str, Any]] = []

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "body": body,
            }
        )

        if self.path == "/api/v1/me":
            self._json(200, {"userId": "user-1", "dayBoundary": {"utcOffsetMinutes": -300, "rolloverOffsetHours": 4}})
        elif self.path.startswith("/api/v1/redirect/"):
            self._json(int(self.path.rsplit("/", 1)[1]), {}, {
                "Location": f"http://localhost:{self.server.server_port}/must-not-receive-token",
            })
        elif self.path.startswith("/api/v1/tasks?status=all"):
            self._json(
                200,
                {
                    "nextCursor": None,
                    "tasks": [
                        {
                            "id": "daily",
                            "type": "habit",
                            "title": "Study Anki",
                            "status": "open",
                            "repsPerDayGoal": 1,
                            "schedule": {"mode": "weekly", "daysOfWeek": list(range(7))},
                        },
                        {
                            "id": "twice",
                            "type": "habit",
                            "title": "Twice",
                            "status": "open",
                            "repsPerDayGoal": 2,
                            "schedule": {"mode": "weekly", "daysOfWeek": list(range(7))},
                        },
                        {
                            "id": "completed-daily",
                            "type": "habit",
                            "title": "Already studied",
                            "status": "completed",
                            "repsPerDayGoal": 1,
                            "schedule": {"mode": "weekly", "daysOfWeek": list(range(7))},
                        },
                    ]
                },
            )
        elif self.path == "/api/v1/tasks":
            self._json(201, {"task": {"id": "created-habit", "title": body["title"]}})
        elif self.path == "/api/v1/tracker-points":
            self._json(201, {"trackerPoint": {"id": "point-1", **body}})
        elif self.path == "/api/v1/tasks/habit%2Fwith%20space/complete":
            self._json(200, {"task": {"id": "habit/with space"}, "awarded": {"alreadyCompleted": False}})
        elif self.path == "/api/v1/rate-limited":
            self._json(429, {"error": {"code": "rate_limited", "message": "Slow down", "details": {}}}, {"Retry-After": "7"})
        else:
            self._json(404, {"error": {"code": "not_found", "message": "Missing", "details": {}}})

    def _json(self, status: int, value: Dict[str, Any], headers: Dict[str, str] | None = None) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


class TaskHeroClientTest(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = TaskHeroClient(TEST_TOKEN, f"http://127.0.0.1:{self.server.server_port}/api/v1")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_connection_and_habit_selection(self) -> None:
        self.assertEqual(self.client.me(), {"userId": "user-1", "dayBoundary": {"utcOffsetMinutes": -300, "rolloverOffsetHours": 4}})
        habits = self.client.list_daily_habits()
        self.assertEqual([habit["id"] for habit in habits], ["daily", "completed-daily"])
        self.assertTrue(all(request["authorization"] == f"Bearer {TEST_TOKEN}" for request in _Handler.requests))
        self.assertTrue(all(request["user_agent"] == f"TaskHero-Anki/{ADDON_VERSION}" for request in _Handler.requests))

    def test_mutation_payloads_and_encoded_habit_id(self) -> None:
        habit = self.client.create_daily_habit()
        self.assertEqual(habit["id"], "created-habit")
        point = self.client.create_tracker_points(2, "Completed 10 reviews", "anki-event", "Anki", "anki-event")
        self.assertEqual(point["trackerPoint"]["sourceId"], "anki-event")
        self.assertEqual(point["trackerPoint"]["sourceName"], "Anki")
        self.assertEqual(point["trackerPoint"]["idempotencyKey"], "anki-event")
        point_request = _Handler.requests[-1]
        self.assertEqual(set(point_request["body"]), {"value", "description", "sourceId", "sourceName", "idempotencyKey"})
        self.client.complete_habit("habit/with space", "2026-09-01T12:00:00Z", "Anki", "habit-event")

        create_request = next(request for request in _Handler.requests if request["path"] == "/api/v1/tasks")
        self.assertEqual(create_request["body"]["schedule"]["daysOfWeek"], list(range(7)))
        self.assertEqual(create_request["body"]["repsPerDayGoal"], 1)
        self.assertEqual(create_request["body"]["difficulty"], "normal")
        complete_request = _Handler.requests[-1]
        self.assertEqual(complete_request["path"], "/api/v1/tasks/habit%2Fwith%20space/complete")
        self.assertEqual(complete_request["body"], {"completedAt": "2026-09-01T12:00:00Z", "sourceName": "Anki",
                                                  "idempotencyKey": "habit-event", "onlyIfIncomplete": True})

    def test_structured_error(self) -> None:
        with self.assertRaises(ApiError) as raised:
            self.client._request("GET", "/rate-limited")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after_seconds, 7)
        self.assertEqual(raised.exception.code, "rate_limited")

    def test_rejects_full_api_keys_and_malformed_integration_tokens(self) -> None:
        for credential in ("", "th-api-full-account-key", "th-int-v1-linear-" + "A" * 43,
                           "th-int-v1-anki-" + "A" * 42):
            with self.subTest(credential=credential), self.assertRaisesRegex(ValueError, "valid TaskHero Anki"):
                TaskHeroClient(credential)

    def test_pagination_includes_old_habits_and_deduplicates(self) -> None:
        habit = {"id": "old", "type": "habit", "status": "completed", "repsPerDayGoal": 1,
                 "schedule": {"mode": "weekly", "daysOfWeek": list(range(7))}}
        with patch.object(self.client, "_request", side_effect=[
            {"tasks": [dict(habit, id="new")], "nextCursor": "page:2"},
            {"tasks": [], "nextCursor": "page:3"},
            {"tasks": [habit, dict(habit, id="new")], "nextCursor": None},
        ]) as request:
            self.assertEqual([task["id"] for task in self.client.list_daily_habits()], ["new", "old"])
            self.assertIn("type=habit", request.call_args_list[0].args[1])
            self.assertIn("cursor=page%3A3", request.call_args_list[2].args[1])

    def test_malformed_or_looping_lists_are_not_success(self) -> None:
        for response in ({}, {"tasks": []}, {"tasks": {}, "nextCursor": None},
                         {"tasks": [None], "nextCursor": None}, {"tasks": [], "nextCursor": 42}):
            with self.subTest(response=response), patch.object(self.client, "_request", return_value=response):
                with self.assertRaises(ApiError):
                    self.client.list_daily_habits()
        with patch.object(self.client, "_request", return_value={"tasks": [], "nextCursor": "loop"}):
            with self.assertRaises(ApiError):
                self.client.list_daily_habits()

    def test_unconfirmed_rewards_and_invalid_identity_are_retried(self) -> None:
        calls = [lambda: self.client.me(),
                 lambda: self.client.create_tracker_points(1, "review", "event", "Anki", "event"),
                 lambda: self.client.complete_habit("habit", "2026-09-02T12:00:00Z", "Anki", "event")]
        for call in calls:
            with patch.object(self.client, "_request", return_value={}):
                with self.assertRaises(ApiError) as raised:
                    call()
                self.assertTrue(raised.exception.retryable)

    def test_invalid_json_is_not_a_success_but_empty_204_is(self) -> None:
        with patch.object(self.client._opener, "open") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.status = 200
            for raw in (b"", b"not json", b"[]", b"null", b"\xff"):
                response.read.return_value = raw
                with self.assertRaises(ApiError):
                    self.client._request("GET", "/me")
            response.status = 204
            self.assertEqual(self.client._request("DELETE", "/tasks/fixture"), {})

    def test_authenticated_redirects_are_rejected_without_a_second_request(self) -> None:
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), self.assertRaises(ApiError) as raised:
                self.client._request("GET", f"/redirect/{code}")
            self.assertEqual(raised.exception.code, "redirect_refused")
            self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(_Handler.requests), 5)
        self.assertTrue(all("/redirect/" in request["path"] for request in _Handler.requests))
        request = Request("https://api.example.test/me", headers={"Authorization": "Bearer synthetic"})
        for target in ("http://api.example.test/me", "https://other.example.test/me", "https://api.example.test/other"):
            self.assertIsNone(_RejectRedirects().redirect_request(request, None, 302, "Found", Message(), target))

    def test_missing_or_invalid_day_rules_cannot_silently_use_local_midnight(self) -> None:
        for boundary in (None, {}, {"utcOffsetMinutes": True, "rolloverOffsetHours": 4},
                         {"utcOffsetMinutes": 0, "rolloverOffsetHours": float("nan")}):
            with self.subTest(boundary=boundary), patch.object(self.client, "_request", return_value={
                "userId": "user-1", "dayBoundary": boundary,
            }), self.assertRaises(ApiError):
                self.client.me()

    def test_connection_accepts_taskhero_afternoon_rollover_values(self) -> None:
        for hours in range(-11, 13):
            response = {"userId": "user-1", "dayBoundary": {"utcOffsetMinutes": 0, "rolloverOffsetHours": hours}}
            with self.subTest(hours=hours), patch.object(self.client, "_request", return_value=response):
                self.assertEqual(self.client.me(), response)


if __name__ == "__main__":
    unittest.main()
