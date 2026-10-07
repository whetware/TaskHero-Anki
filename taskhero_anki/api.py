from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import DayBoundary
from .credentials import normalize_integration_token


DEFAULT_API_BASE_URL = "https://api.taskhero.app/api/v1"
ADDON_VERSION = "1.0.0"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never follow a redirect, including HTTPS downgrades.
        # The fixed API endpoint has no legitimate redirect requirement.
        return None


@dataclass(frozen=True)
class ApiError(Exception):
    status: Optional[int]
    code: str
    message: str
    retry_after_seconds: Optional[int] = None

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in (408, 425, 429) or bool(self.status and self.status >= 500)

    def __str__(self) -> str:
        return self.message


class TaskHeroClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_API_BASE_URL, timeout_seconds: float = 10.0) -> None:
        self._api_key = normalize_integration_token(api_key)
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(_RejectRedirects())

    def me(self) -> Dict[str, Any]:
        response = self._request("GET", "/me")
        if not isinstance(response.get("userId"), str) or not response["userId"]:
            raise ApiError(None, "invalid_response", "TaskHero returned an invalid account identity.")
        try:
            DayBoundary.from_api(response.get("dayBoundary"))
        except ValueError as error:
            raise ApiError(None, "invalid_response", str(error)) from error
        return response

    def list_tasks(self) -> List[Dict[str, Any]]:
        tasks_by_id: Dict[str, Dict[str, Any]] = {}
        seen_cursors = set()
        cursor = None
        for _ in range(1000):
            query = {"status": "all", "limit": "100", "type": "habit"}
            if cursor is not None:
                query["cursor"] = cursor
            response = self._request("GET", f"/tasks?{urlencode(query)}")
            tasks = response.get("tasks")
            if not isinstance(tasks, list) or "nextCursor" not in response:
                raise ApiError(None, "invalid_response", "TaskHero returned an invalid task list.")
            for task in tasks:
                if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                    raise ApiError(None, "invalid_response", "TaskHero returned an invalid habit.")
                tasks_by_id[task["id"]] = task
            cursor = response["nextCursor"]
            if cursor is None:
                return list(tasks_by_id.values())
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ApiError(None, "invalid_response", "TaskHero returned an invalid task-list cursor.")
            seen_cursors.add(cursor)
        raise ApiError(None, "invalid_response", "TaskHero's task list exceeded the page limit.")

    def list_daily_habits(self) -> List[Dict[str, Any]]:
        return [task for task in self.list_tasks() if _is_eligible_daily_habit(task)]

    def create_daily_habit(self, title: str = "Study Anki") -> Dict[str, Any]:
        response = self._request(
            "POST",
            "/tasks",
            {
                "type": "habit",
                "title": title,
                "difficulty": "normal",
                "repsPerDayGoal": 1,
                "schedule": {"mode": "weekly", "daysOfWeek": [0, 1, 2, 3, 4, 5, 6]},
            },
        )
        task = response.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise ApiError(None, "invalid_response", "TaskHero returned an invalid habit.")
        return task

    def create_tracker_points(
        self, value: int, description: str, source_id: str, source_name: str, idempotency_key: str
    ) -> Dict[str, Any]:
        response = self._request(
            "POST",
            "/tracker-points",
            {"value": value, "description": description, "sourceId": source_id,
             "sourceName": source_name, "idempotencyKey": idempotency_key},
        )
        _require_result_id(response, "trackerPoint")
        return response

    def complete_habit(self, habit_id: str, completed_at: str, source_name: str, idempotency_key: str) -> Dict[str, Any]:
        response = self._request(
            "POST", f"/tasks/{quote(habit_id, safe='')}/complete",
            {"completedAt": completed_at, "sourceName": source_name,
             "idempotencyKey": idempotency_key, "onlyIfIncomplete": True},
        )
        if _require_result_id(response, "task") != habit_id or not isinstance(response.get("awarded"), dict):
            raise ApiError(None, "invalid_response", "TaskHero returned an invalid completion result.")
        return response

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": f"TaskHero-Anki/{ADDON_VERSION}",
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if response.status == 204:
                    return {}
                raw = response.read()
        except HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise ApiError(error.code, "redirect_refused", "TaskHero redirected the request. No credential was forwarded.")
            raw = error.read()
            envelope = _decode_json(raw)
            error_body = envelope.get("error") if isinstance(envelope, dict) else None
            code = error_body.get("code") if isinstance(error_body, dict) else "http_error"
            message = error_body.get("message") if isinstance(error_body, dict) else None
            retry_after = _parse_retry_after(error.headers.get("Retry-After"))
            raise ApiError(error.code, str(code), str(message or f"TaskHero request failed ({error.code})."), retry_after)
        except (URLError, TimeoutError, OSError) as error:
            raise ApiError(None, "network_error", f"Could not reach TaskHero: {error}")

        decoded = _decode_json(raw)
        if not isinstance(decoded, dict):
            raise ApiError(None, "invalid_response", "TaskHero returned an invalid response.")
        return decoded


def _is_eligible_daily_habit(task: Mapping[str, Any]) -> bool:
    if task.get("type") != "habit" or task.get("status") not in ("open", "completed"):
        return False
    if task.get("repsPerDayGoal") != 1:
        return False
    schedule = task.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("mode") != "weekly":
        return False
    days = schedule.get("daysOfWeek")
    return isinstance(days, list) and set(days) == set(range(7))


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _require_result_id(response: Mapping[str, Any], key: str) -> str:
    result = response.get(key)
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise ApiError(None, "invalid_response", "TaskHero did not confirm the reward. It will be retried safely.")
    return result["id"]


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None
