"""Run the shadow package against isolated HA helpers; mock notifications only."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.automation import DATA_COMPONENT
from homeassistant.components.automation.config import (
    ValidationStatus,
    async_validate_config_item,
)
from homeassistant.core import Context, State
from homeassistant.helpers.script import Script
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.meteoswiss_rain_radar.const import DOMAIN
from custom_components.meteoswiss_rain_radar.hail_coordinator import HailResult
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis
from custom_components.meteoswiss_rain_radar.models import RadarResult

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1] / "docs/examples/hail-shadow-hold.yaml"
)
START = datetime(2026, 6, 1, 12, tzinfo=UTC)
HOLD = "input_boolean.hail_shadow_hold"
COUNT = "input_number.hail_shadow_clear_count"
LAST = "input_datetime.hail_shadow_last_clear"
SIGNATURE = "input_text.hail_shadow_last_signature"
SETTINGS = "input_text.hail_shadow_settings"
MODULE = "custom_components.meteoswiss_rain_radar"


def set_observation(hass, example, observation, *, health="ok", poh=0, state="off"):
    variables = example.config["variables"].as_dict()
    attrs = {
        "observation": observation,
        "data_health": health,
        "coverage_complete": health == "ok",
    }
    hass.states.async_set(variables["hail_entity"], state, attrs)
    hass.states.async_set(
        variables["poh_entity"], str(poh), {**attrs, "unit_of_measurement": "%"}
    )
    hass.states.async_set(
        variables["distance_entity"],
        "2" if state == "on" else "unknown",
        {**attrs, "unit_of_measurement": "km"},
    )


def services_in(value):
    if isinstance(value, dict):
        if "service" in value:
            yield value["service"]
        for child in value.values():
            yield from services_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from services_in(child)


def clear_count(hass):
    return int(float(hass.states.get(COUNT).state))


def clear_notifications(example):
    return [
        data
        for data in example.notifications
        if data["notification_id"] == "meteoswiss_hail_shadow_clear_evidence"
    ]


@pytest.fixture
async def shadow(hass, freezer):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    package = load_yaml(str(EXAMPLE_PATH))
    assert set(services_in(package)) == {
        "input_boolean.turn_on",
        "input_number.set_value",
        "input_datetime.set_datetime",
        "input_text.set_value",
        "persistent_notification.create",
    }
    for domain in ("input_boolean", "input_number", "input_datetime", "input_text"):
        assert all("initial" not in value for value in package[domain].values())
    mock_restore_cache(
        hass,
        [
            State(HOLD, "on"),
            State(COUNT, "6"),
            State(LAST, "2026-06-01 11:55:00"),
            State(SIGNATURE, "old observation"),
            State(SETTINGS, "old settings"),
        ],
    )
    for domain in ("input_boolean", "input_number", "input_datetime", "input_text"):
        assert await async_setup_component(hass, domain, package)
    assert await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()
    restored = {entity: hass.states.get(entity).state for entity in (HOLD, COUNT, LAST)}
    config = await async_validate_config_item(
        hass, "automation", deepcopy(package["automation"][0])
    )
    assert config.validation_status == ValidationStatus.OK
    assert config["initial_state"] is False
    script = Script(
        hass,
        config["actions"],
        "hail_shadow_example",
        "automation",
        script_mode=config["mode"],
        variables=config["variables"],
    )
    notifications = []
    calls = []
    original_call = hass.services.async_call
    allowed = {
        ("input_boolean", "turn_on"): {HOLD},
        ("input_number", "set_value"): {COUNT},
        ("input_datetime", "set_datetime"): {LAST},
        ("input_text", "set_value"): {SIGNATURE, SETTINGS},
    }

    async def guarded_call(domain, service, service_data=None, **kwargs):
        calls.append((domain, service, service_data))
        if (domain, service) == ("persistent_notification", "create"):
            schema = hass.services.async_services()[domain][service].schema
            notifications.append(schema(service_data))
            return None
        assert (domain, service) in allowed
        targets = kwargs.get("target", {}).get(
            "entity_id", service_data.get("entity_id", [])
        )
        if isinstance(targets, str):
            targets = [targets]
        assert targets and set(targets) <= allowed[domain, service]
        return await original_call(domain, service, service_data, **kwargs)

    async def run(*, trigger_id="clock", to_state=None, **variables):
        await script.async_run(
            {"trigger": {"id": trigger_id, "to_state": to_state}, **variables},
            Context(),
        )
        await hass.async_block_till_done()

    with patch.object(type(hass.services), "async_call", side_effect=guarded_call):
        await run(trigger_id="reset")
        yield SimpleNamespace(
            config=config,
            run=run,
            notifications=notifications,
            calls=calls,
            restored=restored,
        )
        await script.async_stop()
        assert not hass.states.async_entity_ids("cover")
        assert all(service != "turn_off" for _, service, _ in calls)


async def sample(hass, shadow, freezer, minute, **kwargs):
    observation = START + timedelta(minutes=minute)
    freezer.move_to(observation + timedelta(seconds=30))
    set_observation(hass, shadow, observation, **kwargs)
    await shadow.run()


async def test_restored_hold_survives_but_startup_discards_old_evidence(hass, shadow):
    assert shadow.restored[HOLD] == "on"
    assert float(shadow.restored[COUNT]) == 6
    assert shadow.restored[LAST] == "2026-06-01 11:55:00"
    assert hass.states.get(HOLD).state == "on"
    assert clear_count(hass) == 0
    assert hass.states.get(LAST).attributes["timestamp"] == START.timestamp()
    assert not shadow.notifications


@pytest.mark.parametrize("health", ["ok", "partial_coverage"])
async def test_fresh_alarm_latches_immediately_even_with_partial_coverage(
    hass, shadow, freezer, health
):
    hass.states.async_set(HOLD, "off")
    await sample(hass, shadow, freezer, 5, state="on", poh=80, health=health)
    assert hass.states.get(HOLD).state == "on"
    assert clear_count(hass) == -1
    assert len(shadow.notifications) == 1
    assert shadow.notifications[0]["title"] == "Hail shadow: hold requested"
    assert "Source: MeteoSwiss" in shadow.notifications[0]["message"]
    assert datetime.now(UTC) == START + timedelta(minutes=5, seconds=30)


async def test_seven_distinct_clear_samples_span_thirty_minutes_without_release(
    hass, shadow, freezer
):
    for index in range(1, 8):
        await sample(hass, shadow, freezer, index * 5)
        assert clear_count(hass) == index
        assert len(clear_notifications(shadow)) == (1 if index == 7 else 0)
        await shadow.run()  # Identical cache is not another sample.
        assert clear_count(hass) == index
    assert hass.states.get(HOLD).state == "on"
    assert "hold remains ON" in clear_notifications(shadow)[0]["message"]
    await sample(hass, shadow, freezer, 40)
    assert len(clear_notifications(shadow)) == 1
    assert clear_count(hass) == 7


async def test_cached_clear_cannot_supply_thirty_minutes(hass, shadow, freezer):
    await sample(hass, shadow, freezer, 5)
    for minute in range(6, 41):
        freezer.move_to(START + timedelta(minutes=minute))
        await shadow.run()
    assert clear_count(hass) == -1
    assert not clear_notifications(shadow)
    assert hass.states.get(HOLD).state == "on"


@pytest.mark.parametrize(
    "health,state,poh,offset",
    [
        ("missing", "unknown", "unknown", 0),
        ("stale", "unknown", "unknown", 0),
        ("future", "unknown", "unknown", 5),
        ("error", "unknown", "unknown", 0),
        ("empty", "unknown", "unknown", 0),
        ("all_nodata", "unknown", "unknown", 0),
        ("outside_grid", "unknown", "unknown", 0),
        ("no_cells", "unknown", "unknown", 0),
        ("off_season", "unknown", "unknown", 0),
        ("partial_coverage", "unknown", 0, 0),
        ("partial_coverage", "off", 20, 0),
        ("ok", "off", "unknown", 0),
        ("ok", "off", "nan", 0),
        ("ok", "off", "inf", 0),
        ("ok", "off", 0, -15),  # Stale despite a cached healthy attribute.
        ("ok", "off", 0, 5),  # Future despite a cached healthy attribute.
        ("ok", "unavailable", "unknown", 0),
    ],
)
async def test_unusable_data_breaks_evidence_and_requires_new_recovery_samples(
    hass, shadow, freezer, health, state, poh, offset
):
    await sample(hass, shadow, freezer, 5)
    await sample(hass, shadow, freezer, 10)
    set_observation(
        hass,
        shadow,
        START + timedelta(minutes=10 + offset),
        health=health,
        state=state,
        poh=poh,
    )
    await shadow.run()
    assert clear_count(hass) == -1
    assert hass.states.get(HOLD).state == "on"
    await sample(hass, shadow, freezer, 15)  # Recovery boundary, not evidence.
    assert clear_count(hass) == 0
    await shadow.run()  # The recovery snapshot cannot be reused.
    assert clear_count(hass) == 0
    await sample(hass, shadow, freezer, 20)
    assert clear_count(hass) == 1
    assert not clear_notifications(shadow)
    for minute in range(25, 55, 5):
        await sample(hass, shadow, freezer, minute)
    assert clear_count(hass) == 7
    assert len(clear_notifications(shadow)) == 1
    assert hass.states.get(HOLD).state == "on"


@pytest.mark.parametrize("poh,state", [(1, "off"), (80, "on")])
async def test_same_time_correction_never_advances_clear_count(
    hass, shadow, freezer, poh, state
):
    await sample(hass, shadow, freezer, 5)
    await sample(hass, shadow, freezer, 10)
    set_observation(hass, shadow, START + timedelta(minutes=10), poh=poh, state=state)
    await shadow.run()
    assert clear_count(hass) == -1
    assert not clear_notifications(shadow)
    assert hass.states.get(HOLD).state == "on"


async def test_gap_and_out_of_order_data_do_not_complete_sequence(
    hass, shadow, freezer
):
    await sample(hass, shadow, freezer, 5)
    await sample(hass, shadow, freezer, 10)
    await sample(hass, shadow, freezer, 20)  # Missing 12:15.
    assert clear_count(hass) == -1
    await shadow.run()  # Recovery establishes a new boundary.
    assert clear_count(hass) == 0
    await sample(hass, shadow, freezer, 25)
    assert clear_count(hass) == 1
    set_observation(hass, shadow, START + timedelta(minutes=20))
    await shadow.run()
    assert clear_count(hass) == -1
    assert not clear_notifications(shadow)


@pytest.mark.parametrize("reset", ["startup_or_reload", "removal", "options"])
async def test_restart_reload_options_and_queued_removal_discard_six_samples(
    hass, shadow, freezer, reset
):
    for minute in range(5, 35, 5):
        await sample(hass, shadow, freezer, minute)
    assert clear_count(hass) == 6
    if reset == "startup_or_reload":
        await shadow.run(trigger_id="reset")
    elif reset == "removal":
        # Preserve the removal event even if current states have already recovered.
        await shadow.run(trigger_id="observation", to_state=None)
    else:
        await shadow.run(threshold_percent=85)
    assert clear_count(hass) in (-1, 0)
    assert hass.states.get(HOLD).state == "on"
    await sample(hass, shadow, freezer, 35)
    assert not clear_notifications(shadow)
    assert clear_count(hass) <= 1


async def test_wrong_distance_units_cannot_request_or_certify_clear(
    hass, shadow, freezer
):
    await sample(hass, shadow, freezer, 5)
    variables = shadow.config["variables"].as_dict()
    entity = variables["distance_entity"]
    previous = hass.states.get(entity)
    hass.states.async_set(
        entity, "1.2", {**previous.attributes, "unit_of_measurement": "mi"}
    )
    await shadow.run()
    assert clear_count(hass) == -1
    assert not shadow.notifications


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_integration_option_reload_emits_the_captured_removal_reset(
    hass, shadow, freezer
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MeteoSwiss Rain Radar",
        version=1,
        data={"radius": 5, "threshold": 0.2, "latitude": 0, "longitude": 0},
    )
    entry.add_to_hass(hass)
    rain = RadarResult(MagicMock(), False, None, START)
    hail = HailResult(START, HailAnalysis("ok", False, 0, None, True), 10)
    removals = []
    entity_id = shadow.config["variables"].as_dict()["hail_entity"]

    def state_changed(event):
        state = event.data["new_state"]
        if event.data["entity_id"] == entity_id and (
            state is None or state.state not in ("on", "off")
        ):
            removals.append(event)

    remove_listener = hass.bus.async_listen("state_changed", state_changed)
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
            hass.config_entries.async_update_entry(
                entry, options={"hail_poh_threshold": 85}
            )
            await hass.async_block_till_done()
            assert removals
            await shadow.run(
                trigger_id="observation", to_state=removals[0].data["new_state"]
            )
            assert clear_count(hass) == -1
            assert hass.states.get(HOLD).state == "on"
        finally:
            remove_listener()
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


@pytest.mark.parametrize("change", ["missing", "stale", "clock_reversal"])
async def test_final_notification_rechecks_data_after_helper_writes(
    hass, shadow, freezer, change
):
    for minute in range(5, 35, 5):
        await sample(hass, shadow, freezer, minute)
    changed = []
    observation = START + timedelta(minutes=35)

    def interrupt(event):
        state = event.data["new_state"]
        if (
            event.data["entity_id"] == LAST
            and state.attributes["timestamp"] == observation.timestamp()
        ):
            changed.append(True)
            if change == "missing":
                set_observation(
                    hass, shadow, None, health="missing", state="unknown", poh="unknown"
                )
            else:
                offset = 11 if change == "stale" else -1
                freezer.move_to(observation + timedelta(minutes=offset))

    unsubscribe = hass.bus.async_listen("state_changed", interrupt)
    try:
        await sample(hass, shadow, freezer, 35)
        assert changed
        assert not clear_notifications(shadow)
        assert hass.states.get(HOLD).state == "on"
    finally:
        unsubscribe()


async def test_package_is_disabled_by_default_and_enable_resets_evidence(hass, shadow):
    package = load_yaml(str(EXAMPLE_PATH))
    assert await async_setup_component(hass, "automation", deepcopy(package))
    await hass.async_block_till_done()
    self_id = next(
        trigger["entity_id"]
        for trigger in package["automation"][0]["trigger"]
        if isinstance(trigger.get("entity_id"), str)
        and trigger["entity_id"].startswith("automation.")
    )
    assert hass.states.get(self_id).state == "off"
    assert clear_count(hass) == 0
    assert not shadow.notifications
    # Simulate enabling the isolated test entity, never a live automation.
    hass.states.async_set(COUNT, "6")
    component = hass.data[DATA_COMPONENT]
    try:
        await component.get_entity(self_id).async_turn_on()
        await hass.async_block_till_done()
        assert hass.states.get(self_id).state == "on"
        assert clear_count(hass) == 0
        assert hass.states.get(HOLD).state == "on"
        assert not shadow.notifications
    finally:
        await component.async_remove_entity(self_id)
        await hass.async_block_till_done()
