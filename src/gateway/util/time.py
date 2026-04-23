"""Small datetime helpers used across the gateway."""
from datetime import datetime, timezone


def iso8601_utc(ts: float) -> str:
    """Format a POSIX timestamp as a UTC ISO-8601 string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
