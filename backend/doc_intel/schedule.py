"""When the next automatic SharePoint sync is due (pure functions; all stored times naive UTC).

Semantics (plan §2.4 / §2.6):
- Enabling a schedule, or changing it, fires at the next ``sync_hour``:00 local time,
  unless the previous sync is recent enough that ``last sync + interval`` is still ahead.
- After any sync run (scheduled or "Sync now"), the next one is ``interval_days`` later,
  at ``sync_hour``:00 local time. A manual run therefore resets the clock: the interval
  means "at most N days between checks".
- A disabled schedule has no next run (None).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _to_local(value_utc: datetime, tz: ZoneInfo) -> datetime:
    aware = value_utc.replace(tzinfo=timezone.utc) if value_utc.tzinfo is None else value_utc
    return aware.astimezone(tz)


def _to_naive_utc(value_local: datetime) -> datetime:
    return value_local.astimezone(timezone.utc).replace(tzinfo=None)


def _at_hour(day: datetime, hour: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(day.date(), time(hour=hour), tzinfo=tz)


def next_after_run(run_started_utc: datetime, *, interval_days: int, hour: int, tz_name: str) -> datetime:
    """``hour``:00 local on the local date of (run start + interval_days)."""
    tz = ZoneInfo(tz_name)
    target_day = _to_local(run_started_utc, tz) + timedelta(days=int(interval_days))
    return _to_naive_utc(_at_hour(target_day, int(hour), tz))


def next_hour_occurrence(now_utc: datetime, *, hour: int, tz_name: str) -> datetime:
    """The next ``hour``:00 local strictly after ``now``."""
    tz = ZoneInfo(tz_name)
    local_now = _to_local(now_utc, tz)
    candidate = _at_hour(local_now, int(hour), tz)
    if candidate <= local_now:
        candidate = _at_hour(local_now + timedelta(days=1), int(hour), tz)
    return _to_naive_utc(candidate)


def next_sync_at(
    *,
    enabled: bool,
    interval_days: int,
    hour: int,
    tz_name: str,
    now_utc: datetime,
    last_sync_utc: datetime | None = None,
) -> datetime | None:
    """``next_sync_at`` for a schedule that was just saved (enabled, changed or disabled)."""
    if not enabled:
        return None
    if last_sync_utc is not None:
        candidate = next_after_run(last_sync_utc, interval_days=interval_days, hour=hour, tz_name=tz_name)
        if candidate > now_utc:
            return candidate
    return next_hour_occurrence(now_utc, hour=hour, tz_name=tz_name)
