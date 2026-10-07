from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional, Set, Tuple

from anki.cards import Card
from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import QAction, QMenu, QTimer
from aqt.utils import tooltip

from .api import TaskHeroClient
from .config import Settings, SettingsStore
from .credentials import CredentialStore
from .engine import WorkerResult, process_next_event
from .storage import PendingReview, StateStore


UNDO_GRACE_MS = 10_000
TICK_INTERVAL_MS = 2_000
_CONNECTION_RESET_MESSAGE = (
    "The saved Anki token is unavailable, but previous TaskHero activity remains. "
    "Choose Disconnect to cancel unsent rewards and reset progress before reconnecting."
)


class AddonController:
    def __init__(self) -> None:
        profile_folder = Path(mw.pm.profileFolder())
        self.data_folder = profile_folder / "taskhero_anki"
        self.settings_store = SettingsStore(self.data_folder / "settings.json")
        self.credential_store = CredentialStore(self.data_folder / "credentials.json")
        self.store = StateStore(self.data_folder / "state.sqlite3")
        self.settings = self.settings_store.load()
        self.api_key = self.credential_store.load()
        self._timer = QTimer(mw)
        self._timer.timeout.connect(self.tick)
        self._worker_busy = False
        self._worker_lock = Lock()
        self._unexpected_error: Optional[str] = None
        self._menu: Optional[QMenu] = None
        self._shutting_down = False

    def start(self) -> None:
        self._shutting_down = False
        if self.connected and self.settings.habit_id:
            # Repair a shutdown between saving a new selection and retiring the
            # old unsent completion. Its ledger row still blocks same-day replay.
            self.store.cancel_unsent_habit_events_for_other_habit(self.settings.habit_id, _now_ms())
        self._install_menu()
        self._ensure_revlog_baseline()
        self.scan_new_revlog_entries()
        self._timer.start(TICK_INTERVAL_MS)
        self.tick()

    def shutdown(self) -> None:
        self._shutting_down = True
        self._timer.stop()
        if self._menu is not None:
            try:
                mw.form.menuTools.removeAction(self._menu.menuAction())
            except RuntimeError:
                pass
            self._menu.deleteLater()
            self._menu = None

    @property
    def connected(self) -> bool:
        return bool(self.api_key)

    @property
    def active(self) -> bool:
        return not self._shutting_down

    @property
    def needs_connection_reset(self) -> bool:
        return not self.connected and self.store.has_reward_session_activity()

    def client_for_key(self, api_key: str) -> TaskHeroClient:
        return TaskHeroClient(api_key)

    def save_settings(self, api_key: str, settings: Settings) -> None:
        settings.validate()
        if not self.active:
            raise ValueError("This Anki profile has closed. Reopen settings in the active profile.")
        if self.connected and api_key.strip() != self.api_key:
            raise ValueError("Disconnect before replacing the Anki token or changing TaskHero accounts.")
        if not self.connected and self._worker_busy:
            raise ValueError("Wait for the last in-flight request to finish before reconnecting.")
        # An unavailable credential does not end its ledger session. Require the
        # existing, confirmed Disconnect path before another account can use it.
        if self.needs_connection_reset:
            raise ValueError(_CONNECTION_RESET_MESSAGE)
        if settings.day_boundary is None:
            raise ValueError("Test the connection or refresh habits to load TaskHero's day settings before saving.")
        if (not self.connected or self.settings.day_boundary is None) and mw.col is not None:
            # Exclude reviews already present on this desktop at reconnect.
            # Unseen mobile history can still arrive in a later sync.
            # Existing development profiles without day settings also need this
            # initial exclusion snapshot before their first post-upgrade scan.
            max_id = int(mw.col.db.scalar("SELECT coalesce(max(id), 0) FROM revlog") or 0)
            max_usn = int(mw.col.db.scalar("SELECT coalesce(max(usn), 0) FROM revlog") or 0)
            self.store.exclude_reviews([int(row[0]) for row in mw.col.db.all("SELECT id FROM revlog")])
            self.store.advance_scan_cursors(max_id, max_usn)
        self.credential_store.save(api_key)
        try:
            self.settings_store.save(settings)
        except OSError:
            if self.api_key:
                self.credential_store.save(self.api_key)
            else:
                self.credential_store.delete()
            raise
        self.api_key = api_key.strip()
        if self.settings.habit_id != settings.habit_id:
            self.store.cancel_unsent_habit_events_for_other_habit(settings.habit_id, _now_ms())
        self.settings = settings
        self._unexpected_error = None
        self._materialize_days({settings.day_boundary.day(_now_ms())})
        self.tick()

    def disconnect(self) -> int:
        now_ms = _now_ms()
        cancelled = self.store.cancel_unsent(now_ms)
        self.api_key = None
        try:
            self.credential_store.delete()
        except OSError as error:
            raise OSError("TaskHero activity is stopped for this session, but the saved credential could not be removed. "
                          "Fix the file permissions and disconnect again before restarting Anki.") from error
        self.settings = Settings(
            review_batch_size=self.settings.review_batch_size,
            daily_batch_goal=self.settings.daily_batch_goal,
            reward_points=self.settings.reward_points,
        )
        self.settings_store.save(self.settings)
        self._unexpected_error = None
        self.update_reviewer_status()
        return cancelled

    def retry_failed(self) -> int:
        count = self.store.retry_failed(_now_ms())
        self._start_worker()
        self.update_reviewer_status()
        return count

    def open_settings(self) -> None:
        from .ui import SettingsDialog

        dialog = SettingsDialog(self, mw)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def record_local_review(self, card: Card) -> None:
        if mw.col is None or not self.connected or not self.active or self.settings.day_boundary is None:
            return
        row = mw.col.db.first(
            "SELECT id, usn FROM revlog WHERE cid = ? AND ease BETWEEN 1 AND 4 ORDER BY id DESC LIMIT 1",
            card.id,
        )
        if not row:
            return
        revlog_id, usn = int(row[0]), int(row[1])
        now_ms = _now_ms()
        self.store.record_review(revlog_id, usn, "desktop", now_ms + UNDO_GRACE_MS, now_ms, self.settings.day_boundary)
        self.store.advance_scan_cursors(revlog_id, usn)
        self.update_reviewer_status()

    def scan_new_revlog_entries(self) -> None:
        if mw.col is None:
            return
        if self.connected and self.settings.day_boundary is None:
            return
        cursor_id, cursor_usn = self.store.scan_cursors()
        rows = mw.col.db.all(
            "SELECT id, usn, ease FROM revlog WHERE id > ? OR usn > ? ORDER BY id",
            cursor_id,
            cursor_usn,
        )
        now_ms = _now_ms()
        max_id, max_usn = cursor_id, cursor_usn
        answered_rows = []
        for revlog_id_raw, usn_raw, ease in rows:
            revlog_id, usn = int(revlog_id_raw), int(usn_raw)
            # Reviews discovered after sync cannot be undone on this desktop, so
            # they can become stable immediately. Local hook inserts win via the
            # primary key and retain their undo grace period.
            # Manual/automatic rescheduling also writes revlog rows (ease=0).
            # Advance past those rows, but only answered cards earn credit.
            if self.connected and 1 <= int(ease) <= 4:
                answered_rows.append((revlog_id, usn))
            max_id = max(max_id, revlog_id)
            max_usn = max(max_usn, usn)
        if self.connected and self.settings.day_boundary is not None:
            self.store.record_reviews(answered_rows, "sync", now_ms, now_ms, self.settings.day_boundary)
        elif rows:
            self.store.exclude_reviews([int(row[0]) for row in rows])
        self.store.advance_scan_cursors(max_id, max_usn)
        self.tick()

    def reconcile_after_undo(self) -> None:
        self._finalize_due_reviews(force_all_pending=True)
        self.update_reviewer_status()

    def tick(self) -> None:
        affected_days = self._finalize_due_reviews(force_all_pending=False)
        self._materialize_days(affected_days)
        self._start_worker()
        self.update_reviewer_status()

    def update_reviewer_status(self) -> None:
        if mw.state != "review" or mw.reviewer is None:
            return
        try:
            webview = mw.reviewer.bottom.web
        except AttributeError:
            return
        status, compact_status, state = self._status_texts()
        text_json = json.dumps(status)
        compact_text_json = json.dumps(compact_status)
        state_json = json.dumps(state)
        webview.eval(
            f"""
            (() => {{
                let status = document.getElementById('taskhero-anki-status');
                if (!status) {{
                    status = document.createElement('div');
                    status.id = 'taskhero-anki-status';
                    status.style.position = 'absolute';
                    status.style.left = '10px';
                    status.style.top = '4px';
                    status.style.zIndex = '1000';
                    status.style.padding = '2px 7px';
                    status.style.borderRadius = '8px';
                    status.style.font = '12px sans-serif';
                    status.style.background = 'rgba(38, 50, 56, 0.82)';
                    status.style.color = '#fff';
                    status.style.pointerEvents = 'none';
                    window.addEventListener('resize', () => {{
                        const current = document.getElementById('taskhero-anki-status');
                        if (current) {{
                            current.textContent = document.body.clientWidth < 520
                                ? current.dataset.compactText
                                : current.dataset.fullText;
                        }}
                    }});
                    document.body.appendChild(status);
                }}
                status.dataset.fullText = {text_json};
                status.dataset.compactText = {compact_text_json};
                status.textContent = document.body.clientWidth < 520
                    ? status.dataset.compactText
                    : status.dataset.fullText;
                status.setAttribute('role', 'status');
                status.setAttribute('aria-label', status.dataset.fullText);
                status.dataset.state = {state_json};
                status.style.background = status.dataset.state === 'failed'
                    ? 'rgba(183, 28, 28, 0.90)'
                    : status.dataset.state === 'queued'
                        ? 'rgba(230, 126, 34, 0.90)'
                        : status.dataset.state === 'disconnected'
                            ? 'rgba(84, 110, 122, 0.90)'
                            : 'rgba(38, 50, 56, 0.82)';
            }})();
            """
        )

    def status_text(self, compact: bool = False) -> Tuple[str, str]:
        status, compact_status, state = self._status_texts()
        return (compact_status if compact else status), state

    def _status_texts(self) -> Tuple[str, str, str]:
        if self.connected and self.settings.day_boundary is None:
            return "TH · Refresh day settings", "TH · Settings needed", "failed"
        if not self.connected:
            return "TH · Disconnected", "TH · Disconnected", "disconnected"
        today = self.settings.day_boundary.day(_now_ms())
        reviewer_progress = self.store.reviewer_progress(today, self.settings)
        daily_batches = min(reviewer_progress.daily_batches, self.settings.daily_batch_goal)
        goal_complete = reviewer_progress.daily_batches >= self.settings.daily_batch_goal
        checkmark = " ✓" if goal_complete else ""
        progress = (
            f"TH · Reviews {reviewer_progress.batch_reviews}/{reviewer_progress.batch_size} · "
            f"Batches {daily_batches}/{self.settings.daily_batch_goal}{checkmark}"
        )
        compact_progress = (
            f"TH · R{reviewer_progress.batch_reviews}/{reviewer_progress.batch_size} · "
            f"B{daily_batches}/{self.settings.daily_batch_goal}{checkmark}"
        )
        queue = self.store.queue_stats()
        if queue.failed:
            suffix, state = f" · {queue.failed} failed", "failed"
        elif self._unexpected_error:
            suffix, state = " · failed", "failed"
        elif queue.queued:
            suffix, state = f" · {queue.queued} queued", "queued"
        else:
            suffix, state = "", "connected"
        return f"{progress}{suffix}", f"{compact_progress}{suffix}", state

    def queue_summary(self) -> str:
        queue = self.store.queue_stats()
        summary = f"Queued: {queue.queued}    Failed: {queue.failed}"
        if self.needs_connection_reset:
            summary += f"\n{_CONNECTION_RESET_MESSAGE}"
        if queue.last_error:
            summary += f"\nLast error: {queue.last_error}"
        if self.settings_store.load_error:
            summary += f"\n{self.settings_store.load_error}"
        if self._unexpected_error:
            detail = self._unexpected_error
            if self.api_key:
                detail = detail.replace(self.api_key, "[redacted]")
            detail = detail[:500]
            summary += (
                f"\nLocal error: {detail}\nCheck disk space and profile permissions, then restart Anki. "
                "Contact support@taskheroics.com if the problem continues."
            )
        return summary

    def _ensure_revlog_baseline(self) -> None:
        if mw.col is None:
            return
        max_id = int(mw.col.db.scalar("SELECT coalesce(max(id), 0) FROM revlog") or 0)
        max_usn = int(mw.col.db.scalar("SELECT coalesce(max(usn), 0) FROM revlog") or 0)
        self.store.ensure_scan_baseline(max_id, max_usn)

    def _finalize_due_reviews(self, force_all_pending: bool) -> Set[str]:
        if mw.col is None or not self.connected or self.settings.day_boundary is None:
            return set()
        cutoff_ms = 2**63 - 1 if force_all_pending else _now_ms()
        due = self.store.due_pending_reviews(cutoff_ms)
        if not due:
            return set()
        existing: Set[int] = set()
        for chunk in _chunks(due, 400):
            placeholders = ",".join("?" for _ in chunk)
            ids = [review.revlog_id for review in chunk]
            rows = mw.col.db.all(f"SELECT id FROM revlog WHERE id IN ({placeholders})", *ids)
            existing.update(int(row[0]) for row in rows)
        return self.store.finalize_reviews(due, existing, accept_existing=not force_all_pending)

    def _materialize_days(self, review_days: Set[str]) -> None:
        if not self.connected or not self.settings.habit_id or self.settings.day_boundary is None:
            return
        now_ms = _now_ms()
        for review_day in review_days:
            self.store.materialize_events(review_day, self.settings, now_ms)

    def _start_worker(self) -> None:
        if (
            self._shutting_down
            or not self.connected
            or self.settings.day_boundary is None
            or self._worker_busy
            or not self.store.has_due_event(_now_ms())
            or not self._worker_lock.acquire(blocking=False)
        ):
            return
        self._worker_busy = True
        client = self.client_for_key(self.api_key or "")

        operation = QueryOp(
            parent=mw,
            op=lambda _collection: process_next_event(self.store, client),
            success=self._worker_succeeded,
        ).without_collection()
        try:
            operation.failure(self._worker_failed).run_in_background()
        except Exception:
            self._worker_busy = False
            self._worker_lock.release()
            raise

    def _worker_succeeded(self, result: WorkerResult) -> None:
        self._worker_busy = False
        self._worker_lock.release()
        if self._shutting_down:
            return
        self._unexpected_error = None
        if self.connected and result.outcome == "sent" and result.message:
            tooltip(result.message, period=2500)
        self.update_reviewer_status()
        if result.outcome != "idle":
            QTimer.singleShot(100, self._start_worker)

    def _worker_failed(self, error: Exception) -> None:
        self._worker_busy = False
        self._worker_lock.release()
        if self._shutting_down:
            return
        self._unexpected_error = str(error)
        self.update_reviewer_status()

    def _install_menu(self) -> None:
        menu = QMenu("TaskHero for Anki", mw)
        settings_action = QAction("Settings…", mw)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        sync_action = QAction("Process queued activity", mw)
        sync_action.triggered.connect(self.tick)
        menu.addAction(sync_action)

        mw.form.menuTools.addMenu(menu)
        self._menu = menu


_controller: Optional[AddonController] = None
_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.profile_will_close.append(_on_profile_will_close)
    gui_hooks.reviewer_did_answer_card.append(_on_reviewer_did_answer_card)
    gui_hooks.reviewer_did_show_question.append(_on_reviewer_display_changed)
    gui_hooks.reviewer_did_show_answer.append(_on_reviewer_display_changed)
    gui_hooks.state_did_undo.append(_on_state_did_undo)
    gui_hooks.sync_did_finish.append(_on_sync_did_finish)


def _on_profile_open() -> None:
    global _controller
    if _controller is not None:
        _controller.shutdown()
    _controller = AddonController()
    _controller.start()


def _on_profile_will_close() -> None:
    global _controller
    if _controller is not None:
        _controller.shutdown()
        _controller = None


def _on_reviewer_did_answer_card(_reviewer: Any, card: Card, _ease: int) -> None:
    if _controller is not None:
        _controller.record_local_review(card)


def _on_reviewer_display_changed(_card: Card) -> None:
    if _controller is not None:
        QTimer.singleShot(0, _controller.update_reviewer_status)


def _on_state_did_undo(_changes: Any) -> None:
    if _controller is not None:
        _controller.reconcile_after_undo()


def _on_sync_did_finish() -> None:
    if _controller is not None:
        _controller.scan_new_revlog_entries()


def _chunks(values: List[PendingReview], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _now_ms() -> int:
    return int(time.time() * 1000)
