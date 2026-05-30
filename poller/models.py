"""Shared types for the poller. Match docs/DATA_FORMAT.md."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Fix:
    """A single decrypted location report for one tracker.

    `ts` is timezone-aware UTC. `acc` is in metres or None if unknown.
    """

    id: str
    ts: datetime
    lat: float
    lng: float
    acc: float | None = None
    src: str = "network"

    def to_json_record(self) -> dict:
        rec: dict = {
            "id": self.id,
            "ts": self.ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": self.lat,
            "lng": self.lng,
            "src": self.src,
        }
        if self.acc is not None:
            rec["acc"] = self.acc
        return rec

    @property
    def utc_date_key(self) -> str:
        """The `YYYY-MM-DD` (UTC) the fix belongs to in the day file scheme."""
        return self.ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
