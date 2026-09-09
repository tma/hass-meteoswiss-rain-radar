"""Additive version-1 setup, real HA entities/options, reload and cleanup."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.httpx_client import get_async_client
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.meteoswiss_rain_radar import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.meteoswiss_rain_radar.config_flow import hail_options_schema
from custom_components.meteoswiss_rain_radar.const import (
    CONF_HAIL_MAX_AGE,
    CONF_HAIL_POLL,
    CONF_HAIL_RADIUS,
    CONF_HAIL_THRESHOLD,
    DOMAIN,
)
from custom_components.meteoswiss_rain_radar.coordinator import (
    MeteoSwissRainRadarCoordinator,
)
from custom_components.meteoswiss_rain_radar.hail_coordinator import HailResult
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis
from custom_components.meteoswiss_rain_radar.models import RadarResult
from custom_components.meteoswiss_rain_radar.radar_downloader import RadarDownloader

from .hail_helpers import OBSERVATION, hail_bytes
from .test_hail_downloader import TODAY_URL, item, stac_asset

MODULE = "custom_components.meteoswiss_rain_radar"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def make_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MeteoSwiss Rain Radar",
        version=1,
        data={"radius": 5.0, "threshold": 0.2, "latitude": 0, "longitude": 0},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_entry(hass, freezer):
    freezer.move_to(OBSERVATION)
    rain_result = RadarResult(MagicMock(), True, 2.5, OBSERVATION)
    hail_result = HailResult(OBSERVATION, HailAnalysis("ok", True, 80, 1, True), 10)
    with (
        patch(
            f"{MODULE}.coordinator.MeteoSwissRainRadarCoordinator._async_update_data",
            AsyncMock(return_value=rain_result),
        ),
        patch(
            f"{MODULE}.hail_coordinator.MeteoSwissHailCoordinator._async_update_data",
            AsyncMock(return_value=hail_result),
        ),
    ):
        entry = make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_version1_rain_runtime_api_ids_and_hail_entities(hass, setup_entry):
    entry = setup_entry
    assert entry.version == 1
    assert entry.data["radius"] == 5 and entry.data["threshold"] == 0.2
    rain = entry.runtime_data
    assert isinstance(rain, MeteoSwissRainRadarCoordinator)
    assert hass.data[DOMAIN][entry.entry_id] is rain
    registry = er.async_get(hass)
    suffixes = {
        item.unique_id.removeprefix(f"{entry.entry_id}_"): item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert set(suffixes) == {
        "rain",
        "distance",
        "last_radar",
        "hail",
        "hail_max_poh",
        "hail_distance",
        "hail_observation",
        "hail_age",
        "hail_health",
    }
    assert hass.states.get(suffixes["rain"]).state == "on"
    assert hass.states.get(suffixes["distance"]).state == "2.5"
    assert hass.states.get(suffixes["hail"]).state == "on"
    assert (
        hass.states.get(suffixes["hail_max_poh"]).attributes["unit_of_measurement"]
        == "%"
    )
    assert (
        hass.states.get(suffixes["hail_distance"]).attributes["unit_of_measurement"]
        == "km"
    )
    assert (
        hass.states.get(suffixes["hail_age"]).attributes["unit_of_measurement"] == "min"
    )
    assert hass.states.get(suffixes["hail_health"]).state == "ok"
    assert not hass.states.async_entity_ids("cover")
    assert not hass.services.has_service("cover", "open_cover")
    # A source outage only changes hail reporting, never rain availability/state.
    rain.hail_coordinator.async_set_updated_data(
        HailResult(OBSERVATION, HailAnalysis("error"), 10)
    )
    await hass.async_block_till_done()
    for suffix in ("hail", "hail_max_poh", "hail_distance"):
        assert hass.states.get(suffixes[suffix]).state == STATE_UNKNOWN
    assert hass.states.get(suffixes["hail_health"]).state == "error"
    assert hass.states.get(suffixes["rain"]).state == "on"


async def test_options_reload_without_migration_or_id_changes(hass, setup_entry):
    entry = setup_entry
    previous = entry.runtime_data
    registry = er.async_get(hass)
    before = {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    hass.config_entries.async_update_entry(
        entry, options={CONF_HAIL_POLL: 120, CONF_HAIL_THRESHOLD: 90}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is not previous
    assert previous.hail_coordinator._stopped
    assert previous._remove_listener is None
    assert entry.runtime_data.hail_coordinator.poll_seconds == 120
    assert entry.version == 1
    assert before == {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def test_options_flow_defaults_and_validation(hass, setup_entry):
    entry = setup_entry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults == {
        "radius": 5.0,
        "threshold": 0.2,
        CONF_HAIL_RADIUS: 10.0,
        CONF_HAIL_THRESHOLD: 80.0,
        CONF_HAIL_MAX_AGE: 10.0,
        CONF_HAIL_POLL: 60.0,
    }
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_HAIL_THRESHOLD: 101}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HAIL_RADIUS: 2.5,
            CONF_HAIL_THRESHOLD: 80,
            CONF_HAIL_MAX_AGE: 5,
            CONF_HAIL_POLL: 90,
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_HAIL_RADIUS] == 2.5
    assert entry.options["threshold"] == 0.2


@pytest.mark.parametrize(
    "key", [CONF_HAIL_RADIUS, CONF_HAIL_THRESHOLD, CONF_HAIL_MAX_AGE, CONF_HAIL_POLL]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "bad", None, 1000])
def test_hail_options_require_finite_bounded_numbers(key, value):
    with pytest.raises(vol.Invalid):
        vol.Schema(hail_options_schema({}))({key: value})


def test_hail_fractional_radius_and_inclusive_percentage_option():
    result = vol.Schema(hail_options_schema({}))(
        {CONF_HAIL_RADIUS: 0.25, CONF_HAIL_THRESHOLD: 80}
    )
    assert result[CONF_HAIL_RADIUS] == 0.25
    assert result[CONF_HAIL_THRESHOLD] == 80


async def test_setup_failure_cleans_both_coordinators(hass):
    entry = make_entry(hass)
    with (
        patch(f"{MODULE}.MeteoSwissRainRadarCoordinator") as rain_class,
        patch(f"{MODULE}.MeteoSwissHailCoordinator") as hail_class,
    ):
        rain = rain_class.return_value
        hail = hail_class.return_value
        rain.async_config_entry_first_refresh = AsyncMock(
            side_effect=RuntimeError("setup failed")
        )
        rain.stop = AsyncMock()
        hail.stop = AsyncMock()
        with pytest.raises(RuntimeError, match="setup failed"):
            await async_setup_entry(hass, entry)
        rain.stop.assert_awaited_once()
        hail.stop.assert_awaited_once()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_failed_platform_unload_leaves_running_coordinators(hass, setup_entry):
    entry = setup_entry
    rain = entry.runtime_data
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert not await async_unload_entry(hass, entry)
    assert rain.hail_coordinator._stopped is False
    assert hass.data[DOMAIN][entry.entry_id] is rain


async def test_unload_entry_cleans_mapping_and_both_pollers(hass, setup_entry):
    entry = setup_entry
    rain = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]
    assert rain.hail_coordinator._stopped
    assert rain._remove_listener is None


@pytest.fixture
async def http_setup_entry(hass, freezer, httpx_mock):
    """Set up real coordinators and platforms with synthetic HTTP responses."""
    freezer.move_to(OBSERVATION)
    content = hail_bytes()
    asset = stac_asset(content=content)
    _, rain_url = RadarDownloader.build_url(OBSERVATION)
    httpx_mock.add_response(url=rain_url, method="HEAD")
    httpx_mock.add_response(url=rain_url, method="GET", content=content)
    httpx_mock.add_response(url=TODAY_URL, json=item(asset), is_reusable=True)
    httpx_mock.add_response(url=asset["href"], content=content)
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry
    finally:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("outcome", ["missing", "success"])
async def test_rain_inflight_refresh_cannot_rearm_after_unload(
    hass, freezer, httpx_mock, http_setup_entry, outcome
):
    entry = http_setup_entry
    rain = entry.runtime_data
    shared = get_async_client(hass)
    assert rain.downloader._client is shared
    assert not rain.downloader._owns_client
    previous = rain.result_data
    entered, release = asyncio.Event(), asyncio.Event()
    next_observation = OBSERVATION + timedelta(minutes=5)
    _, rain_url = rain.downloader.build_url(next_observation)

    async def response(request):
        entered.set()
        await release.wait()
        return httpx.Response(404 if outcome == "missing" else 200, content=b"rain")

    if outcome == "success":
        httpx_mock.add_response(url=rain_url, method="HEAD")
    httpx_mock.add_callback(
        response, url=rain_url, method="HEAD" if outcome == "missing" else "GET"
    )
    with patch(
        f"{MODULE}.coordinator.RadarData.from_bytes", return_value=previous.radar
    ):
        try:
            freezer.move_to(next_observation + timedelta(seconds=50))
            async_fire_time_changed_exact(hass, datetime.now(UTC))
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert rain._remove_listener is None
            assert not shared.is_closed
            requests_at_unload = httpx_mock.get_requests()
            release.set()
            await hass.async_block_till_done()
            assert rain.last_exception is None
            assert rain._remove_listener is None
            if outcome == "missing":
                assert rain.result_data is previous
            else:
                assert rain.result_data.last_update == next_observation
            for delta in (timedelta(seconds=16), timedelta(minutes=6)):
                freezer.move_to(datetime.now(UTC) + delta)
                async_fire_time_changed_exact(hass, datetime.now(UTC))
                await hass.async_block_till_done()
            assert rain._remove_listener is None
            assert httpx_mock.get_requests() == requests_at_unload
            assert not shared.is_closed
        finally:
            release.set()
            await hass.async_block_till_done()
            await rain.stop()


async def test_rain_queued_refresh_after_stop_does_not_touch_downloader(
    hass, httpx_mock, http_setup_entry
):
    rain = http_setup_entry.runtime_data
    shared = get_async_client(hass)
    requests = httpx_mock.get_requests()
    queued_refresh = rain._scheduled_refresh(OBSERVATION)
    await rain.stop()
    await queued_refresh
    assert rain.last_exception is None
    assert rain.last_update_success
    assert rain._remove_listener is None
    assert httpx_mock.get_requests() == requests
    assert not shared.is_closed


async def test_new_configuration_keeps_rain_defaults_and_current_coordinates(hass):
    hass.config.latitude = 0
    hass.config.longitude = 0
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["data_schema"]({}) == {"radius": 5.0, "threshold": 0.2}
    with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"radius": 5.0, "threshold": 0.2}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["data"]["latitude"] == result["data"]["longitude"] == 0
    assert result["result"].version == 1


async def test_multiple_entries_unload_independently(hass, setup_entry):
    first = setup_entry
    second = make_entry(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    assert first.runtime_data is not second.runtime_data
    assert (
        first.runtime_data.hail_coordinator is not second.runtime_data.hail_coordinator
    )
    shared = first.runtime_data.downloader._client
    assert second.runtime_data.downloader._client is shared
    assert first.runtime_data.hail_coordinator.downloader._client is shared
    assert second.runtime_data.hail_coordinator.downloader._client is shared
    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert not shared.is_closed
    assert first.runtime_data.downloader._client is shared
    assert first.entry_id in hass.data[DOMAIN]
    assert not first.runtime_data.hail_coordinator._stopped
    assert second.entry_id not in hass.data[DOMAIN]


async def test_homeassistant_shutdown_closes_both_coordinators(hass, setup_entry):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    rain = setup_entry.runtime_data
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert rain.hail_coordinator._stopped
    assert rain._remove_listener is None


async def test_platform_setup_exception_cleans_pollers(hass):
    entry = make_entry(hass)
    with (
        patch(f"{MODULE}.MeteoSwissRainRadarCoordinator") as rain_class,
        patch(f"{MODULE}.MeteoSwissHailCoordinator") as hail_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=RuntimeError("platform failed")),
        ),
    ):
        rain = rain_class.return_value
        hail = hail_class.return_value
        rain.async_config_entry_first_refresh = AsyncMock()
        hail.async_refresh = AsyncMock()
        rain.stop = AsyncMock()
        hail.stop = AsyncMock()
        with pytest.raises(RuntimeError, match="platform failed"):
            await async_setup_entry(hass, entry)
        rain.stop.assert_awaited_once()
        hail.stop.assert_awaited_once()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize("stage", ["rain", "hail", "platforms"])
async def test_shutdown_during_setup_cancels_owner_and_cleans_resources(
    hass, freezer, httpx_mock, stage
):
    import asyncio

    import httpx
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.helpers.httpx_client import get_async_client

    from custom_components.meteoswiss_rain_radar.hail_coordinator import (
        MeteoSwissHailCoordinator,
    )

    from .hail_helpers import hail_bytes
    from .test_hail_downloader import TODAY_URL, item, stac_asset

    freezer.move_to(OBSERVATION)
    entry = make_entry(hass)
    entered, release = asyncio.Event(), asyncio.Event()
    constructed = []
    content = hail_bytes()
    asset = stac_asset(content=content)
    original_listeners = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)

    def rain_factory(*args):
        rain = MeteoSwissRainRadarCoordinator(*args)
        constructed.append(rain)
        return rain

    def hail_factory(*args):
        hail = MeteoSwissHailCoordinator(*args)
        constructed.append(hail)
        return hail

    async def barrier():
        entered.set()
        await release.wait()

    async def rain_response(request):
        if stage == "rain":
            await barrier()
        return httpx.Response(200)

    async def daily_response(request):
        if stage == "hail":
            await barrier()
        return httpx.Response(200, json=item(asset), headers={"ETag": '"setup"'})

    async def rain_update(rain):
        assert await rain.downloader.radar_exists(OBSERVATION)
        rain._schedule_next_update()
        return RadarResult(MagicMock(), False, None, OBSERVATION)

    async def forward(*args):
        assert stage == "platforms"
        await barrier()

    httpx_mock.add_callback(rain_response, method="HEAD")
    if stage != "rain":
        httpx_mock.add_callback(daily_response, url=TODAY_URL)
    if stage == "platforms":
        httpx_mock.add_response(url=asset["href"], content=content)

    with (
        patch(f"{MODULE}.MeteoSwissRainRadarCoordinator", new=rain_factory),
        patch(f"{MODULE}.MeteoSwissHailCoordinator", new=hail_factory),
        patch.object(
            MeteoSwissRainRadarCoordinator, "_async_update_data", new=rain_update
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=forward),
    ):
        task = hass.async_create_task(async_setup_entry(hass, entry))
        try:
            await entered.wait()
            rain, hail = constructed
            shared = get_async_client(hass)
            assert rain.downloader._client is hail.downloader._client is shared
            assert (
                hass.bus.async_listeners()[EVENT_HOMEASSISTANT_STOP]
                == original_listeners + 1
            )
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            with pytest.raises(asyncio.CancelledError):
                await task
            await hass.async_block_till_done()
            assert hail._stopped
            assert hail._cancel_expiry is hail._unsub_refresh is None
            assert rain._remove_listener is None
            assert hail._update_task is None
            assert rain.downloader._client is hail.downloader._client is None
            assert not shared.is_closed  # HA owns this until its CLOSE event.
            assert entry.entry_id not in hass.data.get(DOMAIN, {})
            assert not hasattr(entry, "runtime_data")
            assert (
                hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)
                == 0  # HA's initial one-shot listeners also ran.
            )
            release.set()
            await hass.async_block_till_done()
            assert task.cancelled()
            assert entry.entry_id not in hass.data.get(DOMAIN, {})
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
