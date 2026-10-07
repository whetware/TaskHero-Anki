from __future__ import annotations

from typing import Any, Callable, Dict, List

from aqt.operations import QueryOp
from aqt.qt import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimer,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from .api import ADDON_VERSION, ApiError
from .config import (
    MAX_DAILY_BATCH_GOAL,
    MAX_REVIEW_BATCH_SIZE,
    MAX_REWARD_POINTS,
    MIN_REVIEW_BATCH_SIZE,
    MIN_REWARD_POINTS,
    DayBoundary,
    Settings,
)


class SettingsDialog(QDialog):
    def __init__(self, controller: Any, parent: QWidget) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("TaskHero for Anki")
        self.setMinimumWidth(540)
        self.resize(640, 700)
        self._busy_buttons: List[QPushButton] = []
        self._closed = False
        self._day_boundary = controller.settings.day_boundary if controller.connected else None

        root = QVBoxLayout(self)
        # Keep Save/Cancel reachable when wrapped help text exceeds a small screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        intro = QLabel(
            "Earn TaskHero rewards while you study. Your cards, decks and answers stay in Anki. "
            "Get an Anki integration token in TaskHero’s Integrations & API → Anki."
        )
        intro.setWordWrap(True)
        content_layout.addWidget(intro)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.api_key = QLineEdit(controller.api_key or "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("th-int-v1-anki-…")
        self.api_key.setReadOnly(controller.connected)
        self.api_key.setToolTip("Disconnect first to change the saved Anki token or TaskHero account.")
        form.addRow("Anki integration token", self.api_key)

        connection_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.refresh_button = QPushButton("Refresh habits")
        connection_row.addWidget(self.test_button)
        connection_row.addWidget(self.refresh_button)
        connection_widget = QWidget()
        connection_widget.setLayout(connection_row)
        form.addRow("Connection", connection_widget)

        habit_row = QHBoxLayout()
        self.habit_combo = QComboBox()
        self.create_habit_button = QPushButton("Create “Study Anki”")
        habit_row.addWidget(self.habit_combo, 1)
        habit_row.addWidget(self.create_habit_button)
        habit_widget = QWidget()
        habit_widget.setLayout(habit_row)
        form.addRow("Daily habit", habit_widget)
        self.habit_warning = QLabel()
        self.habit_warning.setTextFormat(Qt.TextFormat.PlainText)
        self.habit_warning.setObjectName("Daily habit warning")
        self.habit_warning.setWordWrap(True)
        self.habit_warning.setStyleSheet("color: #a52828; font-weight: 600;")
        form.addRow("", self.habit_warning)
        self.habit_warning.hide()

        self.batch_size = _spinbox(
            MIN_REVIEW_BATCH_SIZE,
            MAX_REVIEW_BATCH_SIZE,
            controller.settings.review_batch_size,
        )
        self.reward_points = _spinbox(MIN_REWARD_POINTS, MAX_REWARD_POINTS, controller.settings.reward_points)
        self.daily_batch_goal = _spinbox(1, MAX_DAILY_BATCH_GOAL, controller.settings.daily_batch_goal)
        _add_explained_row(
            form, "Reviews per batch", self.batch_size,
            "At least 10 reviews. Again, Hard, Good and Easy count equally. Changing this starts a fresh batch.",
        )
        _add_explained_row(
            form, "Points per batch", self.reward_points,
            "1 = Normal, 2 = Hard, 3 = Epic. These are extra points, separate from the daily habit reward. "
            "Changing this starts a fresh batch; rewards already queued stay unchanged.",
        )
        _add_explained_row(
            form, "Batches to complete daily habit", self.daily_batch_goal,
            "Complete the selected habit once per TaskHero day after this many batches. "
            "Extra batches keep earning points. If you’re offline, rewards wait until TaskHero is reachable.",
        )
        content_layout.addLayout(form)

        self.day_boundary_label = QLabel()
        self.day_boundary_label.setObjectName("TaskHero day boundary")
        self.day_boundary_label.setWordWrap(True)
        content_layout.addWidget(self.day_boundary_label)
        self._show_day_boundary()

        self.reward_summary = QLabel()
        self.reward_summary.setObjectName("Reward settings summary")
        self.reward_summary.setAccessibleName("Reward settings summary")
        self.reward_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.reward_summary.setWordWrap(True)
        content_layout.addWidget(self.reward_summary)

        content_layout.addStretch(1)

        self.connection_status = QLabel("Connected" if controller.connected else "Disconnected")
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_status.setWordWrap(True)
        root.addWidget(self.connection_status)

        queue_row = QHBoxLayout()
        self.queue_status = QLabel(controller.queue_summary())
        self.queue_status.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_status.setWordWrap(True)
        self.retry_button = QPushButton("Retry failed")
        queue_row.addWidget(self.queue_status, 1)
        queue_row.addWidget(self.retry_button)
        root.addLayout(queue_row)

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._refresh_queue_status)
        self.queue_timer.start(1_000)

        disconnect_row = QHBoxLayout()
        self.disconnect_button = QPushButton("Disconnect and stop TaskHero activity")
        disconnect_row.addStretch(1)
        disconnect_row.addWidget(self.disconnect_button)
        root.addLayout(disconnect_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(self.buttons)

        footer = QLabel(
            f'TaskHero for Anki {ADDON_VERSION} · '
            '<a href="mailto:support@taskheroics.com">Help</a> · '
            '<a href="https://github.com/whetware/TaskHero-Anki/blob/main/PRIVACY.md">Privacy</a>'
        )
        footer.setOpenExternalLinks(True)
        root.addWidget(footer)

        self._busy_buttons.extend(
            [self.test_button, self.refresh_button, self.create_habit_button, self.retry_button]
        )
        self.test_button.clicked.connect(self._test_connection)
        self.refresh_button.clicked.connect(self._refresh_habits)
        self.create_habit_button.clicked.connect(self._create_habit)
        self.retry_button.clicked.connect(self._retry_failed)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        self.finished.connect(self._on_finished)
        self.api_key.textChanged.connect(self._on_key_changed)
        self.batch_size.valueChanged.connect(self._refresh_reward_summary)
        self.reward_points.valueChanged.connect(self._refresh_reward_summary)
        self.daily_batch_goal.valueChanged.connect(self._refresh_reward_summary)
        self.habit_combo.currentIndexChanged.connect(self._refresh_reward_summary)

        self._populate_habits([], preserve_saved=True)
        self._refresh_reward_summary()
        if controller.connected:
            self._refresh_habits()

    def _refresh_reward_summary(self, _value: Any = None) -> None:
        reviews = self.batch_size.value()
        points = self.reward_points.value()
        batches = self.daily_batch_goal.value()
        point_word = "point" if points == 1 else "points"
        batch_word = "batch" if batches == 1 else "batches"
        habit = (
            f"“{self.habit_combo.currentText()}”"
            if isinstance(self.habit_combo.currentData(), str)
            else "the selected daily habit"
        )
        self.reward_summary.setText(
            f"Summary: Every {reviews} reviews earns {points} TaskHero {point_word}. "
            f"TaskHero completes {habit} after {batches} completed {batch_word} "
            f"(normally {reviews * batches:,} reviews). Extra batches continue earning points."
        )

    def _current_client(self):
        key = self.api_key.text().strip()
        if not key:
            raise ValueError("Enter an Anki integration token first.")
        return self.controller.client_for_key(key)

    def _on_key_changed(self, _text: str) -> None:
        self._day_boundary = None
        self._show_day_boundary()
        self.habit_combo.clear()
        self.habit_combo.addItem("Refresh habits for this token", None)
        self.habit_warning.hide()

    def _on_finished(self, _result: int) -> None:
        self._closed = True
        self.queue_timer.stop()

    def _test_connection(self) -> None:
        try:
            client = self._current_client()
        except ValueError as error:
            showWarning(str(error), parent=self)
            return
        self._run_background(
            "Testing connection…",
            client.me,
            self._connection_tested,
        )

    def _connection_tested(self, account: Dict[str, Any]) -> None:
        self._day_boundary = DayBoundary.from_api(account.get("dayBoundary"))
        self._show_day_boundary()
        self.connection_status.setText("Connected to TaskHero. Save to apply these day settings.")

    def _show_day_boundary(self) -> None:
        description = (
            self._day_boundary.description() if self._day_boundary is not None
            else "Test the connection or refresh habits to load TaskHero's day settings."
        )
        self.day_boundary_label.setText(
            description + " After changing TaskHero's timezone or day rollover, refresh here and Save."
        )

    def _refresh_habits(self) -> None:
        try:
            client = self._current_client()
        except ValueError as error:
            showWarning(str(error), parent=self)
            return
        def loaded(result):
            account, habits = result
            self._connection_tested(account)
            self._habits_loaded(habits)

        self._run_background("Loading daily habits…", lambda: (client.me(), client.list_daily_habits()), loaded)

    def _create_habit(self) -> None:
        try:
            client = self._current_client()
        except ValueError as error:
            showWarning(str(error), parent=self)
            return
        def created(result):
            account, habit = result
            self._connection_tested(account)
            self._habit_created(habit)

        self._run_background("Creating “Study Anki”…", lambda: (client.me(), client.create_daily_habit()), created)

    def _habits_loaded(self, habits: List[Dict[str, Any]]) -> None:
        self._populate_habits(habits)
        saved_id = self.controller.settings.habit_id
        if saved_id and not any(habit.get("id") == saved_id for habit in habits):
            title = self.controller.settings.habit_title or "Previously selected habit"
            self.habit_warning.setText(
                f"“{title}” was deleted or is no longer eligible. Anki cannot complete it. "
                "Choose or create an every-day, one-rep habit and Save. "
                "A missed completion will not be replayed on the new habit."
            )
            self.habit_warning.show()
            self.connection_status.setText("Connected. Your selected daily habit needs replacing.")
        elif habits:
            self.habit_warning.hide()
            self.connection_status.setText("Connected. Select a daily habit and save.")
        else:
            self.habit_warning.hide()
            self.connection_status.setText("Connected. No eligible every-day, one-rep habits found.")

    def _habit_created(self, habit: Dict[str, Any]) -> None:
        self._populate_habits([habit])
        self.habit_warning.hide()
        habit_id = habit.get("id")
        if isinstance(habit_id, str):
            index = self.habit_combo.findData(habit_id)
            if index >= 0:
                self.habit_combo.setCurrentIndex(index)
        self.connection_status.setText("Created “Study Anki”. Save to use it for the daily goal.")

    def _populate_habits(self, habits: List[Dict[str, Any]], preserve_saved: bool = False) -> None:
        selected_id = self.habit_combo.currentData() or self.controller.settings.habit_id
        selected_title = self.controller.settings.habit_title
        self.habit_combo.clear()
        self.habit_combo.addItem("Select or create a daily habit", None)
        self.habit_warning.hide()

        seen = set()
        for habit in habits:
            habit_id, title = habit.get("id"), habit.get("title")
            if isinstance(habit_id, str) and isinstance(title, str):
                self.habit_combo.addItem(title, habit_id)
                seen.add(habit_id)
        if preserve_saved and selected_id and selected_id not in seen:
            self.habit_combo.addItem(selected_title or "Previously selected habit", selected_id)

        if selected_id:
            index = self.habit_combo.findData(selected_id)
            if index >= 0:
                self.habit_combo.setCurrentIndex(index)

    def _save(self) -> None:
        api_key = self.api_key.text().strip()
        habit_id = self.habit_combo.currentData()
        if not api_key:
            showWarning("Enter an Anki integration token.", parent=self)
            return
        if not isinstance(habit_id, str) or not habit_id:
            showWarning("Select or create an every-day TaskHero habit.", parent=self)
            return
        if self._day_boundary is None:
            showWarning("Test the connection or refresh habits to load TaskHero's day settings before saving.", parent=self)
            return
        settings = Settings(
            review_batch_size=self.batch_size.value(),
            daily_batch_goal=self.daily_batch_goal.value(),
            reward_points=self.reward_points.value(),
            habit_id=habit_id,
            habit_title=self.habit_combo.currentText(),
            day_boundary=self._day_boundary,
        )
        try:
            self.controller.save_settings(api_key, settings)
        except (ValueError, OSError) as error:
            showWarning(str(error), parent=self)
            return
        self.accept()

    def _disconnect(self) -> None:
        if not askUser(
            "Remove the saved TaskHero credential and cancel all unsent TaskHero activity for this Anki profile? "
            "Reconnecting starts fresh batch and daily-goal progress, even with a replacement token. "
            "A request already sent may still finish. No further requests will be started.",
            parent=self,
        ):
            return
        try:
            cancelled = self.controller.disconnect()
        except OSError as error:
            showWarning(str(error), parent=self)
            self.connection_status.setText("Disconnected. Could not finish removing saved settings; try Disconnect again.")
            return
        self.api_key.clear()
        self.api_key.setReadOnly(False)
        self.habit_combo.clear()
        self.habit_combo.addItem("Select or create a daily habit", None)
        self.connection_status.setText(f"Disconnected. Cancelled {cancelled} unsent event(s).")
        self.queue_status.setText(self.controller.queue_summary())

    def _retry_failed(self) -> None:
        count = self.controller.retry_failed()
        self._refresh_queue_status()
        tooltip(f"Queued {count} failed TaskHero event(s) for retry.", parent=self)

    def _refresh_queue_status(self) -> None:
        if not self._closed:
            self.queue_status.setText(self.controller.queue_summary())

    def _run_background(
        self,
        busy_text: str,
        operation: Callable[[], Any],
        success: Callable[[Any], None],
    ) -> None:
        self.connection_status.setText(busy_text)
        self._set_busy(True)

        def on_success(result: Any) -> None:
            if self._closed or not self.controller.active:
                return
            self._set_busy(False)
            success(result)

        def on_failure(error: Exception) -> None:
            if self._closed or not self.controller.active:
                return
            self._set_busy(False)
            if isinstance(error, ApiError):
                self.connection_status.setText(f"Connection failed: {error.message}")
            else:
                self.connection_status.setText(f"Connection failed: {error}")

        try:
            query = QueryOp(parent=self, op=lambda _collection: operation(), success=on_success).without_collection()
            query.failure(on_failure).run_in_background()
        except Exception as error:
            on_failure(error)

    def _set_busy(self, busy: bool) -> None:
        for button in self._busy_buttons:
            button.setDisabled(busy)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setDisabled(busy)
        self.api_key.setDisabled(busy)
        self.habit_combo.setDisabled(busy)
        self.disconnect_button.setDisabled(busy)


def _spinbox(minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


def _add_explained_row(form: QFormLayout, title: str, field: QSpinBox, explanation: str) -> None:
    label = QLabel(title)
    label.setBuddy(field)
    field.setAccessibleName(title)
    field.setAccessibleDescription(explanation)
    field.setToolTip(explanation)

    help_text = QLabel(explanation)
    help_text.setWordWrap(True)
    help_text.setObjectName(f"{title}.help")
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(4)
    layout.addWidget(field)
    layout.addWidget(help_text)
    form.addRow(label, content)
