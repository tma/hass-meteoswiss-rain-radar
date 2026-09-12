"""Validate the notification example with real HA schemas and no service calls."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.automation.config import (
    ValidationStatus,
    async_validate_config_item,
)
from homeassistant.components.persistent_notification import async_setup
from homeassistant.helpers import condition, entity_registry, template
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.yaml import load_yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_rain_radar.const import DOMAIN
from custom_components.meteoswiss_rain_radar.hail_coordinator import HailResult
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis
from custom_components.meteoswiss_rain_radar.models import RadarResult

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1] / "docs/examples/hail-notification.yaml"
)
NOW = datetime(2026, 6, 1, 12, 10, tzinfo=UTC)
OBSERVATION = NOW - timedelta(minutes=5)
MODULE = "custom_components.meteoswiss_rain_radar"


@pytest.fixture
def example_yaml():
    return load_yaml(str(EXAMPLE_PATH))


@pytest.fixture
async def example(hass, freezer, example_yaml):
    freezer.move_to(NOW)
    with patch.object(
        type(hass.services),
        "async_call",
        AsyncMock(side_effect=AssertionError("No actions allowed")),
    ) as service_call:
        config = await async_validate_config_item(
            hass, "automation", deepcopy(example_yaml)
        )
        assert config.validation_status == ValidationStatus.OK
        assert config["initial_state"] is False
        assert len(config["actions"]) == 1
        assert config["actions"][0]["action"] == "persistent_notification.create"
        yield config
        service_call.assert_not_called()


def set_states(
    hass,
    example_yaml,
    *,
    state="on",
    health="ok",
    observation=OBSERVATION,
    poh="80",
    distance="2",
    poh_unit="%",
    distance_unit="km",
    mismatch=False,
):
    attrs = {
        "observation": observation,
        "data_health": health,
        "coverage_complete": health == "ok",
    }
    variables = example_yaml["variables"]
    hass.states.async_set(variables["hail_entity"], state, attrs)
    hass.states.async_set(
        variables["poh_entity"], poh, {**attrs, "unit_of_measurement": poh_unit}
    )
    if mismatch:
        attrs = {**attrs, "observation": OBSERVATION - timedelta(minutes=5)}
    hass.states.async_set(
        variables["distance_entity"],
        distance,
        {**attrs, "unit_of_measurement": distance_unit},
    )


def evaluate(hass, config):
    variables = config["variables"].async_render(hass, {})
    decision = condition.async_template(
        hass, config["conditions"][0]["value_template"], variables
    )
    return decision, variables


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, True),
        ({"health": "partial_coverage"}, True),
        ({"observation": OBSERVATION.isoformat()}, True),
        ({"observation": NOW}, True),
        ({"observation": NOW - timedelta(minutes=10)}, True),
        ({"observation": NOW - timedelta(minutes=10, microseconds=1)}, False),
        ({"observation": NOW + timedelta(seconds=1)}, False),
        ({"observation": None}, False),
        ({"observation": "bad-date"}, False),
        ({"state": "unknown"}, False),
        ({"state": "unavailable"}, False),
        ({"state": "off", "poh": "0", "distance": "unknown"}, False),
        ({"state": "unknown", "health": "partial_coverage", "poh": "20"}, False),
        ({"poh": "79.99999"}, False),
        ({"poh": "unknown"}, False),
        ({"poh": "nan"}, False),
        ({"poh": "inf"}, False),
        ({"distance": "unknown"}, False),
        ({"distance": "nan"}, False),
        ({"distance": "-1"}, False),
        ({"distance": "10.01"}, False),
        ({"distance": "10"}, True),
        ({"poh_unit": None}, False),
        ({"distance_unit": "mi"}, False),
        ({"mismatch": True}, False),
        *[
            ({"health": health}, False)
            for health in (
                "outside_grid",
                "no_cells",
                "all_nodata",
                "empty",
                "off_season",
                "missing",
                "stale",
                "future",
                "error",
            )
        ],
    ],
)
async def test_notification_condition(hass, example, example_yaml, kwargs, expected):
    set_states(hass, example_yaml, **kwargs)
    decision, variables = evaluate(hass, example)
    assert decision is expected
    if expected:
        # Register the service to use its actual schema, but never call it.
        await async_setup(hass, {})
        payload = template.render_complex(example["actions"][0]["data"], variables)
        schema = hass.services.async_services()["persistent_notification"][
            "create"
        ].schema
        schema(payload)
        assert "Source: MeteoSwiss" in payload["message"]
        assert OBSERVATION.date().isoformat() in payload["message"]
        assert payload["notification_id"] == "meteoswiss_hail_reporting"
        assert "latitude" not in payload["message"]
        assert "longitude" not in payload["message"]


async def test_cached_data_expires_without_clear_or_release(
    hass, example, example_yaml, freezer
):
    hass.loop.set_debug(False)  # A frozen monotonic jump isn't a slow task.
    set_states(hass, example_yaml)
    decision, variables = evaluate(hass, example)
    assert decision is True
    before = variables["observation"]
    freezer.move_to(NOW + timedelta(minutes=4))
    decision, variables = evaluate(hass, example)
    assert decision is True and variables["observation"] == before
    assert variables["age_minutes"] == 9
    freezer.move_to(NOW + timedelta(minutes=6))
    assert evaluate(hass, example)[0] is False
    set_states(
        hass,
        example_yaml,
        state="off",
        poh="0",
        distance="unknown",
        observation=NOW + timedelta(minutes=5),
    )
    assert evaluate(hass, example)[0] is False


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_example_matches_actual_hail_entities(hass, example, example_yaml):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MeteoSwiss Rain Radar",
        version=1,
        data={"radius": 5, "threshold": 0.2, "latitude": 0, "longitude": 0},
    )
    entry.add_to_hass(hass)
    rain = RadarResult(MagicMock(), False, None, OBSERVATION)
    hail = HailResult(OBSERVATION, HailAnalysis("ok", True, 80, 2, True), 10)
    with (
        patch(
            f"{MODULE}.coordinator.MeteoSwissRainRadarCoordinator._async_update_data",
            AsyncMock(return_value=rain),
        ),
        patch(
            f"{MODULE}.hail_coordinator.MeteoSwissHailCoordinator._async_update_data",
            AsyncMock(return_value=hail),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            registry = entity_registry.async_get(hass)
            suffixes = {
                item.unique_id.removeprefix(f"{entry.entry_id}_"): item.entity_id
                for item in entity_registry.async_entries_for_config_entry(
                    registry, entry.entry_id
                )
            }
            variables = example_yaml["variables"]
            assert suffixes["hail"] == variables["hail_entity"]
            assert suffixes["hail_max_poh"] == variables["poh_entity"]
            assert suffixes["hail_distance"] == variables["distance_entity"]
            assert set(example_yaml["trigger"][0]["entity_id"]) == {
                variables[key]
                for key in ("hail_entity", "poh_entity", "distance_entity")
            }
            assert evaluate(hass, example)[0] is True
            assert not hass.states.async_entity_ids("cover")
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("category,step", [("config", "user"), ("options", "init")])
async def test_native_form_section_translations_labels_units_and_help(
    hass, category, step
):
    path = (
        EXAMPLE_PATH.parents[2]
        / "custom_components/meteoswiss_rain_radar/translations/en.json"
    )
    source = json.loads(path.read_text())
    sections = source[category]["step"][step]["sections"]
    translated = await async_get_translations(hass, "en", category, {DOMAIN})
    prefix = f"component.{DOMAIN}.{category}.step.{step}.sections"
    units = {
        "rain": {
            "radius": "km",
            "threshold": "mm/h",
            "rain_max_age_minutes": "minutes",
            "rain_poll_seconds": "seconds",
        },
        "hail": {
            "hail_radius_km": "km",
            "hail_poh_threshold": "%",
            "hail_max_age_minutes": "minutes",
            "hail_poll_seconds": "seconds",
        },
    }
    assert set(sections) == {"rain", "hail"}
    for section, fields in units.items():
        texts = sections[section]
        assert translated[f"{prefix}.{section}.name"] == section.title()
        assert translated[f"{prefix}.{section}.description"] == texts["description"]
        assert texts["description"]
        assert set(texts["data"]) == set(texts["data_description"]) == set(fields)
        for key, unit in fields.items():
            label = translated[f"{prefix}.{section}.data.{key}"]
            assert label == texts["data"][key]
            assert f"({unit})" in label
            help_text = translated[f"{prefix}.{section}.data_description.{key}"]
            assert help_text == texts["data_description"][key]
            assert len(help_text.split()) >= 10
    rain_help = sections["rain"]["data_description"]
    assert "only above" in rain_help["threshold"]
    assert "0.2 mm/h" in rain_help["threshold"]
    assert "lighter" in rain_help["threshold"] and "heavier" in rain_help["threshold"]
    assert "five-minute accumulation" in rain_help["threshold"]
    assert "rounds fractional radii up" in rain_help["radius"]
    assert "unknown" in rain_help["rain_max_age_minutes"]
    assert "does not create new observations" in rain_help["rain_poll_seconds"]
    hail_help = sections["hail"]["data_description"]
    assert "true circle" in hail_help["hail_radius_km"]
    assert "current Home Assistant home" in hail_help["hail_radius_km"]
    assert "meets or exceeds" in hail_help["hail_poh_threshold"]
    assert "any size" in hail_help["hail_poh_threshold"]
    assert "property hit" in hail_help["hail_poh_threshold"]
    assert "unknown" in hail_help["hail_max_age_minutes"]
    assert "does not create new observations" in hail_help["hail_poll_seconds"]
    assert (
        "finite" in translated[f"component.{DOMAIN}.{category}.error.invalid_options"]
    )
    assert source["config"]["step"]["user"] == source["options"]["step"]["init"]
