from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_RADIUS, CONF_THRESHOLD, DOMAIN
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
        self.result_data: RadarResult | None = None
        self._remove_listener = None
        self._stopped = False

    def start(self):
        self._schedule_next_update()

    async def stop(self):
        self._stopped = True
        await self.downloader.close()
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    def _schedule_next_update(
        self,
        retry: bool = False,
    ):
        if self._stopped:
            return

        if self._remove_listener:
            self._remove_listener()

        now = datetime.now(UTC)

        if retry:
            when = now + timedelta(seconds=15)

        else:
            minute = ((now.minute // 5) + 1) * 5
            if minute == 60:
                when = now.replace(
                    minute=0,
                    second=50,
                    microsecond=0,
                ) + timedelta(hours=1)
            else:
                when = now.replace(
                    minute=minute,
                    second=50,
                    microsecond=0,
                )

        _LOGGER.debug(
            "Next radar update: %s",
            when,
        )

        self._remove_listener = async_track_point_in_utc_time(
            self.hass,
            self._scheduled_refresh,
            when,
        )

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

    async def _async_update_data(
        self,
    ) -> RadarResult:
        timestamp = self._expected_timestamp()
        if not await self.downloader.radar_exists(
            timestamp,
        ):
            self._schedule_next_update(retry=True)

            if self.result_data is not None:
                return self.result_data

            raise UpdateFailed(
                "Radar data for "
                f"{self.downloader.build_url(timestamp)[1]} not yet available, "
                "retrying in 15 seconds."
            )
        radar_bytes = await self.downloader.fetch_radar(timestamp)

        radar_data = RadarData.from_bytes(
            radar_bytes,
            threshold=self.entry.options.get(
                CONF_THRESHOLD,
                self.entry.data[CONF_THRESHOLD],
            ),
        )

        rain, distance = self.detector.detect(
            radar_data,
            latitude=self.entry.data["latitude"],
            longitude=self.entry.data["longitude"],
            radius_km=self.entry.options.get(
                CONF_RADIUS,
                self.entry.data[CONF_RADIUS],
            ),
        )
        self._schedule_next_update()
        self.result_data = RadarResult(
            radar=radar_data,
            rain=rain,
            distance_km=distance,
            last_update=timestamp,
        )
        return self.result_data
