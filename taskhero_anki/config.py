from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


MIN_REVIEW_BATCH_SIZE = 10
MAX_REVIEW_BATCH_SIZE = 100_000
MIN_REWARD_POINTS = 1
MAX_REWARD_POINTS = 3
MAX_DAILY_BATCH_GOAL = 100_000


@dataclass(frozen=True)
class DayBoundary:
    utc_offset_minutes: int = 0
    rollover_offset_hours: float = 0

    def __post_init__(self) -> None:
        if type(self.utc_offset_minutes) is not int or not -840 <= self.utc_offset_minutes <= 840:
            raise ValueError("TaskHero returned an invalid timezone offset.")
        hours = self.rollover_offset_hours
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not -24 < hours < 24:
            raise ValueError("TaskHero returned an invalid day rollover.")

    @classmethod
    def from_api(cls, raw: Any) -> "DayBoundary":
        if not isinstance(raw, dict):
            raise ValueError("Could not load TaskHero's day settings. Try again, or contact support@taskheroics.com if this continues.")
        return cls(raw.get("utcOffsetMinutes"), raw.get("rolloverOffsetHours"))

    def day(self, timestamp_ms: int) -> str:
        # Match TaskHero's current account-offset contract, not the computer's
        # timezone or Anki's scheduler rollover. Delayed reviews use their own time.
        shifted_seconds = timestamp_ms / 1000 + self.utc_offset_minutes * 60 - self.rollover_offset_hours * 3600
        return datetime.fromtimestamp(shifted_seconds, tz=timezone.utc).date().isoformat()

    def description(self) -> str:
        minutes = round(self.rollover_offset_hours * 60) % (24 * 60)
        offset = abs(self.utc_offset_minutes)
        sign = "+" if self.utc_offset_minutes >= 0 else "−"
        return f"TaskHero day resets at {minutes // 60:02d}:{minutes % 60:02d} (UTC{sign}{offset // 60:02d}:{offset % 60:02d})."


@dataclass(frozen=True)
class Settings:
    review_batch_size: int = 10
    daily_batch_goal: int = 3
    reward_points: int = 1
    habit_id: Optional[str] = None
    habit_title: Optional[str] = None
    day_boundary: Optional[DayBoundary] = DayBoundary()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Settings":
        boundary = raw.get("day_boundary")
        if boundary is not None and not isinstance(boundary, dict):
            raise ValueError("Saved day settings must be an object.")
        review_batch_size = _bounded_int(
            raw.get("review_batch_size"),
            cls.review_batch_size,
            MIN_REVIEW_BATCH_SIZE,
            MAX_REVIEW_BATCH_SIZE,
        )
        reward_points = _bounded_int(
            raw.get("reward_points"),
            cls.reward_points,
            MIN_REWARD_POINTS,
            MAX_REWARD_POINTS,
        )
        settings = cls(
            review_batch_size=review_batch_size,
            daily_batch_goal=_bounded_int(
                raw.get("daily_batch_goal"),
                cls.daily_batch_goal,
                1,
                MAX_DAILY_BATCH_GOAL,
            ),
            reward_points=reward_points,
            habit_id=_optional_string(raw.get("habit_id")),
            habit_title=_optional_string(raw.get("habit_title")),
            # Existing development profiles must fetch authoritative rules; do
            # not silently interpret a missing boundary as desktop midnight.
            day_boundary=(DayBoundary(boundary.get("utc_offset_minutes"), boundary.get("rollover_offset_hours"))
                          if boundary is not None else None),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not MIN_REVIEW_BATCH_SIZE <= self.review_batch_size <= MAX_REVIEW_BATCH_SIZE:
            raise ValueError("Review batch size must be between 10 and 100,000.")
        if not 1 <= self.daily_batch_goal <= MAX_DAILY_BATCH_GOAL:
            raise ValueError("Daily batch goal must be between 1 and 100,000.")
        if not MIN_REWARD_POINTS <= self.reward_points <= MAX_REWARD_POINTS:
            raise ValueError("Reward points must be between 1 and 3 (TaskHero's Epic task value).")


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.load_error: Optional[str] = None

    def load(self) -> Settings:
        self.load_error = None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise ValueError("Settings must be an object.")
        except FileNotFoundError:
            return Settings(day_boundary=None)
        except (OSError, ValueError, TypeError):
            self.load_error = (
                "Saved TaskHero settings could not be read. Rewards are paused. "
                "Refresh habits, choose your settings, and Save to reconnect."
            )
            return Settings(day_boundary=None)
        try:
            return Settings.from_mapping(value)
        except (ValueError, TypeError):
            # Preserve the usable preferences, but never guess a day boundary:
            # the controller must pause accounting until fresh rules are saved.
            self.load_error = (
                "Saved TaskHero day settings are invalid. Rewards are paused. "
                "Refresh habits and Save to continue."
            )
            return Settings.from_mapping({**value, "day_boundary": None})

    def save(self, settings: Settings) -> None:
        settings.validate()
        _atomic_json_write(self.path, asdict(settings), 0o600)
        self.load_error = None


def _atomic_json_write(path: Path, value: Mapping[str, Any], mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, mode)
        except OSError:
            pass
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _bounded_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    parsed = _integer(value)
    if parsed is None:
        return fallback
    return min(maximum, max(minimum, parsed))


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
