"""Independent per-entry hail reporting, with no protective release state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyproj.exceptions import ProjError

from .const import (
    CONF_HAIL_MAX_AGE,
    CONF_HAIL_POLL,
    CONF_HAIL_RADIUS,
    CONF_HAIL_THRESHOLD,
    DEFAULT_HAIL_MAX_AGE,
    DEFAULT_HAIL_POLL,
    DEFAULT_HAIL_RADIUS,
    DEFAULT_HAIL_THRESHOLD,
    DOMAIN,
)
from .hail_downloader import HailDownloader
from .hail_reader import HailAnalysis, read_hail

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HailResult:
    observation: datetime | None
    analysis: HailAnalysis
    max_age_minutes: float

    def age_minutes(self, now: datetime) -> float | None:
        if self.observation is None or self.observation > now:
            return None
        return (now - self.observation).total_seconds() / 60

    def at(self, now: datetime) -> HailResult:
        """Recheck freshness even when the downloaded file has not changed."""
        health = None
        if self.observation is not None and self.observation > now:
            health = "future"
        elif not 4 <= now.month <= 9 or (
            self.observation is not None and not 4 <= self.observation.month <= 9
        ):
            health = "off_season"
        elif self.analysis.health == "error":
            return self  # A failed discovery cannot certify even a fresh cached value.
        elif (age := self.age_minutes(now)) is not None and age > self.max_age_minutes:
            health = "stale"
        if health:
            return HailResult(
                self.observation, HailAnalysis(health), self.max_age_minutes
            )
        return self


class MeteoSwissHailCoordinator(DataUpdateCoordinator[HailResult]):
    data: HailResult

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.entry = entry
        self.poll_seconds = entry.options.get(CONF_HAIL_POLL, DEFAULT_HAIL_POLL)
        self.max_age_minutes = entry.options.get(
            CONF_HAIL_MAX_AGE, DEFAULT_HAIL_MAX_AGE
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_hail_{entry.entry_id}",
            update_interval=timedelta(seconds=self.poll_seconds),
        )
        self.downloader = HailDownloader(
            get_async_client(hass), async_add_executor_job=hass.async_add_executor_job
        )
        self._analysis_key: tuple | None = None
        self._analysis: HailAnalysis | None = None
        self._stopped = False
        self._update_task: asyncio.Task | None = None
        self._cancel_expiry = None

    @property
    def current_result(self) -> HailResult:
        if not self.last_update_success:
            return HailResult(
                self.data.observation if self.data else None,
                HailAnalysis("error"),
                self.max_age_minutes,
            )
        if self.data is None:
            return HailResult(None, HailAnalysis("missing"), self.max_age_minutes)
        return self.data.at(datetime.now(UTC))

    async def stop(self):
        self._stopped = True
        if self._cancel_expiry:
            self._cancel_expiry()
            self._cancel_expiry = None
        await self.async_shutdown()
        if self._update_task and self._update_task is not asyncio.current_task():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        await self.downloader.close()
        self._analysis_key = None
        self._analysis = None

    async def _async_update_data(self) -> HailResult:
        self._update_task = asyncio.current_task()
        try:
            result = await self._update_hail()
            if self._cancel_expiry:
                self._cancel_expiry()
                self._cancel_expiry = None
            if result.observation and result.analysis.health in (
                "ok",
                "partial_coverage",
            ):
                expires = result.observation + timedelta(
                    minutes=self.max_age_minutes, microseconds=1
                )
                self._cancel_expiry = async_track_point_in_utc_time(
                    self.hass, self._expire_observation, expires
                )
            return result
        finally:
            self._update_task = None

    @callback
    def _expire_observation(self, now: datetime) -> None:
        self._cancel_expiry = None
        if not self._stopped and self.data:
            self.data = self.data.at(now)
            self.async_update_listeners()

    async def _update_hail(self) -> HailResult:
        now = datetime.now(UTC)
        if self._stopped:
            return HailResult(None, HailAnalysis("missing"), self.max_age_minutes)
        if not 4 <= now.month <= 9:
            return HailResult(None, HailAnalysis("off_season"), self.max_age_minutes)
        observation = self.data.observation if self.data else None
        try:
            discovery = await self.downloader.discover(now)
            observation = discovery.observation
            result = HailResult(
                observation, HailAnalysis(discovery.health), self.max_age_minutes
            ).at(datetime.now(UTC))
            if discovery.asset is None or result.analysis.health != "ok":
                return result
            asset = discovery.asset
            settings = (
                self.hass.config.latitude,
                self.hass.config.longitude,
                self.entry.options.get(CONF_HAIL_RADIUS, DEFAULT_HAIL_RADIUS),
                self.entry.options.get(CONF_HAIL_THRESHOLD, DEFAULT_HAIL_THRESHOLD),
            )
            key = (*asset.identity, *settings)
            if key != self._analysis_key:
                content = await self.downloader.fetch(asset)
                analysis = await self.hass.async_add_executor_job(
                    read_hail, content, asset.observation, *settings
                )
                self._analysis_key, self._analysis = key, analysis
            assert self._analysis is not None
            return HailResult(observation, self._analysis, self.max_age_minutes).at(
                datetime.now(UTC)
            )
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
            ProjError,
        ) as err:
            _LOGGER.warning(
                "Hail update failed for entry %s: %s", self.entry.entry_id, err
            )
            return HailResult(observation, HailAnalysis("error"), self.max_age_minutes)
