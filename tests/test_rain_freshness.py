"""Rain freshness, health and polling with real HA coordinator plumbing."""

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.meteoswiss_rain_radar.const import (
    CONF_RADIUS,
    CONF_RAIN_MAX_AGE,
    CONF_RAIN_POLL,
    CONF_THRESHOLD,
    DOMAIN,
)
from custom_components.meteoswiss_rain_radar.coordinator import (
    MeteoSwissRainRadarCoordinator,
)
from custom_components.meteoswiss_rain_radar.hail_coordinator import HailResult
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis
from custom_components.meteoswiss_rain_radar.models import RadarResult
from custom_components.meteoswiss_rain_radar.radar_downloader import RadarDownloader

from .hail_helpers import OBSERVATION
from .test_hail_setup import make_entry

MODULE = "custom_components.meteoswiss_rain_radar.coordinator"
SETUP_MODULE = "custom_components.meteoswiss_rain_radar"
# The published five-minute frame is normally readable just after its slot.
PUBLISHED = OBSERVATION + timedelta(seconds=50)


def make_coordinator(hass, *, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RADIUS: 5, CONF_THRESHOLD: 0.2, "latitude": 47.0, "longitude": 7.5},
        options=options or {},
        version=1,
    )
    entry.add_to_hass(hass)
    coordinator = MeteoSwissRainRadarCoordinator(hass, entry)
    coordinator.downloader = MagicMock()
    coordinator.downloader.close = AsyncMock()
    coordinator.downloader.radar_exists = AsyncMock(return_value=True)
    coordinator.downloader.fetch_radar = AsyncMock(return_value=BytesIO(b"radar"))
    coordinator.downloader.build_url = MagicMock(
        return_value=("rzc.h5", "https://example.invalid/rzc.h5")
    )
    coordinator.detector = MagicMock()
    coordinator.detector.detect.return_value = (True, 2.5)
    return coordinator


@pytest.fixture
async def coordinator(hass, freezer):
    freezer.move_to(PUBLISHED)
    with patch(f"{MODULE}.RadarData") as radar_data:
        radar_data.from_bytes.return_value = MagicMock(name="RadarData")
        coordinator = make_coordinator(hass)
        yield coordinator
        await coordinator.stop()


async def test_fresh_download_reports_ok_with_the_source_observation(coordinator):
    await coordinator.async_refresh()

    result = coordinator.current_result
    assert result.health == "ok"
    assert result.rain is True and result.distance_km == 2.5
    assert result.last_update == OBSERVATION
    assert result.age_minutes(datetime.now(UTC)) == pytest.approx(50 / 60)
    assert coordinator.last_update_success


async def test_cached_frame_is_not_redownloaded_and_keeps_its_timestamp(
    coordinator, freezer
):
    await coordinator.async_refresh()
    freezer.move_to(OBSERVATION + timedelta(minutes=3))
    await coordinator.async_refresh()

    assert coordinator.downloader.radar_exists.await_count == 1
    assert coordinator.downloader.fetch_radar.await_count == 1
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.data.health == "ok"
    assert coordinator.current_result.age_minutes(datetime.now(UTC)) == 3


async def test_unpublished_next_frame_keeps_the_fresh_cache_then_expires(
    coordinator, freezer
):
    await coordinator.async_refresh()
    coordinator.downloader.radar_exists.return_value = False

    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=50))
    await coordinator.async_refresh()
    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == OBSERVATION  # Never renewed by a 404.
    assert coordinator.data.rain is True

    freezer.move_to(OBSERVATION + timedelta(minutes=10, microseconds=1))
    await coordinator.async_refresh()
    assert coordinator.data.health == "stale"
    assert coordinator.data.rain is None and coordinator.data.distance_km is None
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.downloader.fetch_radar.await_count == 1


async def test_freshness_cutoff_is_inclusive_at_the_limit(coordinator, freezer):
    await coordinator.async_refresh()

    freezer.move_to(OBSERVATION + timedelta(minutes=10))
    assert coordinator.current_result.health == "ok"
    freezer.move_to(OBSERVATION + timedelta(minutes=10, microseconds=1))
    assert coordinator.current_result.health == "stale"
    assert coordinator.current_result.rain is None


async def test_expiry_timer_publishes_stale_without_any_request(
    hass, freezer, coordinator
):
    coordinator.max_age_minutes = 1
    coordinator.poll_seconds = 300
    notifications = []
    remove = coordinator.async_add_listener(
        lambda: notifications.append(coordinator.data)
    )
    try:
        await coordinator.async_refresh()
        assert coordinator._cancel_expiry is not None
        poll_timer = coordinator._remove_listener
        requests = coordinator.downloader.radar_exists.await_count

        freezer.move_to(OBSERVATION + timedelta(minutes=1, microseconds=1))
        async_fire_time_changed_exact(hass, datetime.now(UTC))
        await hass.async_block_till_done()

        assert coordinator.data.health == "stale"
        assert coordinator.data.rain is None
        assert notifications[-1].health == "stale"
        assert coordinator.downloader.radar_exists.await_count == requests
        assert coordinator._remove_listener is poll_timer  # Poll deadline unchanged.
        assert coordinator._cancel_expiry is None
    finally:
        remove()


async def test_error_is_not_cleared_by_an_unpublished_frame(coordinator, freezer):
    """Dry 10:00, timeout 10:05, 404 at 10:06: the failure must stay latched."""
    coordinator.detector.detect.return_value = (False, None)
    await coordinator.async_refresh()
    assert coordinator.data.health == "ok" and coordinator.data.rain is False

    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=10))
    coordinator.downloader.radar_exists.side_effect = httpx.ReadTimeout("timeout")
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"

    freezer.move_to(OBSERVATION + timedelta(minutes=6))
    coordinator.downloader.radar_exists.side_effect = None
    coordinator.downloader.radar_exists.return_value = False
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"  # Not a restored dry reading.
    assert coordinator.data.rain is None
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.data.age_minutes(datetime.now(UTC)) == 6
    assert coordinator.downloader.fetch_radar.await_count == 1


async def test_previous_frame_download_clears_the_error_latch(coordinator, freezer):
    """Revalidating the cached frame recovers, with its source time unchanged."""
    await coordinator.async_refresh()
    coordinator.downloader.radar_exists.side_effect = httpx.ConnectError("down")
    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=10))
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"

    published = {OBSERVATION}

    async def exists(timestamp):
        return timestamp in published

    coordinator.downloader.radar_exists.side_effect = exists
    freezer.move_to(OBSERVATION + timedelta(minutes=6))
    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == OBSERVATION  # Unchanged source time.
    assert coordinator.data.rain is True
    assert coordinator._failed is False
    assert coordinator.downloader.fetch_radar.await_count == 2


async def test_poll_before_publication_uses_the_previous_frame(hass, freezer):
    """A 300 second poll can always land before the current frame is published."""
    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=10))
    with patch(f"{MODULE}.RadarData") as radar_data:
        radar_data.from_bytes.return_value = MagicMock(name="RadarData")
        coordinator = make_coordinator(hass, options={CONF_RAIN_POLL: 300})
        published = {OBSERVATION}

        async def exists(timestamp):
            return timestamp in published

        coordinator.downloader.radar_exists.side_effect = exists
        try:
            await coordinator.async_refresh()
            assert coordinator.data.health == "ok"
            assert coordinator.data.last_update == OBSERVATION
            assert coordinator.downloader.radar_exists.await_count == 2
            assert coordinator.downloader.fetch_radar.await_count == 1

            # The next poll is again ahead of publication; take the next frame.
            published.add(OBSERVATION + timedelta(minutes=5))
            freezer.move_to(OBSERVATION + timedelta(minutes=10, seconds=10))
            await coordinator.async_refresh()
            assert coordinator.data.last_update == OBSERVATION + timedelta(minutes=5)
            assert coordinator.data.health == "ok"
            assert coordinator.downloader.fetch_radar.await_count == 2
        finally:
            await coordinator.stop()


async def test_frame_search_is_bounded_to_one_step_and_the_age_limit(hass, freezer):
    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=10))
    with patch(f"{MODULE}.RadarData"):
        coordinator = make_coordinator(hass)
        coordinator.downloader.radar_exists.return_value = False
        try:
            await coordinator.async_refresh()
            assert coordinator.data.health == "missing"
            # Current and one previous frame only; no unbounded history walk.
            assert coordinator.downloader.radar_exists.await_args_list == [
                ((OBSERVATION + timedelta(minutes=5),), {}),
                ((OBSERVATION,), {}),
            ]

            coordinator.max_age_minutes = 1
            coordinator.downloader.radar_exists.reset_mock()
            await coordinator.async_refresh()
            assert coordinator.downloader.radar_exists.await_count == 1
        finally:
            await coordinator.stop()


async def test_missing_first_update_has_no_values_and_keeps_polling(
    hass, coordinator, freezer
):
    coordinator.downloader.radar_exists.return_value = False
    await coordinator.async_refresh()

    assert coordinator.data.health == "missing"
    assert coordinator.data.rain is None and coordinator.data.distance_km is None
    assert coordinator.data.last_update is None
    assert coordinator.data.age_minutes(datetime.now(UTC)) is None
    assert coordinator.last_update_success
    assert coordinator._remove_listener is not None

    freezer.move_to(PUBLISHED + timedelta(seconds=61))
    async_fire_time_changed_exact(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    assert coordinator.downloader.radar_exists.await_count == 4  # Two frames a poll.


async def test_future_observation_is_unknown_not_rain(coordinator, freezer):
    await coordinator.async_refresh()

    freezer.move_to(OBSERVATION - timedelta(minutes=1))
    result = coordinator.current_result
    assert result.health == "future"
    assert result.rain is None and result.distance_km is None
    assert result.age_minutes(datetime.now(UTC)) is None
    assert result.last_update == OBSERVATION


@pytest.mark.parametrize(
    "failure",
    [
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("HEAD", "https://example.invalid"),
            response=httpx.Response(503),
        ),
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("timeout"),
        TimeoutError("timeout"),
    ],
)
async def test_request_failures_are_error_health_not_missing(coordinator, failure):
    coordinator.downloader.radar_exists.side_effect = failure
    await coordinator.async_refresh()

    assert coordinator.data.health == "error"
    assert coordinator.data.rain is None
    assert coordinator.last_update_success  # Health stays readable.
    coordinator.downloader.fetch_radar.assert_not_awaited()


@pytest.mark.parametrize("failure", [OSError("truncated file"), ValueError("bad grid")])
async def test_parse_failures_are_error_health(coordinator, failure):
    coordinator.detector.detect.side_effect = failure
    await coordinator.async_refresh()

    assert coordinator.data.health == "error"
    assert coordinator.data.rain is None
    assert coordinator.last_update_success


async def test_error_retains_observation_advances_age_and_notifies_each_poll(
    hass, coordinator, freezer
):
    notifications = []
    await coordinator.async_refresh()
    remove = coordinator.async_add_listener(
        lambda: notifications.append(coordinator.current_result)
    )
    try:
        coordinator.downloader.radar_exists.side_effect = httpx.ReadTimeout("timeout")
        for minutes in (6, 7):
            freezer.move_to(OBSERVATION + timedelta(minutes=minutes))
            await coordinator.async_refresh()
            assert coordinator.data.health == "error"
            assert coordinator.data.last_update == OBSERVATION
        # Consecutive failures still publish, so the age entity keeps advancing.
        assert len(notifications) == 2
        ages = [
            item.age_minutes(OBSERVATION + timedelta(minutes=7))
            for item in notifications
        ]
        assert ages == [7, 7]
        assert notifications[0].health == notifications[1].health == "error"
    finally:
        remove()


async def test_error_recovers_on_a_later_poll(hass, coordinator, freezer):
    coordinator.downloader.radar_exists.side_effect = httpx.ConnectError("down")
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"

    coordinator.downloader.radar_exists.side_effect = None
    coordinator.downloader.radar_exists.return_value = True
    freezer.move_to(PUBLISHED + timedelta(seconds=61))
    async_fire_time_changed_exact(hass, datetime.now(UTC))
    await hass.async_block_till_done()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.data.rain is True


async def test_unexpected_failure_latches_and_still_schedules_the_next_poll(
    coordinator, freezer
):
    await coordinator.async_refresh()
    coordinator.downloader.radar_exists.side_effect = RuntimeError("unexpected")
    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=10))
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert coordinator.current_result.health == "error"
    assert coordinator._failed is True
    assert coordinator._remove_listener is not None

    # The next poll finds nothing published; that is no evidence of recovery.
    coordinator.downloader.radar_exists.side_effect = None
    coordinator.downloader.radar_exists.return_value = False
    freezer.move_to(OBSERVATION + timedelta(minutes=6))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.health == "error"
    assert coordinator.data.rain is None
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.data.age_minutes(datetime.now(UTC)) == 6


async def test_stop_cancels_both_timers_and_no_later_work_rearms(
    hass, coordinator, freezer
):
    await coordinator.async_refresh()
    assert coordinator._remove_listener is not None
    assert coordinator._cancel_expiry is not None

    await coordinator.stop()
    assert coordinator._remove_listener is None
    assert coordinator._cancel_expiry is None
    coordinator.downloader.close.assert_awaited_once()

    requests = coordinator.downloader.radar_exists.await_count
    for minutes in (2, 11):
        freezer.move_to(OBSERVATION + timedelta(minutes=minutes))
        async_fire_time_changed_exact(hass, datetime.now(UTC))
        await hass.async_block_till_done()
    assert coordinator.downloader.radar_exists.await_count == requests
    assert coordinator._remove_listener is None
    assert coordinator._cancel_expiry is None


async def test_queued_refresh_after_stop_leaves_no_timer(coordinator):
    queued = coordinator._scheduled_refresh(datetime.now(UTC))
    await coordinator.stop()
    await queued

    assert coordinator.downloader.radar_exists.await_count == 0
    assert coordinator._remove_listener is None
    assert coordinator._cancel_expiry is None


async def test_inflight_update_finishing_after_stop_cannot_rearm(hass, coordinator):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(_timestamp):
        entered.set()
        await release.wait()
        return False

    coordinator.downloader.radar_exists.side_effect = blocked
    task = hass.async_create_task(coordinator.async_refresh())
    await entered.wait()
    await coordinator.stop()
    release.set()
    await task

    assert coordinator._remove_listener is None
    assert coordinator._cancel_expiry is None


async def test_settings_come_from_options_then_entry_data(hass):
    coordinator = make_coordinator(
        hass, options={CONF_RAIN_MAX_AGE: 5, CONF_RAIN_POLL: 120}
    )
    assert coordinator.max_age_minutes == 5
    assert coordinator.poll_seconds == 120

    saved = make_coordinator(hass)
    hass.config_entries.async_update_entry(
        saved.entry, data={**saved.entry.data, CONF_RAIN_MAX_AGE: 30}
    )
    saved = MeteoSwissRainRadarCoordinator(hass, saved.entry)
    assert saved.max_age_minutes == 30  # Initial setup stores rain settings in data.
    assert saved.poll_seconds == 60
    await coordinator.stop()


def test_result_freshness_transitions_without_a_coordinator():
    result = RadarResult(MagicMock(), True, 2.5, OBSERVATION, "ok", 10)
    assert result.at(OBSERVATION + timedelta(minutes=10)) is result
    stale = result.at(OBSERVATION + timedelta(minutes=10, microseconds=1))
    assert stale.health == "stale" and stale.rain is None
    assert stale.last_update == OBSERVATION
    assert result.at(OBSERVATION - timedelta(seconds=1)).health == "future"
    error = RadarResult(None, None, None, OBSERVATION, "error", 10)
    assert error.at(OBSERVATION + timedelta(minutes=1)) is error
    assert error.age_minutes(OBSERVATION + timedelta(minutes=1)) == 1


# ---------------------------------------------------------------------------
# Entity behavior through a real configuration entry
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

HAIL_RESULT = HailResult(OBSERVATION, HailAnalysis("ok", True, 80, 1, True), 10)
RAIN_SUFFIXES = ("rain", "distance", "last_radar", "rain_age", "rain_health")


@pytest.fixture
async def loaded_entry(hass, freezer, request):
    """Load an entry whose rain updates come from the patched rain result."""
    freezer.move_to(PUBLISHED)
    rain = getattr(request, "param", RadarResult(MagicMock(), True, 2.5, OBSERVATION))
    with (
        patch(
            f"{SETUP_MODULE}.coordinator.MeteoSwissRainRadarCoordinator."
            "_async_update_data",
            AsyncMock(return_value=rain),
        ),
        patch(
            f"{SETUP_MODULE}.hail_coordinator.MeteoSwissHailCoordinator."
            "_async_update_data",
            AsyncMock(return_value=HAIL_RESULT),
        ),
    ):
        entry = make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


def entity_ids(hass, entry):
    return {
        item.unique_id.removeprefix(f"{entry.entry_id}_"): item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def test_rain_diagnostics_match_the_hail_entity_set(hass, loaded_entry):
    ids = entity_ids(hass, loaded_entry)
    assert set(ids) == {
        *RAIN_SUFFIXES,
        "hail",
        "hail_max_poh",
        "hail_distance",
        "hail_observation",
        "hail_age",
        "hail_health",
    }
    age = hass.states.get(ids["rain_age"])
    assert age.attributes["unit_of_measurement"] == "min"
    assert age.attributes["device_class"] == "duration"
    assert float(age.state) == pytest.approx(50 / 60)
    health = hass.states.get(ids["rain_health"])
    assert health.state == "ok"
    assert health.attributes["options"] == [
        "ok",
        "missing",
        "stale",
        "future",
        "error",
    ]
    assert hass.states.get(ids["last_radar"]).state == OBSERVATION.isoformat()
    for suffix in RAIN_SUFFIXES:
        attributes = hass.states.get(ids[suffix]).attributes
        assert attributes["data_health"] == "ok"
        assert attributes["observation"] == OBSERVATION
        assert attributes["attribution"] == "Source: MeteoSwiss"
        # The legacy rain reader proves no coverage, so rain claims none.
        assert "coverage_complete" not in attributes


async def test_rain_weather_becomes_unknown_at_expiry_while_diagnostics_stay(
    hass, freezer, loaded_entry
):
    ids = entity_ids(hass, loaded_entry)
    assert hass.states.get(ids["rain"]).state == "on"
    assert hass.states.get(ids["distance"]).state == "2.5"

    freezer.move_to(OBSERVATION + timedelta(minutes=10, microseconds=1))
    async_fire_time_changed_exact(hass, datetime.now(UTC))
    await hass.async_block_till_done()

    assert hass.states.get(ids["rain"]).state == STATE_UNKNOWN
    assert hass.states.get(ids["distance"]).state == STATE_UNKNOWN
    assert hass.states.get(ids["rain_health"]).state == "stale"
    assert hass.states.get(ids["last_radar"]).state == OBSERVATION.isoformat()
    assert float(hass.states.get(ids["rain_age"]).state) == pytest.approx(10, abs=1e-4)
    # Hail keeps its own timestamp and expiry; rain never changes hail reporting.
    assert hass.states.get(ids["hail_observation"]).state == OBSERVATION.isoformat()


async def test_rain_diagnostics_stay_readable_on_coordinator_failure(
    hass, loaded_entry
):
    ids = entity_ids(hass, loaded_entry)
    loaded_entry.runtime_data.async_set_update_error(RuntimeError("rain outage"))
    await hass.async_block_till_done()

    assert hass.states.get(ids["rain"]).state == STATE_UNAVAILABLE
    assert hass.states.get(ids["distance"]).state == STATE_UNAVAILABLE
    assert hass.states.get(ids["rain_health"]).state == "error"
    assert hass.states.get(ids["last_radar"]).state == OBSERVATION.isoformat()
    assert hass.states.get(ids["rain_age"]).state not in (
        STATE_UNKNOWN,
        STATE_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    "failure,health",
    [(httpx.ConnectError("down"), "error"), (None, "missing")],
    ids=["error", "missing"],
)
async def test_first_update_outage_still_loads_entry_and_hail(
    hass, freezer, failure, health
):
    """A rain outage at startup registers diagnostics instead of failing setup."""
    freezer.move_to(PUBLISHED)
    exists = (
        AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=False)
    )
    with (
        patch.object(RadarDownloader, "radar_exists", exists),
        patch.object(RadarDownloader, "fetch_radar", AsyncMock()),
        patch(
            f"{SETUP_MODULE}.hail_coordinator.MeteoSwissHailCoordinator."
            "_async_update_data",
            AsyncMock(return_value=HAIL_RESULT),
        ),
    ):
        entry = make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            assert entry.state is ConfigEntryState.LOADED
            ids = entity_ids(hass, entry)
            assert set(RAIN_SUFFIXES) <= set(ids)
            assert hass.states.get(ids["rain_health"]).state == health
            assert hass.states.get(ids["rain"]).state == STATE_UNKNOWN
            assert hass.states.get(ids["distance"]).state == STATE_UNKNOWN
            assert hass.states.get(ids["last_radar"]).state == STATE_UNKNOWN
            assert hass.states.get(ids["rain_age"]).state == STATE_UNKNOWN
            assert hass.states.get(ids["hail_health"]).state == "ok"
            assert entry.runtime_data._remove_listener is not None
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
