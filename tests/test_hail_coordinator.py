"""Reporting freshness and per-entry isolation with real HA coordinator plumbing."""

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.meteoswiss_rain_radar.const import (
    CONF_HAIL_MAX_AGE,
    CONF_HAIL_POLL,
    CONF_HAIL_RADIUS,
    CONF_HAIL_THRESHOLD,
    DOMAIN,
)
from custom_components.meteoswiss_rain_radar.hail_coordinator import (
    HailResult,
    MeteoSwissHailCoordinator,
)
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis, read_hail
from custom_components.meteoswiss_rain_radar.stac_downloader import (
    Discovery,
    StacAsset,
    StacDownloader,
)

from .hail_helpers import OBSERVATION, hail_bytes

MODULE = "custom_components.meteoswiss_rain_radar.hail_coordinator"


def make_coordinator(hass, *, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"radius": 5, "threshold": 0.2, "latitude": 1, "longitude": 1},
        options=options or {},
        version=1,
    )
    entry.add_to_hass(hass)
    coordinator = MeteoSwissHailCoordinator(hass, entry)
    coordinator.downloader = AsyncMock(spec=StacDownloader)
    content = hail_bytes(np.full((7, 7), 0.8))
    asset = StacAsset(
        "https://data.geo.admin.ch/test.h5",
        hashlib.sha256(content).hexdigest(),
        OBSERVATION,
    )
    coordinator.downloader.discover.return_value = Discovery(asset, "ok", OBSERVATION)
    coordinator.downloader.fetch.return_value = content
    return coordinator


@pytest.fixture
async def coordinator(hass, freezer):
    freezer.move_to(OBSERVATION + timedelta(minutes=1))
    hass.config.latitude = hass.config.longitude = 0
    coordinator = make_coordinator(hass, options={CONF_HAIL_RADIUS: 1})
    yield coordinator
    await coordinator.stop()


async def test_executor_current_hass_coordinates_and_immediate_inclusive_detection(
    coordinator,
):
    thread_id = threading.get_ident()
    calls = []

    def worker(*args):
        assert threading.get_ident() != thread_id
        calls.append(args)
        return read_hail(*args)

    # HA's test helper executes Mock targets inline; use a real worker function.
    with patch(f"{MODULE}.read_hail", new=worker):
        await coordinator.async_refresh()
    assert coordinator.data.analysis.detected is True  # No eight-minute wait.
    assert coordinator.data.analysis.max_poh == 80
    assert coordinator.data.analysis.health == "ok"
    assert len(calls) == 1
    assert calls[0][2:] == (0, 0, 1, 80)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_cached_observation_recomputes_age_and_stales_without_download(
    coordinator, freezer
):
    with patch(f"{MODULE}.read_hail", wraps=read_hail) as reader:
        await coordinator.async_refresh()
        freezer.move_to(OBSERVATION + timedelta(minutes=8))
        await coordinator.async_refresh()
        assert coordinator.current_result.age_minutes(datetime.now(UTC)) == 8
        assert coordinator.current_result.analysis.detected is True
        assert reader.call_count == 1
        freezer.move_to(OBSERVATION + timedelta(minutes=10, microseconds=1))
        await coordinator.async_refresh()
    assert coordinator.current_result.analysis == HailAnalysis("stale")
    assert coordinator.downloader.fetch.await_count == 1


async def test_freshness_deadline_updates_entities_without_waiting_for_poll(
    coordinator, hass, freezer
):
    await coordinator.async_refresh()
    freezer.move_to(OBSERVATION + timedelta(minutes=10, seconds=1))
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    assert coordinator.data.analysis.health == "stale"
    assert coordinator.data.analysis.detected is None


async def test_error_cannot_reuse_fresh_clear_or_positive_data(coordinator):
    await coordinator.async_refresh()
    coordinator.downloader.discover.side_effect = httpx.ReadTimeout("timeout")
    await coordinator.async_refresh()
    assert coordinator.last_update_success  # Health remains readable.
    assert coordinator.data.analysis == HailAnalysis("error")
    assert coordinator.data.observation == OBSERVATION
    coordinator.downloader.discover.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.data.analysis.detected is True
    assert coordinator.downloader.fetch.await_count == 1


@pytest.mark.parametrize(
    "health,observation",
    [("missing", None), ("future", OBSERVATION + timedelta(minutes=5))],
)
async def test_missing_future_never_false_or_zero(coordinator, health, observation):
    coordinator.downloader.discover.return_value = Discovery(None, health, observation)
    await coordinator.async_refresh()
    assert coordinator.data.analysis == HailAnalysis(health)
    assert coordinator.data.age_minutes(OBSERVATION) is None
    coordinator.downloader.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "now",
    [datetime(2026, 3, 31, 23, 59, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)],
)
async def test_off_season_skips_downloads(coordinator, freezer, now):
    freezer.move_to(now)
    await coordinator.async_refresh()
    assert coordinator.data.analysis == HailAnalysis("off_season")
    coordinator.downloader.discover.assert_not_awaited()


@pytest.mark.parametrize("content,health", [(b"", "empty"), (b"broken", "error")])
async def test_empty_and_malformed_files(coordinator, content, health):
    coordinator.downloader.fetch.return_value = content
    await coordinator.async_refresh()
    assert coordinator.data.analysis == HailAnalysis(health)


async def test_changed_home_reanalyses_cached_bytes_and_entry_isolation(
    coordinator, hass
):
    other = make_coordinator(
        hass,
        options={
            CONF_HAIL_RADIUS: 1,
            CONF_HAIL_THRESHOLD: 90,
            CONF_HAIL_POLL: 120,
            CONF_HAIL_MAX_AGE: 5,
        },
    )
    try:
        await coordinator.async_refresh()
        await other.async_refresh()
        assert coordinator.data.analysis.detected is True
        assert other.data.analysis.detected is False
        assert other.update_interval == timedelta(seconds=120)
        assert other.max_age_minutes == 5
        assert coordinator.downloader is not other.downloader
        assert coordinator._analysis is not other._analysis
        hass.config.latitude = hass.config.longitude = 1
        await coordinator.async_refresh()
        assert coordinator.data.analysis.health == "outside_grid"
        assert other.data.analysis.health == "ok"
    finally:
        await other.stop()


async def test_same_timestamp_correction_reanalysed(coordinator):
    await coordinator.async_refresh()
    changed = hail_bytes()
    old = coordinator.downloader.discover.return_value.asset
    asset = StacAsset(old.url, hashlib.sha256(changed).hexdigest(), OBSERVATION)
    coordinator.downloader.discover.return_value = Discovery(asset, "ok", OBSERVATION)
    coordinator.downloader.fetch.return_value = changed
    await coordinator.async_refresh()
    assert coordinator.data.analysis.detected is False
    assert coordinator.data.observation == OBSERVATION
    assert coordinator.downloader.fetch.await_count == 2


async def test_shutdown_cancels_inflight_request_and_timer(coordinator, hass):
    await coordinator.async_refresh()
    entered = asyncio.Event()

    async def blocked(_now):
        entered.set()
        await asyncio.Event().wait()

    coordinator.downloader.discover.side_effect = blocked
    task = hass.async_create_task(coordinator.async_refresh())
    await entered.wait()
    await coordinator.stop()
    assert task.cancelled()
    assert coordinator._cancel_expiry is None
    assert coordinator._analysis is None
    coordinator.downloader.close.assert_awaited_once()


def test_clock_reversal_age_boundaries_and_offseason_observation():
    result = HailResult(OBSERVATION, HailAnalysis("ok", True, 80, 1, True), 10)
    assert result.at(OBSERVATION + timedelta(minutes=10)).analysis.detected is True
    assert result.at(
        OBSERVATION + timedelta(minutes=10, microseconds=1)
    ).analysis == HailAnalysis("stale")
    assert result.at(OBSERVATION - timedelta(seconds=1)).analysis == HailAnalysis(
        "future"
    )
    march = datetime(2026, 3, 31, 23, 55, tzinfo=UTC)
    result = HailResult(march, HailAnalysis("ok", False, 0, None, True), 10)
    assert result.at(march + timedelta(minutes=5)).analysis == HailAnalysis(
        "off_season"
    )


@pytest.mark.parametrize("last_success", [True, False])
async def test_expiry_preserves_actual_poll_deadline_and_error_state(
    coordinator, hass, freezer, last_success
):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from pytest_homeassistant_custom_component.common import (
        async_fire_time_changed_exact,
    )

    freezer.move_to(OBSERVATION + timedelta(minutes=9, seconds=45))
    notifications = []
    remove = coordinator.async_add_listener(
        lambda: notifications.append(coordinator.data)
    )
    try:
        await coordinator.async_refresh()
        cancel = coordinator._unsub_refresh
        timer = cancel.__self__
        deadline = timer.when()
        poll_time = datetime.now(UTC) + timedelta(seconds=deadline - hass.loop.time())
        error = UpdateFailed("earlier update failed")
        coordinator.last_update_success = last_success
        coordinator.last_exception = error
        calls = coordinator.downloader.discover.await_count

        freezer.move_to(OBSERVATION + timedelta(minutes=10))
        async_fire_time_changed_exact(hass, datetime.now(UTC))
        await hass.async_block_till_done()
        assert coordinator.data.analysis.detected is True
        freezer.move_to(OBSERVATION + timedelta(minutes=10, microseconds=1))
        async_fire_time_changed_exact(hass, datetime.now(UTC))
        await hass.async_block_till_done()
        assert coordinator.data.analysis == HailAnalysis("stale")
        assert notifications[-1].analysis.detected is None
        assert coordinator._unsub_refresh is cancel
        assert timer.when() == deadline and not timer.cancelled()
        assert coordinator.downloader.discover.await_count == calls
        assert coordinator.last_update_success is last_success
        assert coordinator.last_exception is error

        freezer.move_to(poll_time + timedelta(microseconds=1))
        async_fire_time_changed_exact(hass, datetime.now(UTC))
        await hass.async_block_till_done()
        assert coordinator.downloader.discover.await_count == calls + 1
    finally:
        remove()
