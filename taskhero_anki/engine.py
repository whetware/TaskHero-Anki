from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .api import ApiError, TaskHeroClient
from .storage import OutboundEvent, StateStore


@dataclass(frozen=True)
class WorkerResult:
    outcome: str
    event_kind: Optional[str] = None
    message: Optional[str] = None


def process_next_event(store: StateStore, client: TaskHeroClient, now_ms: Optional[int] = None) -> WorkerResult:
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    event = store.claim_next_event(current_ms)
    if event is None:
        return WorkerResult("idle")

    try:
        response = _send_event(client, event)
    except ApiError as error:
        if error.retryable:
            delay_seconds = error.retry_after_seconds or _backoff_seconds(event.attempt_count)
            store.reschedule(event.event_id, current_ms + delay_seconds * 1000, str(error), current_ms)
            return WorkerResult("queued", event.kind, str(error))
        message = str(error)
        if event.kind == "habit_completion" and error.status in (403, 404):
            message += (
                " Open Tools → TaskHero for Anki, refresh habits, and select or create an eligible "
                "daily habit. A missed completion will not be replayed on a replacement."
            )
        store.mark_failed(event.event_id, message, current_ms)
        return WorkerResult("failed", event.kind, message)
    except Exception as error:
        delay_seconds = _backoff_seconds(event.attempt_count)
        store.reschedule(event.event_id, current_ms + delay_seconds * 1000, str(error), current_ms)
        return WorkerResult("queued", event.kind, str(error))

    store.mark_sent(event, response, current_ms)
    if event.kind == "tracker_point":
        value = event.payload.get("value")
        return WorkerResult("sent", event.kind, f"TaskHero awarded {value} point{'s' if value != 1 else ''}.")
    return WorkerResult("sent", event.kind, "TaskHero daily habit completed.")


def _send_event(client: TaskHeroClient, event: OutboundEvent):
    if event.kind == "tracker_point":
        return client.create_tracker_points(
            int(event.payload["value"]), str(event.payload["description"]), str(event.payload["sourceId"]),
            str(event.payload["sourceName"]), str(event.payload["idempotencyKey"])
        )
    if event.kind == "habit_completion":
        return client.complete_habit(
            str(event.payload["habitId"]), str(event.payload["completedAt"]), str(event.payload["sourceName"]),
            str(event.payload["idempotencyKey"])
        )
    raise ValueError(f"Unsupported outbound event kind: {event.kind}")


def _backoff_seconds(attempt_count: int) -> int:
    return min(300, max(2, 2 ** min(attempt_count, 8)))
