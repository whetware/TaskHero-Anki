from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import DayBoundary, Settings


@dataclass(frozen=True)
class PendingReview:
    revlog_id: int
    review_day: str


@dataclass(frozen=True)
class OutboundEvent:
    event_id: str
    kind: str
    review_day: str
    payload: Dict[str, Any]
    attempt_count: int


@dataclass(frozen=True)
class QueueStats:
    queued: int
    failed: int
    last_error: Optional[str]


@dataclass(frozen=True)
class ReviewerProgress:
    batch_reviews: int
    batch_size: int
    daily_batches: int


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        self._secure_database_files()
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._secure_database_files()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_events (
                    revlog_id INTEGER PRIMARY KEY,
                    reviewed_at_ms INTEGER NOT NULL,
                    review_day TEXT NOT NULL,
                    usn INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'discarded')),
                    stable_after_ms INTEGER NOT NULL,
                    recorded_at_ms INTEGER NOT NULL,
                    reward_session INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS review_events_day_state_idx
                    ON review_events (review_day, state);

                CREATE TABLE IF NOT EXISTS excluded_reviews (
                    revlog_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS outbound_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('tracker_point', 'habit_completion')),
                    review_day TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'sent', 'failed', 'cancelled')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at_ms INTEGER NOT NULL,
                    last_error TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    reward_session INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS outbound_events_status_available_idx
                    ON outbound_events (status, available_at_ms);

                CREATE TABLE IF NOT EXISTS sent_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    remote_id TEXT,
                    sent_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reward_day_progress (
                    review_day TEXT PRIMARY KEY,
                    credited_reviews INTEGER NOT NULL,
                    batch_size INTEGER NOT NULL,
                    reward_points INTEGER NOT NULL
                );
                """
            )
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            # Preserve existing development-profile receipts when adding session
            # accounting. A schema upgrade must not wipe duplicate protection.
            for table in ("review_events", "outbound_events"):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "reward_session" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN reward_session INTEGER NOT NULL DEFAULT 0")
            # Remove obsolete data without losing receipt identities or progress.
            # Full API responses are not needed to prevent duplicate delivery.
            for table, column in (("sent_events", "response_json"), ("reward_day_progress", "daily_points_cap")):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if column in columns:
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            self._secure_database_files()
        self.recover_in_flight()
        self._upgrade_queued_attribution()

    def _upgrade_queued_attribution(self) -> None:
        # Keep descriptions/identities frozen across retries. Older queued batches
        # did not save their review count, so do not infer it from today's settings.
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT event_id, kind, payload_json FROM outbound_events WHERE status IN ('pending', 'failed', 'in_flight')"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                upgraded = dict(payload)
                upgraded.setdefault("sourceName", "Anki")
                upgraded.setdefault("idempotencyKey", row["event_id"])
                if row["kind"] == "habit_completion":
                    upgraded.setdefault("onlyIfIncomplete", True)
                if upgraded != payload:
                    connection.execute(
                        "UPDATE outbound_events SET payload_json = ? WHERE event_id = ?",
                        (json.dumps(upgraded, sort_keys=True), row["event_id"]),
                    )

    def _secure_database_files(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                os.chmod(path, 0o600)
            except OSError:
                continue

    def profile_id(self) -> str:
        existing = self.get_meta("profile_id")
        if existing:
            return existing
        generated = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('profile_id', ?)", (generated,))
        return self.get_meta("profile_id") or generated

    def get_meta(self, key: str) -> Optional[str]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def ensure_scan_baseline(self, max_revlog_id: int, max_usn: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('scan_revlog_id', ?)", (str(max_revlog_id),))
            connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('scan_usn', ?)", (str(max_usn),))

    def scan_cursors(self) -> Tuple[int, int]:
        return _safe_int(self.get_meta("scan_revlog_id")), _safe_int(self.get_meta("scan_usn"))

    def advance_scan_cursors(self, max_revlog_id: int, max_usn: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current_id = _meta_int(connection, "scan_revlog_id")
            current_usn = _meta_int(connection, "scan_usn")
            _set_meta(connection, "scan_revlog_id", str(max(current_id, max_revlog_id)))
            _set_meta(connection, "scan_usn", str(max(current_usn, max_usn)))

    def record_review(
        self,
        revlog_id: int,
        usn: int,
        origin: str,
        stable_after_ms: int,
        recorded_at_ms: int,
        day_boundary: DayBoundary = DayBoundary(),
    ) -> bool:
        return self.record_reviews([(revlog_id, usn)], origin, stable_after_ms, recorded_at_ms, day_boundary) == 1

    def exclude_reviews(self, revlog_ids: Sequence[int]) -> None:
        # A USN changes when local rows are uploaded; cursors alone cannot keep
        # pre-connection/disconnected history excluded. Store only the IDs.
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR IGNORE INTO excluded_reviews (revlog_id) VALUES (?)",
                ((revlog_id,) for revlog_id in revlog_ids),
            )

    def record_reviews(
        self,
        rows: Sequence[Tuple[int, int]],
        origin: str,
        stable_after_ms: int,
        recorded_at_ms: int,
        day_boundary: DayBoundary,
    ) -> int:
        if not rows:
            return 0
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            session = _meta_int(connection, "reward_session")
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO review_events (
                    revlog_id, reviewed_at_ms, review_day, usn, origin, state, stable_after_ms, recorded_at_ms, reward_session
                ) SELECT ?, ?, ?, ?, ?, 'pending', ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM excluded_reviews WHERE revlog_id = ?)
                """,
                ((revlog_id, revlog_id, day_boundary.day(revlog_id), usn, origin,
                  stable_after_ms, recorded_at_ms, session, revlog_id) for revlog_id, usn in rows),
            )
        return cursor.rowcount

    def due_pending_reviews(self, now_ms: int) -> List[PendingReview]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT revlog_id, review_day FROM review_events WHERE state = 'pending' AND stable_after_ms <= ?",
                (now_ms,),
            ).fetchall()
        return [PendingReview(int(row["revlog_id"]), str(row["review_day"])) for row in rows]

    def finalize_reviews(
        self,
        due_reviews: Sequence[PendingReview],
        existing_revlog_ids: Set[int],
        accept_existing: bool = True,
    ) -> Set[str]:
        affected_days: Set[str] = set()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for review in due_reviews:
                exists = review.revlog_id in existing_revlog_ids
                if exists and not accept_existing:
                    continue
                state = "accepted" if exists else "discarded"
                connection.execute(
                    "UPDATE review_events SET state = ? WHERE revlog_id = ? AND state = 'pending'",
                    (state, review.revlog_id),
                )
                affected_days.add(review.review_day)
        return affected_days

    def review_count(self, review_day: str) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT count(*) AS count FROM review_events WHERE review_day = ? AND state = 'accepted' AND reward_session = ?",
                (review_day, _meta_int(connection, "reward_session")),
            ).fetchone()
        return int(row["count"]) if row else 0

    def reviewer_progress(self, review_day: str, settings: Settings) -> ReviewerProgress:
        settings.validate()
        with closing(self._connect()) as connection, connection:
            session = _meta_int(connection, "reward_session")
            counts = connection.execute(
                """
                SELECT count(*) AS progress_count,
                    sum(CASE WHEN state = 'accepted' THEN 1 ELSE 0 END) AS accepted_count
                FROM review_events WHERE review_day = ? AND state != 'discarded' AND reward_session = ?
                """,
                (review_day, session),
            ).fetchone()
            previous = connection.execute(
                """
                SELECT status FROM outbound_events
                WHERE review_day = ? AND kind = 'tracker_point' AND reward_session = ?
                """,
                (review_day, session),
            ).fetchall()
            progress = connection.execute(
                "SELECT * FROM reward_day_progress WHERE review_day = ?",
                (review_day,),
            ).fetchone()

        progress_reviews = int(counts["progress_count"] or 0)
        accepted_reviews = int(counts["accepted_count"] or 0)
        if progress is None:
            # Match the conservative upgrade rule in materialize_events(): old
            # rewards without progress metadata must not make old reviews reusable.
            credited_reviews = accepted_reviews if previous else 0
        elif (settings.review_batch_size, settings.reward_points) != (
            int(progress["batch_size"]),
            int(progress["reward_points"]),
        ):
            # Saving changed batch rules starts a fresh batch from the current
            # accepted-review baseline, even before the next worker tick.
            credited_reviews = accepted_reviews
        else:
            credited_reviews = int(progress["credited_reviews"])

        uncredited_reviews = max(0, progress_reviews - credited_reviews)
        return ReviewerProgress(
            # A full tentative batch remains visibly full during the Undo grace
            # period, then resets after its durable reward event is created.
            batch_reviews=min(uncredited_reviews, settings.review_batch_size),
            batch_size=settings.review_batch_size,
            daily_batches=sum(1 for row in previous if row["status"] != "cancelled"),
        )

    def materialize_events(self, review_day: str, settings: Settings, now_ms: int) -> List[str]:
        settings.validate()
        if settings.day_boundary is None:
            raise ValueError("Refresh TaskHero's day settings before earning rewards.")
        count = self.review_count(review_day)
        profile_id = self.profile_id()
        created: List[str] = []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            session = _meta_int(connection, "reward_session")
            previous = connection.execute(
                "SELECT reward_session FROM outbound_events WHERE review_day = ? AND kind = 'tracker_point'",
                (review_day,),
            ).fetchall()
            current_batches = [row for row in previous if row["reward_session"] == session]
            progress = connection.execute("SELECT * FROM reward_day_progress WHERE review_day = ?", (review_day,)).fetchone()
            batch_size = settings.review_batch_size
            reward_points = settings.reward_points
            if progress is None:
                # Pre-release ledgers did not retain batch sizes. Do not re-credit
                # already recorded reviews on upgrade; start the new accounting here.
                credited = count if current_batches else 0
            else:
                credited = int(progress["credited_reviews"])
                if review_day != settings.day_boundary.day(now_ms):
                    batch_size = progress["batch_size"]
                    reward_points = progress["reward_points"]
                elif (batch_size, reward_points) != (progress["batch_size"], progress["reward_points"]):
                    # Changes start a fresh batch; frozen queued/sent payloads never
                    # change, and old reviews cannot mint rewards under new rules.
                    credited = count
            batches = max(0, (count - credited) // batch_size)
            for batch_index in range(len(previous) + 1, len(previous) + batches + 1):
                event_id = deterministic_event_id(profile_id, "tracker", review_day, batch_index)
                payload = {
                    "value": reward_points,
                    "description": f"Completed {batch_size} review{'s' if batch_size != 1 else ''}",
                    "sourceId": event_id,
                    "sourceName": "Anki",
                    "idempotencyKey": event_id,
                }
                if _insert_event(connection, event_id, "tracker_point", review_day, payload, now_ms):
                    created.append(event_id)
                    credited += batch_size
            connection.execute(
                """INSERT INTO reward_day_progress (review_day, credited_reviews, batch_size, reward_points) VALUES (?, ?, ?, ?)
                ON CONFLICT(review_day) DO UPDATE SET credited_reviews = excluded.credited_reviews,
                    batch_size = excluded.batch_size, reward_points = excluded.reward_points""",
                (review_day, credited, batch_size, reward_points),
            )

            existing_habit = connection.execute(
                "SELECT 1 FROM outbound_events WHERE review_day = ? AND kind = 'habit_completion' LIMIT 1", (review_day,)
            ).fetchone()
            completed_batches = int(
                connection.execute(
                    """
                    SELECT count(*) FROM outbound_events
                    WHERE review_day = ? AND kind = 'tracker_point' AND status != 'cancelled' AND reward_session = ?
                    """,
                    (review_day, session),
                ).fetchone()[0]
            )
            if settings.habit_id and completed_batches >= settings.daily_batch_goal and not existing_habit:
                event_id = deterministic_event_id(profile_id, "habit", review_day, 1, settings.habit_id)
                goal_review = connection.execute(
                    """
                    SELECT max(reviewed_at_ms) AS reviewed_at_ms FROM review_events
                    WHERE review_day = ? AND state = 'accepted' AND reward_session = ?
                    """,
                    (review_day, session),
                ).fetchone()
                completed_at_ms = (
                    int(goal_review["reviewed_at_ms"])
                    if goal_review and goal_review["reviewed_at_ms"] is not None
                    else now_ms
                )
                payload = {
                    "habitId": settings.habit_id,
                    "completedAt": iso_timestamp(completed_at_ms),
                    "sourceName": "Anki",
                    "idempotencyKey": event_id,
                    "onlyIfIncomplete": True,
                }
                if _insert_event(connection, event_id, "habit_completion", review_day, payload, now_ms):
                    created.append(event_id)
        return created

    def claim_next_event(self, now_ms: int) -> Optional[OutboundEvent]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, kind, review_day, payload_json, attempt_count
                FROM outbound_events
                WHERE status = 'pending' AND available_at_ms <= ?
                ORDER BY created_at_ms, event_id
                LIMIT 1
                """,
                (now_ms,),
            ).fetchone()
            if row is None:
                return None
            attempt_count = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE outbound_events
                SET status = 'in_flight', attempt_count = ?, updated_at_ms = ?
                WHERE event_id = ? AND status = 'pending'
                """,
                (attempt_count, now_ms, str(row["event_id"])),
            )
        return OutboundEvent(
            event_id=str(row["event_id"]),
            kind=str(row["kind"]),
            review_day=str(row["review_day"]),
            payload=json.loads(str(row["payload_json"])),
            attempt_count=attempt_count,
        )

    def has_due_event(self, now_ms: int) -> bool:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM outbound_events WHERE status = 'pending' AND available_at_ms <= ? LIMIT 1",
                (now_ms,),
            ).fetchone()
        return row is not None

    def mark_sent(self, event: OutboundEvent, response: Dict[str, Any], now_ms: int) -> None:
        remote_id = _remote_id(response)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO sent_events (event_id, kind, remote_id, sent_at_ms) VALUES (?, ?, ?, ?)",
                (event.event_id, event.kind, remote_id, now_ms),
            )
            connection.execute(
                "UPDATE outbound_events SET status = 'sent', last_error = NULL, updated_at_ms = ? WHERE event_id = ?",
                (now_ms, event.event_id),
            )

    def reschedule(self, event_id: str, available_at_ms: int, error: str, now_ms: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE outbound_events
                SET status = 'pending', available_at_ms = ?, last_error = ?, updated_at_ms = ?
                WHERE event_id = ? AND status = 'in_flight'
                """,
                (available_at_ms, error[:500], now_ms, event_id),
            )

    def mark_failed(self, event_id: str, error: str, now_ms: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE outbound_events SET status = 'failed', last_error = ?, updated_at_ms = ? WHERE event_id = ? AND status = 'in_flight'",
                (error[:500], now_ms, event_id),
            )

    def recover_in_flight(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE outbound_events SET status = 'pending' WHERE status = 'in_flight'"
            )

    def retry_failed(self, now_ms: int) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE outbound_events
                SET status = 'pending', available_at_ms = ?, last_error = NULL, updated_at_ms = ?
                WHERE status = 'failed'
                """,
                (now_ms, now_ms),
            )
        return cursor.rowcount

    def cancel_unsent_habit_events_for_other_habit(self, habit_id: str, now_ms: int) -> int:
        # Keep every row in the ledger, including today's cancelled event. A new
        # selection takes effect for future days, never as a same-day replay.
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT event_id, payload_json FROM outbound_events
                WHERE kind = 'habit_completion' AND status IN ('pending', 'in_flight', 'failed')"""
            ).fetchall()
            old_ids = [
                row["event_id"] for row in rows
                if json.loads(row["payload_json"])["habitId"] != habit_id
            ]
            for event_id in old_ids:
                connection.execute(
                    """UPDATE outbound_events
                    SET status = 'cancelled', last_error = NULL, updated_at_ms = ?
                    WHERE event_id = ? AND status IN ('pending', 'in_flight', 'failed')""",
                    (now_ms, event_id),
                )
        return len(old_ids)

    def has_reward_session_activity(self) -> bool:
        with closing(self._connect()) as connection:
            session = _meta_int(connection, "reward_session")
            return bool(connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM review_events WHERE reward_session = ? AND state != 'discarded'
                ) OR EXISTS (
                    SELECT 1 FROM outbound_events WHERE reward_session = ? AND status != 'cancelled'
                ) OR EXISTS (SELECT 1 FROM reward_day_progress)
                """,
                (session, session),
            ).fetchone()[0])

    def cancel_unsent(self, now_ms: int) -> int:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE review_events SET state = 'discarded' WHERE state = 'pending'")
            cursor = connection.execute(
                """
                UPDATE outbound_events
                SET status = 'cancelled', updated_at_ms = ?
                WHERE status IN ('pending', 'in_flight', 'failed')
                """,
                (now_ms,),
            )
            # A new connection starts fresh even when it replaces the same user's
            # token. Retain review IDs, frozen events and receipts, but never use
            # the previous session's batches or partial reviews for new rewards.
            connection.execute("DELETE FROM reward_day_progress")
            _set_meta(connection, "reward_session", str(_meta_int(connection, "reward_session") + 1))
        return cursor.rowcount

    def queue_stats(self) -> QueueStats:
        with closing(self._connect()) as connection, connection:
            queued_row = connection.execute(
                "SELECT count(*) AS count FROM outbound_events WHERE status IN ('pending', 'in_flight')"
            ).fetchone()
            failed_row = connection.execute(
                "SELECT count(*) AS count FROM outbound_events WHERE status = 'failed'"
            ).fetchone()
            error_row = connection.execute(
                """
                SELECT last_error FROM outbound_events
                WHERE last_error IS NOT NULL AND status IN ('pending', 'failed')
                ORDER BY updated_at_ms DESC LIMIT 1
                """
            ).fetchone()
        return QueueStats(
            queued=int(queued_row["count"]) if queued_row else 0,
            failed=int(failed_row["count"]) if failed_row else 0,
            last_error=str(error_row["last_error"]) if error_row else None,
        )

    def event_statuses(self) -> List[Tuple[str, str]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT event_id, status FROM outbound_events ORDER BY event_id").fetchall()
        return [(str(row["event_id"]), str(row["status"])) for row in rows]


def iso_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def deterministic_event_id(profile_id: str, kind: str, review_day: str, ordinal: int, extra: str = "") -> str:
    digest = hashlib.sha256(f"{profile_id}|{kind}|{review_day}|{ordinal}|{extra}".encode("utf-8")).hexdigest()
    return f"anki-{digest[:40]}"


def _insert_event(
    connection: sqlite3.Connection,
    event_id: str,
    kind: str,
    review_day: str,
    payload: Dict[str, Any],
    now_ms: int,
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO outbound_events (
            event_id, kind, review_day, payload_json, status, attempt_count,
            available_at_ms, last_error, created_at_ms, updated_at_ms, reward_session
        ) VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?, ?)
        """,
        (event_id, kind, review_day, json.dumps(payload, separators=(",", ":"), sort_keys=True),
         now_ms, now_ms, now_ms, _meta_int(connection, "reward_session")),
    )
    return cursor.rowcount == 1


def _remote_id(response: Dict[str, Any]) -> Optional[str]:
    for key in ("trackerPoint", "task"):
        value = response.get(key)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            return str(value["id"])
    return None


def _safe_int(value: Optional[str]) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _meta_int(connection: sqlite3.Connection, key: str) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return _safe_int(str(row["value"])) if row else 0


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
