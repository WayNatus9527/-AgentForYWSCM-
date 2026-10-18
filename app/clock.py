"""Business clocks are per-engine. Authentication always uses real time."""
from datetime import datetime, timedelta, timezone
from threading import Lock
import time
import math


def parse_timestamp(value):
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('Timestamp must be an ISO timestamp or datetime')
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Timestamp must include an explicit UTC offset')
    return instant.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def security_timestamp():
    return time.time()


def monotonic_seconds():
    return time.monotonic()


class SystemBusinessClock:
    def now(self):
        return utc_now()


class VirtualBusinessClock:
    """Per-runtime clock; the simulator schedules every intervening event."""
    def __init__(self, start):
        self._current = parse_timestamp(start)
        self._lock = Lock()

    def now(self):
        with self._lock:
            return self._current.isoformat()

    def advance(self, *, seconds=0):
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Business time may advance only by a non-negative duration')
        with self._lock:
            self._current += timedelta(seconds=seconds)
            return self._current.isoformat()


def clock_for(settings):
    return VirtualBusinessClock(settings.virtual_time) if settings.virtual_time else SystemBusinessClock()


def business_now(engine):
    return engine.business_clock.now()
