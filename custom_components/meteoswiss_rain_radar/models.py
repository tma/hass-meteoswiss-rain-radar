from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import DEFAULT_RAIN_MAX_AGE
from .radar import RadarData

# Rain health describes the update and the observation age only. The legacy rain
# reader reports no coverage evidence, so there is no coverage health or attribute.
RAIN_HEALTH = ("ok", "missing", "stale", "future", "error")


@dataclass(frozen=True, slots=True)
class RadarResult:
    radar: RadarData | None
    rain: bool | None
    distance_km: float | None
    last_update: datetime | None
    health: str = "ok"
    max_age_minutes: float = DEFAULT_RAIN_MAX_AGE

    def age_minutes(self, now: datetime) -> float | None:
        if self.last_update is None or self.last_update > now:
            return None
        return (now - self.last_update).total_seconds() / 60

    def at(self, now: datetime) -> RadarResult:
        """Recheck freshness on reads; a cached observation expires on its own."""
        if self.health in ("missing", "error"):
            return self  # A failed or absent update cannot certify cached weather.
        if self.last_update is None:
            health = "missing"
        elif self.last_update > now:
            health = "future"
        elif (age := self.age_minutes(now)) is not None and age > self.max_age_minutes:
            health = "stale"
        else:
            return self
        return RadarResult(
            None, None, None, self.last_update, health, self.max_age_minutes
        )
