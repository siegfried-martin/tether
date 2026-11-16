"""The calendar connectors' common shape: one event instance inside the window.

Times are stored as text so they compare and group exactly:
- timed: UTC, "YYYY-MM-DDTHH:MM:SSZ"
- all-day: the date, "YYYY-MM-DD" (end is exclusive, as in Google and iCalendar)
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tether.db import iso

WINDOW_BACK_DAYS = 7
WINDOW_AHEAD_DAYS = 90

BUSY_LEVELS = ("free", "tentative", "busy", "oof")   # ascending: the merged copy takes the highest


@dataclass(frozen=True)
class Instance:
    uid: str
    instance_start: str      # original start: identifies the instance within a series
    start: str
    end: str
    all_day: bool
    title: str
    location: str | None = None
    description: str | None = None
    modified_at: str | None = None
    busy: str = "busy"
    # Google only, for deleting from the event panel (not part of the content hash):
    external_id: str | None = None      # this instance's event ID
    series_id: str | None = None        # the repeating series' event ID
    organizer_self: bool = True         # the calendar's owner organizes it
    guests: int = 0                     # other people invited

    @property
    def key(self) -> str:
        return f"{self.uid}|{self.instance_start}"


@dataclass(frozen=True)
class Window:
    """The rolling window: 7 days back to 90 days ahead, in the owner's zone."""
    first_day: date
    last_day: date           # exclusive
    tz: ZoneInfo

    @classmethod
    def around(cls, today: date, tz: ZoneInfo) -> "Window":
        return cls(today - timedelta(days=WINDOW_BACK_DAYS), today + timedelta(days=WINDOW_AHEAD_DAYS), tz)

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first_day, time(0), self.tz)

    @property
    def end(self) -> datetime:
        return datetime.combine(self.last_day, time(0), self.tz)

    def starts_before(self, start: str, all_day: bool) -> bool:
        """True when an event starting at `start` has dropped behind the window."""
        if all_day:
            return start < self.first_day.isoformat()
        return start < iso(self.start)

    def contains(self, start: str, all_day: bool) -> bool:
        if all_day:
            return self.first_day.isoformat() <= start < self.last_day.isoformat()
        return iso(self.start) <= start < iso(self.end)


def fmt(value: date | datetime, tz: ZoneInfo) -> str:
    """Normalize a start or end. A floating (zone-less) time is read in the owner's zone."""
    if isinstance(value, datetime):
        return iso(value if value.tzinfo else value.replace(tzinfo=tz))
    return value.isoformat()


def is_past(start: str, all_day: bool, now: datetime, tz: ZoneInfo) -> bool:
    if all_day:
        return start < now.astimezone(tz).date().isoformat()
    return start < iso(now)
