from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
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
from .radar_downloader import create_radar_downloader
from .stac_downloader import StacAsset

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
        self.downloader = create_radar_downloader(
            get_async_client(hass), async_add_executor_job=hass.async_add_executor_job
        )
        self.detector = RainDetector()
        self.max_age_minutes = self._setting(CONF_RAIN_MAX_AGE, DEFAULT_RAIN_MAX_AGE)
        self.poll_seconds = self._setting(CONF_RAIN_POLL, DEFAULT_RAIN_POLL)
        self.result_data: RadarResult | None = None
        self._frame_key: tuple | None = None
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

    def _read_radar(
        self,
        content: bytes,
        threshold: float,
        latitude: float,
        longitude: float,
        radius_km: float,
    ):
        """Decode and search the grid in the executor, off the event loop."""
        radar_data = RadarData.from_bytes(content, threshold=threshold)
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
        """Read the published asset from the catalogue; never guess a filename."""
        cached = self.result_data
        try:
            if self._stopped:
                return self._no_new_frame(cached)  # Never start work after stop().
            discovery = await self.downloader.discover(datetime.now(UTC))
            found = RadarResult(
                None,
                None,
                None,
                discovery.observation,
                discovery.health,
                self.max_age_minutes,
            ).at(datetime.now(UTC))
            if self._stopped or discovery.asset is None or found.health != "ok":
                return self._no_new_frame(cached, found)
            if (
                cached is not None
                and cached.last_update is not None
                and discovery.asset.observation < cached.last_update
            ):
                return self._no_new_frame(cached)
            settings = (
                self._setting(CONF_THRESHOLD, DEFAULT_THRESHOLD),
                self.entry.data["latitude"],
                self.entry.data["longitude"],
                self._setting(CONF_RADIUS, DEFAULT_RADIUS),
            )
            key = (*discovery.asset.identity, *settings)
            if key == self._frame_key and cached is not None and not self._failed:
                return cached.at(datetime.now(UTC))  # Only its age changes.
            return await self._download(discovery.asset, settings, key)
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

    def _no_new_frame(
        self, cached: RadarResult | None, found: RadarResult | None = None
    ) -> RadarResult:
        """An absent frame is no evidence; only a download clears a failure."""
        if self._failed:
            return self._unusable("error")
        if cached is not None:
            return cached.at(datetime.now(UTC))
        if found is not None:
            return found
        return self._unusable("missing")

    async def _download(
        self, asset: StacAsset, settings: tuple, key: tuple
    ) -> RadarResult:
        """Fetch and read one five-minute frame, keeping its source timestamp."""
        content = await self.downloader.fetch(asset, force=self._failed)
        radar_data, rain, distance = await self.hass.async_add_executor_job(
            self._read_radar, content, *settings
        )
        self._failed = False
        self._frame_key = key
        self.result_data = RadarResult(
            radar=radar_data,
            rain=rain,
            distance_km=distance,
            last_update=asset.observation,
            health="ok",
            max_age_minutes=self.max_age_minutes,
        )
        return self.result_data.at(datetime.now(UTC))
