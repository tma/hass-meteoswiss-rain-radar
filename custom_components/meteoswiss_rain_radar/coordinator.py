from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_RADIUS,
    CONF_RAIN_MAX_AGE,
    CONF_RAIN_POLL,
    CONF_THRESHOLD,
    DEFAULT_RADIUS,
    DEFAULT_RAIN_MAX_AGE,
    DEFAULT_RAIN_POLL,
    DEFAULT_THRESHOLD,
    DOMAIN,
)
from .detector import RainDetector
from .models import RadarResult
from .radar import RadarData
from .radar_downloader import RadarDownloader

if TYPE_CHECKING:
    from .hail_coordinator import MeteoSwissHailCoordinator

_LOGGER = logging.getLogger(__name__)


class MeteoSwissRainRadarCoordinator(DataUpdateCoordinator[RadarResult]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ):
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
        )
        self.entry = entry
        self.hail_coordinator: MeteoSwissHailCoordinator | None = None
        self.downloader = RadarDownloader(get_async_client(hass))
        self.detector = RainDetector()
        self.max_age_minutes = self._setting(CONF_RAIN_MAX_AGE, DEFAULT_RAIN_MAX_AGE)
        self.poll_seconds = self._setting(CONF_RAIN_POLL, DEFAULT_RAIN_POLL)
        self.result_data: RadarResult | None = None
        self._failed = False
        self._remove_listener = None
        self._cancel_expiry = None
        self._stopped = False

    def _setting(self, key: str, default: float) -> float:
        """Options win; initial setup stores the rain settings in the entry data."""
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def last_observation(self) -> datetime | None:
        """Keep the downloaded observation time readable through outages."""
        if self.result_data is not None:
            return self.result_data.last_update
        return self.data.last_update if self.data else None

    @property
    def current_result(self) -> RadarResult:
        """Re-evaluate freshness on every read, not only after an update."""
        if not self.last_update_success:
            return self._unusable("error")
        if self.data is None:
            return self._unusable("missing")
        return self.data.at(datetime.now(UTC))

    def start(self):
        self._schedule_next_update()

    async def stop(self):
        self._stopped = True
        self._cancel_timers()
        await self.downloader.close()

    @callback
    def _cancel_timers(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
        if self._cancel_expiry:
            self._cancel_expiry()
            self._cancel_expiry = None

    def _unusable(self, health: str) -> RadarResult:
        """No weather values, but keep the observation time for age reporting."""
        return RadarResult(
            None, None, None, self.last_observation, health, self.max_age_minutes
        )

    def _schedule_next_update(self):
        """Every check uses the configured interval, published frame or not."""
        if self._stopped:
            return

        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

        when = datetime.now(UTC) + timedelta(seconds=self.poll_seconds)

        _LOGGER.debug(
            "Next radar update: %s",
            when,
        )

        self._remove_listener = async_track_point_in_utc_time(
            self.hass,
            self._scheduled_refresh,
            when,
        )

    def _schedule_expiry(self, result: RadarResult | None) -> None:
        """Publish the expiry of a usable observation without another download."""
        if self._cancel_expiry:
            self._cancel_expiry()
            self._cancel_expiry = None
        if self._stopped or result is None or result.health != "ok":
            return
        if result.last_update is None:
            return
        expires = result.last_update + timedelta(
            minutes=self.max_age_minutes, microseconds=1
        )
        self._cancel_expiry = async_track_point_in_utc_time(
            self.hass, self._expire_observation, expires
        )

    @callback
    def _expire_observation(self, now: datetime) -> None:
        self._cancel_expiry = None
        if not self._stopped and self.data:
            self.data = self.data.at(now)
            self.async_update_listeners()

    async def _scheduled_refresh(
        self,
        _now,
    ):
        if self._stopped:
            return
        await self.async_refresh()

    def _expected_timestamp(self):
        now = datetime.now(UTC)

        minute = (now.minute // 5) * 5

        return now.replace(
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=UTC,
        )

    def _read_radar(
        self,
        radar_bytes: BytesIO,
        threshold: float,
        latitude: float,
        longitude: float,
        radius_km: float,
    ):
        """Decode and search the grid in the executor, off the event loop."""
        radar_data = RadarData.from_bytes(radar_bytes, threshold=threshold)
        rain, distance = self.detector.detect(
            radar_data,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
        )
        return radar_data, rain, distance

    async def _async_update_data(
        self,
    ) -> RadarResult:
        """Report an outage as health instead of failing, so polling continues."""
        try:
            result = await self._update_radar()
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unexpected failure latches too; only a download can clear it.
            self._failed = True
            self._schedule_next_update()
            raise
        self._schedule_next_update()
        self._schedule_expiry(result)
        return result

    async def _update_radar(self) -> RadarResult:
        """Try the current frame, then one bounded step back, then the cache."""
        now = datetime.now(UTC)
        cached = self.result_data
        current = self._expected_timestamp()
        try:
            for timestamp in (current, current - timedelta(minutes=5)):
                if self._stopped:
                    break  # Unloading: never start new work after stop().
                if now - timestamp > timedelta(minutes=self.max_age_minutes):
                    break  # Too old to be usable even if it is published.
                if cached is not None:
                    if timestamp < cached.last_update:
                        break  # Nothing newer to download.
                    if timestamp == cached.last_update and not self._failed:
                        break  # Already downloaded; only its age changes.
                published = await self.downloader.radar_exists(timestamp)
                if self._stopped:
                    break
                if not published:
                    continue  # Not published yet; the source writes around :50.
                return await self._download(timestamp)
        except (
            httpx.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
        ) as err:
            _LOGGER.warning(
                "Rain update failed for entry %s: %s", self.entry.entry_id, err
            )
            self._failed = True
            return self._unusable("error")
        if self._failed:
            # An unpublished frame is no evidence; only a download clears a failure.
            return self._unusable("error")
        if cached is None:
            return self._unusable("missing")
        return cached.at(datetime.now(UTC))

    async def _download(self, timestamp: datetime) -> RadarResult:
        """Fetch and read one five-minute frame, keeping its source timestamp."""
        radar_bytes = await self.downloader.fetch_radar(timestamp)
        radar_data, rain, distance = await self.hass.async_add_executor_job(
            self._read_radar,
            radar_bytes,
            self._setting(CONF_THRESHOLD, DEFAULT_THRESHOLD),
            self.entry.data["latitude"],
            self.entry.data["longitude"],
            self._setting(CONF_RADIUS, DEFAULT_RADIUS),
        )
        self._failed = False
        self.result_data = RadarResult(
            radar=radar_data,
            rain=rain,
            distance_km=distance,
            last_update=timestamp,
            health="ok",
            max_age_minutes=self.max_age_minutes,
        )
        return self.result_data.at(datetime.now(UTC))
