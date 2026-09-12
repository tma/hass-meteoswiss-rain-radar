"""Render real HA forms, not just validate their Voluptuous schemas."""

import json
import math
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from aiohttp.resolver import ThreadedResolver
from homeassistant.components.config.config_entries import (
    ConfigManagerFlowIndexView,
    ConfigManagerFlowResourceView,
    OptionManagerFlowIndexView,
    OptionManagerFlowResourceView,
)
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers.data_entry_flow import FlowManagerIndexView
from homeassistant.setup import async_setup_component

from custom_components.meteoswiss_rain_radar.const import DOMAIN

from .hail_helpers import OBSERVATION, hail_bytes
from .test_hail_downloader import TODAY_URL, item, stac_asset
from .test_hail_setup import MODULE, make_entry
from .test_hail_setup import setup_entry as setup_entry

DEFAULT_SETTINGS = {
    "rain": {
        "radius": 5.0,
        "threshold": 0.2,
        "rain_max_age_minutes": 10.0,
        "rain_poll_seconds": 60.0,
    },
    "hail": {
        "hail_radius_km": 10.0,
        "hail_poh_threshold": 80.0,
        "hail_max_age_minutes": 10.0,
        "hail_poll_seconds": 60.0,
    },
}
FIELDS = [
    (section, key) for section, values in DEFAULT_SETTINGS.items() for key in values
]
HAIL_BOUNDS = {
    "hail_radius_km": (0.1, 100),
    "hail_poh_threshold": (0, 100),
    "hail_max_age_minutes": (1, 60),
    "hail_poll_seconds": (15, 300),
}
RAIN_BOUNDS = {"rain_max_age_minutes": (1, 60), "rain_poll_seconds": (15, 300)}
# Legacy rain radius/threshold deliberately keep no ranges.
BOUNDS = {"rain": RAIN_BOUNDS, "hail": HAIL_BOUNDS}
BOUNDED_FIELDS = [
    (section, key) for section, bounds in BOUNDS.items() for key in bounds
]

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize("flow_kind", ["config", "options"])
async def test_form_serializes_with_actual_ha_view(hass, flow_kind):
    if flow_kind == "config":
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    else:
        manager = hass.config_entries.options
        result = await manager.async_init(make_entry(hass).entry_id)
    view = FlowManagerIndexView(manager)
    rendered = view._prepare_result_json(result)
    assert json.loads(json.dumps(rendered, allow_nan=False))["type"] == "form"
    assert view.json(rendered).status == 200
    assert result["data_schema"]({}) == DEFAULT_SETTINGS
    assert_form_schema(rendered)


@pytest.fixture
async def flow_client(hass, hass_client):
    """Expose the actual HA configuration/options views on an isolated HTTP server."""
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(ConfigManagerFlowIndexView(hass.config_entries.flow))
    hass.http.register_view(ConfigManagerFlowResourceView(hass.config_entries.flow))
    hass.http.register_view(OptionManagerFlowIndexView(hass.config_entries.options))
    hass.http.register_view(OptionManagerFlowResourceView(hass.config_entries.options))
    # Loopback needs no async DNS; avoid pycares' process-wide cleanup thread.
    with (
        patch("aiohttp.connector.DefaultResolver", ThreadedResolver),
        patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)),
    ):
        yield await hass_client()
        await hass.async_block_till_done()


@pytest.mark.parametrize("flow_kind", ["config", "options"])
async def test_form_http_endpoint(hass, flow_client, flow_kind):
    path = "flow" if flow_kind == "config" else "options/flow"
    handler = DOMAIN if flow_kind == "config" else make_entry(hass).entry_id
    response = await flow_client.post(
        f"/api/config/config_entries/{path}", json={"handler": handler}
    )
    assert response.status == 200
    rendered = await response.json()
    assert rendered["type"] == "form"
    json.dumps(rendered, allow_nan=False)
    assert_form_schema(rendered)


def assert_form_schema(rendered, defaults=DEFAULT_SETTINGS):
    """Check the frontend packet, including nested field defaults and bounds."""
    sections = rendered["data_schema"]
    assert [section["name"] for section in sections] == ["rain", "hail"]
    for section in sections:
        assert section["type"] == "expandable"
        assert section["expanded"] is True
        fields = section["schema"]
        assert {field["name"]: field["default"] for field in fields} == defaults[
            section["name"]
        ]
        for field in fields:
            assert field["type"] == "float"
            assert math.isfinite(field["default"])
            bounds = BOUNDS[section["name"]].get(field["name"])
            if bounds is not None:
                assert (field["valueMin"], field["valueMax"]) == bounds
            else:
                assert "valueMin" not in field and "valueMax" not in field


@pytest.mark.parametrize("flow_kind", ["config", "options"])
@pytest.mark.parametrize("section_name,key", FIELDS)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
async def test_nonfinite_submission_http_never_saves_or_breaks_error_form(
    hass, flow_client, flow_kind, section_name, key, value
):
    path = "flow" if flow_kind == "config" else "options/flow"
    entry = None if flow_kind == "config" else make_entry(hass)
    response = await flow_client.post(
        f"/api/config/config_entries/{path}",
        json={"handler": DOMAIN if entry is None else entry.entry_id},
    )
    flow_id = (await response.json())["flow_id"]
    url = f"/api/config/config_entries/{path}/{flow_id}"
    response = await flow_client.post(url, json={section_name: {key: value}})
    # Range rejection happens before the step. NaN handling varies by Voluptuous.
    bounded = key in BOUNDS[section_name]
    allowed_statuses = (400,) if bounded else (200,)
    if bounded and value == "nan":
        allowed_statuses = (200, 400)
    assert response.status in allowed_statuses
    rendered = await response.json()
    json.dumps(rendered, allow_nan=False)
    assert rendered["errors"]
    if response.status == 200:
        assert rendered["type"] == "form"
        assert rendered["errors"] == {"base": "invalid_options"}
        assert_form_schema(rendered)
    assert len(hass.config_entries.async_entries(DOMAIN)) == (0 if entry is None else 1)
    if entry:
        assert entry.options == {}
    response = await flow_client.get(url)
    assert response.status == 200
    assert_form_schema(await response.json())
    # A corrected submission on the same flow still works.
    with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
        response = await flow_client.post(url, json={})
        assert response.status == 200
        assert (await response.json())["type"] == "create_entry"
        await hass.async_block_till_done()
    saved = hass.config_entries.async_entries(DOMAIN)[0]
    for value in (*saved.data.values(), *saved.options.values()):
        assert math.isfinite(value)


@pytest.mark.parametrize("flow_kind", ["config", "options"])
@pytest.mark.parametrize(
    "payload",
    [
        {section: {key: value}}
        for section, bounds in BOUNDS.items()
        for key, limits in bounds.items()
        for value in (limits[0] - 0.01, limits[1] + 0.01)
    ]
    + [{section: {key: "bad"}} for section, key in FIELDS]
    + [{"rain": None}, {"hail": []}],
)
async def test_invalid_shape_type_or_bounds_returns_http400(
    hass, flow_client, flow_kind, payload
):
    path = "flow" if flow_kind == "config" else "options/flow"
    handler = DOMAIN if flow_kind == "config" else make_entry(hass).entry_id
    response = await flow_client.post(
        f"/api/config/config_entries/{path}", json={"handler": handler}
    )
    flow_id = (await response.json())["flow_id"]
    url = f"/api/config/config_entries/{path}/{flow_id}"
    response = await flow_client.post(url, json=payload)
    assert response.status == 400
    errors = await response.json()
    assert errors["errors"]
    json.dumps(errors, allow_nan=False)
    response = await flow_client.get(url)
    assert response.status == 200
    assert_form_schema(await response.json())


@pytest.mark.parametrize("flow_kind", ["config", "options"])
@pytest.mark.parametrize("boundary", [0, 1])
async def test_bounds_are_inclusive_and_storage_stays_flat(hass, flow_kind, boundary):
    if flow_kind == "config":
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    else:
        manager = hass.config_entries.options
        result = await manager.async_init(make_entry(hass).entry_id)
    hail = {key: bounds[boundary] for key, bounds in HAIL_BOUNDS.items()}
    rain = {key: bounds[boundary] for key, bounds in RAIN_BOUNDS.items()}
    # No new legacy rain ranges.
    rain.update(radius=-0.25, threshold=-0.1)
    with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
        result = await manager.async_configure(
            result["flow_id"], user_input={"rain": rain, "hail": hail}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == 1
    if flow_kind == "config":
        assert entry.options == hail
        assert entry.data == {
            **rain,
            "latitude": hass.config.latitude,
            "longitude": hass.config.longitude,
        }
    else:
        assert entry.options == {**rain, **hail}


async def test_initial_hail_values_reach_real_coordinator(hass, freezer, httpx_mock):
    import numpy as np

    from custom_components.meteoswiss_rain_radar.hail_reader import read_hail
    from custom_components.meteoswiss_rain_radar.models import RadarResult

    freezer.move_to(OBSERVATION)
    hass.config.latitude = hass.config.longitude = 0
    values = np.zeros((7, 7))
    values[3, 4] = 0.9  # Qualifying center 0.5 km east, within the chosen circle.
    values[3, 5] = 1  # Outside 0.75 km; must not raise the reported maximum.
    content = hail_bytes(values)
    asset = stac_asset(content=content)
    httpx_mock.add_response(url=TODAY_URL, json=item(asset))
    httpx_mock.add_response(url=asset["href"], content=content)
    hail = {
        "hail_radius_km": 0.75,
        "hail_poh_threshold": 90,
        "hail_max_age_minutes": 3,
        "hail_poll_seconds": 120,
    }
    rain = {
        "radius": 2.5,
        "threshold": 0.4,
        "rain_max_age_minutes": 4,
        "rain_poll_seconds": 150,
    }
    calls = []

    def reader(*args):
        calls.append(args[2:])
        return read_hail(*args)

    with (
        patch(
            f"{MODULE}.coordinator.MeteoSwissRainRadarCoordinator._async_update_data",
            AsyncMock(return_value=RadarResult(MagicMock(), False, None, OBSERVATION)),
        ),
        patch(f"{MODULE}.hail_coordinator.read_hail", new=reader),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"rain": rain, "hail": hail}
        )
        await hass.async_block_till_done()
        entry = result["result"]
        try:
            assert entry.options == hail
            assert entry.data == {**rain, "latitude": 0, "longitude": 0}
            rain_coordinator = entry.runtime_data
            assert rain_coordinator.poll_seconds == 150
            assert rain_coordinator.max_age_minutes == 4
            coordinator = entry.runtime_data.hail_coordinator
            assert coordinator.poll_seconds == 120
            assert coordinator.update_interval == timedelta(seconds=120)
            assert coordinator.max_age_minutes == 3
            assert calls == [(0, 0, 0.75, 90)]
            assert coordinator.data.analysis.detected is True
            assert coordinator.data.analysis.max_poh == 90
            assert coordinator.data.analysis.distance_km == pytest.approx(0.5)
            assert (
                coordinator.data.at(
                    OBSERVATION + timedelta(minutes=3)
                ).analysis.detected
                is True
            )
            assert (
                coordinator.data.at(
                    OBSERVATION + timedelta(minutes=3, microseconds=1)
                ).analysis.health
                == "stale"
            )
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_old_entry_options_reopen_reload_and_isolation(hass, setup_entry):
    first = setup_entry
    second = make_entry(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    second_runtime = second.runtime_data
    previous = first.runtime_data
    original_data = dict(first.data)
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    before = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, first.entry_id)
    }
    try:
        result = await hass.config_entries.options.async_init(first.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"rain": {"radius": 7.5}, "hail": {"hail_poll_seconds": 90}},
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        assert first.data == original_data
        assert first.version == 1
        assert first.runtime_data is not previous
        assert previous.hail_coordinator._stopped
        assert first.runtime_data.hail_coordinator.poll_seconds == 90
        assert first.options == {
            **DEFAULT_SETTINGS["rain"],
            **DEFAULT_SETTINGS["hail"],
            "radius": 7.5,
            "hail_poll_seconds": 90,
        }
        assert before == {
            entity.unique_id: entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, first.entry_id)
        }
        assert second.runtime_data is second_runtime
        assert second.options == {}
        assert not second_runtime.hail_coordinator._stopped
        reopened = await hass.config_entries.options.async_init(first.entry_id)
        rendered = FlowManagerIndexView(
            hass.config_entries.options
        )._prepare_result_json(reopened)
        assert_form_schema(
            rendered,
            {
                "rain": {**DEFAULT_SETTINGS["rain"], "radius": 7.5},
                "hail": {**DEFAULT_SETTINGS["hail"], "hail_poll_seconds": 90},
            },
        )
        json.dumps(rendered, allow_nan=False)
    finally:
        assert await hass.config_entries.async_unload(second.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("flow_kind", ["config", "options"])
@pytest.mark.parametrize("section_name,key", FIELDS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_numeric_nonfinite_cannot_reach_storage(
    hass, flow_kind, section_name, key, value
):
    # Nonfinite numbers aren't legal JSON. Test Python values at the flow manager.
    if flow_kind == "config":
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    else:
        manager = hass.config_entries.options
        result = await manager.async_init(make_entry(hass).entry_id)
    flow_id = result["flow_id"]
    try:
        result = await manager.async_configure(
            flow_id, user_input={section_name: {key: value}}
        )
    except InvalidData as err:
        assert key in BOUNDS[section_name]
        assert err.schema_errors
        result = await manager.async_configure(flow_id)
    else:
        assert result["errors"] == {"base": "invalid_options"}
    assert result["type"] == "form"
    rendered = FlowManagerIndexView(manager)._prepare_result_json(result)
    json.dumps(rendered, allow_nan=False)
    assert_form_schema(rendered)
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == (0 if flow_kind == "config" else 1)
    for entry in entries:
        assert entry.options == {}


@pytest.mark.parametrize("flow_kind", ["config", "options"])
@pytest.mark.parametrize("section_name,key", BOUNDED_FIELDS)
async def test_post_submission_check_rejects_nan_even_if_range_allows_it(
    hass, flow_kind, section_name, key
):
    if flow_kind == "config":
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    else:
        manager = hass.config_entries.options
        result = await manager.async_init(make_entry(hass).entry_id)
    # Simulate older/permissive Range implementations without altering display.
    with patch.object(vol.Range, "__call__", lambda self, value: value):
        result = await manager.async_configure(
            result["flow_id"], user_input={section_name: {key: "nan"}}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_options"}
    rendered = FlowManagerIndexView(manager)._prepare_result_json(result)
    json.dumps(rendered, allow_nan=False)
    assert_form_schema(rendered)
