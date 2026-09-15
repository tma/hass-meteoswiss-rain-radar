"""Tests for MeteoSwissRainRadarCoordinator.

Requires: pip install pytest pytest-asyncio pytest-freezer
          pytest-homeassistant-custom-component

Adjust the import below to match your actual module path, e.g.:
    from custom_components.meteoswiss_rain_radar.coordinator import (
        MeteoSwissRainRadarCoordinator,
    )
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_rain_radar.const import (
    CONF_RADIUS,
    CONF_THRESHOLD,
    DOMAIN,
)
from custom_components.meteoswiss_rain_radar.coordinator import (
    MeteoSwissRainRadarCoordinator,
)
from custom_components.meteoswiss_rain_radar.stac_downloader import (
    Discovery,
    StacAsset,
)

MODULE = "custom_components.meteoswiss_rain_radar.coordinator"


def published_now() -> Discovery:
    """A listed asset for the current five-minute slot, as the catalogue gives it."""
    now = datetime.now(UTC)
    observation = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    asset = StacAsset(
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        f"{observation:%Y%m%d}-ch/{observation:rzc%y%j%H%M}ul.001.h5",
        "b" * 64,
        observation,
    )
    return Discovery(asset, "ok", observation)


def make_entry(**data_overrides) -> MockConfigEntry:
    data = {
        CONF_THRESHOLD: 0.2,
        CONF_RADIUS: 10,
        "latitude": 47.0,
        "longitude": 7.5,
    }
    data.update(data_overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, options={})


@pytest.fixture
async def coordinator(hass):
    entry = make_entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{MODULE}.create_radar_downloader"),
        patch(f"{MODULE}.RainDetector"),
    ):
        coord = MeteoSwissRainRadarCoordinator(hass, entry)
    # Replace with fresh AsyncMocks so we can assert on individual tests.
    coord.downloader = MagicMock()
    coord.downloader.close = AsyncMock()
    coord.downloader.discover = AsyncMock(return_value=Discovery(None))
    coord.downloader.fetch = AsyncMock(return_value=b"raw-bytes")
    coord.detector = MagicMock()
    yield coord
    await coord.stop()


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init_sets_up_attributes(hass):
    entry = make_entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{MODULE}.create_radar_downloader"),
        patch(f"{MODULE}.RainDetector"),
    ):
        coord = MeteoSwissRainRadarCoordinator(hass, entry)

    assert coord.entry is entry
    assert coord.name == DOMAIN
    assert coord._remove_listener is None
    assert coord._cancel_expiry is None
    assert coord.poll_seconds == 60 and coord.max_age_minutes == 10


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


def test_start_schedules_next_update(coordinator):
    with patch.object(coordinator, "_schedule_next_update") as mock_schedule:
        coordinator.start()

    mock_schedule.assert_called_once_with()


@pytest.mark.asyncio
async def test_stop_closes_downloader_and_removes_listener(coordinator):
    remove_listener = MagicMock()
    coordinator._remove_listener = remove_listener

    await coordinator.stop()

    coordinator.downloader.close.assert_awaited_once()
    remove_listener.assert_called_once()
    assert coordinator._remove_listener is None


@pytest.mark.asyncio
async def test_stop_without_listener_does_not_raise(coordinator):
    coordinator._remove_listener = None

    await coordinator.stop()

    coordinator.downloader.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# _schedule_next_update
# ---------------------------------------------------------------------------


def test_schedule_next_update_uses_configured_poll_interval(coordinator, freezer):
    now = datetime(2024, 1, 1, 10, 7, 30, tzinfo=UTC)
    freezer.move_to(now)

    with patch(f"{MODULE}.async_track_point_in_utc_time") as mock_track:
        coordinator._schedule_next_update()

    expected_when = now + timedelta(seconds=60)
    mock_track.assert_called_once_with(
        coordinator.hass, coordinator._scheduled_refresh, expected_when
    )
    assert coordinator._remove_listener is mock_track.return_value


def test_schedule_next_update_honours_changed_poll_option(coordinator, freezer):
    now = datetime(2024, 1, 1, 10, 7, 30, tzinfo=UTC)
    freezer.move_to(now)
    coordinator.poll_seconds = 300

    with patch(f"{MODULE}.async_track_point_in_utc_time") as mock_track:
        coordinator._schedule_next_update()

    assert mock_track.call_args.args[2] == now + timedelta(seconds=300)


def test_schedule_next_update_does_nothing_after_stop(coordinator, freezer):
    freezer.move_to(datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC))
    coordinator._stopped = True

    with patch(f"{MODULE}.async_track_point_in_utc_time") as mock_track:
        coordinator._schedule_next_update()

    mock_track.assert_not_called()
    assert coordinator._remove_listener is None


def test_schedule_next_update_cancels_existing_listener(coordinator, freezer):
    freezer.move_to(datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC))
    old_listener = MagicMock()
    coordinator._remove_listener = old_listener

    with patch(f"{MODULE}.async_track_point_in_utc_time"):
        coordinator._schedule_next_update()

    old_listener.assert_called_once()


# ---------------------------------------------------------------------------
# _scheduled_refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_refresh_calls_async_refresh(coordinator):
    with patch.object(
        coordinator, "async_refresh", new_callable=AsyncMock
    ) as mock_refresh:
        await coordinator._scheduled_refresh(datetime.now(UTC))

    mock_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# _async_update_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_update_data_reports_missing_when_radar_not_yet_available(
    coordinator,
):
    coordinator.downloader.discover.return_value = Discovery(None)

    with patch.object(coordinator, "_schedule_next_update") as mock_schedule:
        result = await coordinator._async_update_data()

    assert result.health == "missing"
    assert result.rain is None and result.distance_km is None
    mock_schedule.assert_called_once_with()
    coordinator.downloader.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_update_data_success_uses_options_over_data(coordinator):
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry, options={CONF_THRESHOLD: 0.5, CONF_RADIUS: 20}
    )
    coordinator.downloader.discover.return_value = published_now()

    fake_radar_data = MagicMock(name="RadarData")
    coordinator.detector.detect.return_value = (True, 3.5)

    with (
        patch.object(coordinator, "_schedule_next_update") as mock_schedule,
        patch.object(coordinator, "_schedule_expiry"),
        patch(f"{MODULE}.RadarData") as mock_radar_data_cls,
    ):
        mock_radar_data_cls.from_bytes.return_value = fake_radar_data

        result = await coordinator._async_update_data()

    mock_radar_data_cls.from_bytes.assert_called_once_with(b"raw-bytes", threshold=0.5)
    coordinator.detector.detect.assert_called_once_with(
        fake_radar_data,
        latitude=47.0,
        longitude=7.5,
        radius_km=20,
    )
    mock_schedule.assert_called_once_with()

    assert result.radar is fake_radar_data
    assert result.rain is True
    assert result.distance_km == 3.5
    assert result.health == "ok"


@pytest.mark.asyncio
async def test_async_update_data_success_falls_back_to_entry_data(coordinator):
    # options empty -> should fall back to entry.data values
    coordinator.hass.config_entries.async_update_entry(coordinator.entry, options={})
    coordinator.downloader.discover.return_value = published_now()
    coordinator.detector.detect.return_value = (False, None)

    with (
        patch.object(coordinator, "_schedule_next_update"),
        patch.object(coordinator, "_schedule_expiry"),
        patch(f"{MODULE}.RadarData") as mock_radar_data_cls,
    ):
        mock_radar_data_cls.from_bytes.return_value = MagicMock()

        await coordinator._async_update_data()

    mock_radar_data_cls.from_bytes.assert_called_once_with(b"raw-bytes", threshold=0.2)
    coordinator.detector.detect.assert_called_once_with(
        mock_radar_data_cls.from_bytes.return_value,
        latitude=47.0,
        longitude=7.5,
        radius_km=10,
    )
