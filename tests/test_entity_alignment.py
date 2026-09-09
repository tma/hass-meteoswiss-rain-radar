"""Rain/hail presentation and upgrades through HA's real registry and platforms."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.meteoswiss_rain_radar import async_migrate_rain_entity_ids
from custom_components.meteoswiss_rain_radar.const import DOMAIN
from custom_components.meteoswiss_rain_radar.hail_coordinator import HailResult
from custom_components.meteoswiss_rain_radar.hail_reader import HailAnalysis
from custom_components.meteoswiss_rain_radar.models import RadarResult

from .hail_helpers import OBSERVATION
from .test_hail_setup import MODULE, make_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
PREFIX = "meteoswiss_rain_radar_"
# Registry identity suffix, platform, public name, explicit icon, category.
ENTITIES = (
    ("rain", "binary_sensor", "Rain", "mdi:weather-rainy", None),
    ("distance", "sensor", "Rain qualifying distance", "mdi:map-marker-distance", None),
    ("last_radar", "sensor", "Rain observation", "mdi:clock-outline", "diagnostic"),
    ("hail", "binary_sensor", "Hail", "mdi:weather-hail", None),
    ("hail_max_poh", "sensor", "Hail maximum POH", None, None),
    (
        "hail_distance",
        "sensor",
        "Hail qualifying distance",
        "mdi:map-marker-distance",
        None,
    ),
    (
        "hail_observation",
        "sensor",
        "Hail observation",
        "mdi:clock-outline",
        "diagnostic",
    ),
    ("hail_age", "sensor", "Hail data age", None, "diagnostic"),
    ("hail_health", "sensor", "Hail data health", None, "diagnostic"),
)
LEGACY_NAMES = {"distance": "Distance", "last_radar": "Last Radar Image"}


@pytest.fixture
async def load_entry(hass, freezer):
    freezer.move_to(OBSERVATION)
    loaded = []

    async def load(entry=None):
        entry = entry or make_entry(hass)
        loaded.append(entry)
        # HA's first domain setup also loads other entries already in the registry.
        if entry.state is ConfigEntryState.NOT_LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        return entry

    with (
        patch(
            f"{MODULE}.coordinator.MeteoSwissRainRadarCoordinator._async_update_data",
            AsyncMock(return_value=RadarResult(MagicMock(), True, 2.5, OBSERVATION)),
        ),
        patch(
            f"{MODULE}.hail_coordinator.MeteoSwissHailCoordinator._async_update_data",
            AsyncMock(
                return_value=HailResult(
                    OBSERVATION, HailAnalysis("ok", True, 80, 1, True), 10
                )
            ),
        ),
    ):
        yield load
        for entry in loaded:
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def registry_entries(hass, entry):
    return {
        entity.unique_id.removeprefix(f"{entry.entry_id}_"): entity
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


def seed_legacy_entities(hass, entry):
    registry = er.async_get(hass)
    for suffix, domain, name, _, _ in ENTITIES:
        name = LEGACY_NAMES.get(suffix, name)
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{suffix}",
            config_entry=entry,
            suggested_object_id=PREFIX + name.lower().replace(" ", "_"),
            original_name=name,
            has_entity_name=True,
        )
    return registry_entries(hass, entry)


@pytest.mark.parametrize(
    "units", [METRIC_SYSTEM, US_CUSTOMARY_SYSTEM], ids=["metric", "imperial"]
)
async def test_new_entity_interface_and_legacy_rain_availability(
    hass, load_entry, units
):
    hass.config.units = units
    entry = await load_entry()
    entities = registry_entries(hass, entry)
    assert len(entities) == 9
    for suffix, domain, name, icon, category in ENTITIES:
        entity = entities[suffix]
        assert entity.entity_id == f"{domain}.{PREFIX}{name.lower().replace(' ', '_')}"
        assert entity.original_name == name
        assert entity.original_icon == icon
        assert entity.entity_category == category
        state = hass.states.get(entity.entity_id)
        assert state.attributes["friendly_name"] == f"MeteoSwiss Rain Radar {name}"
        assert state.attributes.get("icon") == icon
    distance = hass.states.get(entities["distance"].entity_id)
    assert distance.state == "2.5"
    assert distance.attributes["unit_of_measurement"] == "km"
    assert "device_class" not in distance.attributes
    rain = entry.runtime_data
    rain.async_set_update_error(RuntimeError("rain outage"))
    rain.hail_coordinator.async_set_update_error(RuntimeError("hail outage"))
    await hass.async_block_till_done()
    assert hass.states.get(entities["last_radar"].entity_id).state == "unavailable"
    assert entities["last_radar"].entity_category == EntityCategory.DIAGNOSTIC
    assert (
        hass.states.get(entities["hail_observation"].entity_id).state
        == OBSERVATION.isoformat()
    )
    assert hass.states.get(entities["hail"].entity_id).state == "unavailable"
    assert hass.states.get(entities["hail_health"].entity_id).state == "error"


async def test_upgrade_renames_exactly_two_ids_and_reload_is_idempotent(
    hass, load_entry
):
    entry = make_entry(hass)
    before = seed_legacy_entities(hass, entry)
    renames = []
    remove_listener = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: (
            renames.append(event.data) if "old_entity_id" in event.data else None
        ),
    )
    try:
        await load_entry(entry)
        after = registry_entries(hass, entry)
        assert len(before) == len(after) == 9
        for suffix, entity in before.items():
            assert after[suffix].id == entity.id
            assert after[suffix].unique_id == entity.unique_id
            assert (after[suffix].entity_id != entity.entity_id) == (
                suffix in LEGACY_NAMES
            )
        assert {(event["old_entity_id"], event["entity_id"]) for event in renames} == {
            (f"sensor.{PREFIX}distance", f"sensor.{PREFIX}rain_qualifying_distance"),
            (f"sensor.{PREFIX}last_radar_image", f"sensor.{PREFIX}rain_observation"),
        }
        assert all(
            hass.states.get(before[key].entity_id) is None for key in LEGACY_NAMES
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert len(renames) == 2
        assert {key: item.entity_id for key, item in after.items()} == {
            key: item.entity_id for key, item in registry_entries(hass, entry).items()
        }
        assert entry.version == 1 and entry.options == {}
    finally:
        remove_listener()


@pytest.mark.parametrize("collision", [False, True])
async def test_two_entries_keep_numeric_ids_without_overwriting_collisions(
    hass, load_entry, collision
):
    registry = er.async_get(hass)
    entries = [make_entry(hass), make_entry(hass)]
    before = [seed_legacy_entities(hass, entry) for entry in entries]
    occupied = []
    if collision:
        for suffix in ("rain_qualifying_distance", "rain_observation"):
            for number in ("", "_2"):
                occupied.append(
                    registry.async_get_or_create(
                        "sensor",
                        "unrelated",
                        suffix + number,
                        suggested_object_id=PREFIX + suffix + number,
                    )
                )
        # HA also reserves IDs for states with no registry entry.
        hass.states.async_set(f"sensor.{PREFIX}rain_observation_3", "reserved")
    for index, entry in enumerate(entries):
        await load_entry(entry)
        after = registry_entries(hass, entry)
        for key, suffix in (
            ("distance", "rain_qualifying_distance"),
            ("last_radar", "rain_observation"),
        ):
            number = index + 1 + (2 if collision else 0)
            if collision and key == "last_radar":
                number += 1
            ending = f"_{number}" if number > 1 else ""
            assert after[key].entity_id == f"sensor.{PREFIX}{suffix}{ending}"
            assert after[key].id == before[index][key].id
        assert len(after) == 9
    for entity in occupied:
        assert registry.async_get(entity.entity_id) == entity
    if collision:
        assert hass.states.get(f"sensor.{PREFIX}rain_observation_3").state == "reserved"


@pytest.mark.parametrize("custom_id", [False, True])
async def test_custom_names_ids_and_disabled_metadata_survive(
    hass, load_entry, custom_id
):
    entry = make_entry(hass)
    registry = er.async_get(hass)
    before = seed_legacy_entities(hass, entry)
    area = ar.async_get(hass).async_create("Test area")
    metadata = {
        "name": "My radar metric",
        "icon": "mdi:umbrella",
        "area_id": area.id,
        "aliases": {"my radar alias"},
        "labels": {"test-label"},
        "categories": {"sensor": "test-category"},
        "hidden_by": er.RegistryEntryHider.USER,
    }
    for key in LEGACY_NAMES:
        entity = before[key]
        updates = dict(metadata)
        if custom_id:
            updates["new_entity_id"] = f"sensor.my_radar_{key}"
        if key == "last_radar":
            updates["disabled_by"] = er.RegistryEntryDisabler.USER
        updated = registry.async_update_entity(entity.entity_id, **updates)
        before[key] = registry.async_update_entity_options(
            updated.entity_id, "sensor", {"display_precision": 2}
        )
    await load_entry(entry)
    after = registry_entries(hass, entry)
    for key in LEGACY_NAMES:
        entity = after[key]
        assert entity.id == before[key].id
        assert entity.unique_id == before[key].unique_id
        assert entity.options == before[key].options
        for attribute, value in metadata.items():
            assert getattr(entity, attribute) == value
        if custom_id:
            assert entity.entity_id == before[key].entity_id
        else:
            assert entity.entity_id != before[key].entity_id
    assert after["last_radar"].disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(after["last_radar"].entity_id) is None
    state = hass.states.get(after["distance"].entity_id)
    assert state.attributes["friendly_name"] == "My radar metric"
    assert state.attributes["icon"] == "mdi:umbrella"


async def test_migration_excludes_wrong_owner_platform_domain_identity_and_custom_ids(
    hass,
):
    registry = er.async_get(hass)
    owner = make_entry(hass)
    candidates = []
    for domain, platform, suffix, object_id, wrong_owner in (
        ("sensor", DOMAIN, "distance", PREFIX + "distance", True),
        ("sensor", "unrelated", "distance", PREFIX + "distance", False),
        ("binary_sensor", DOMAIN, "distance", PREFIX + "distance", False),
        ("sensor", DOMAIN, "unrelated", PREFIX + "distance", False),
        ("sensor", DOMAIN, "distance", PREFIX + "distance_1", False),
        ("sensor", DOMAIN, "distance", PREFIX + "distance_02", False),
        ("sensor", DOMAIN, "distance", PREFIX + "distance_backup", False),
    ):
        entry = make_entry(hass)
        entity = registry.async_get_or_create(
            domain,
            platform,
            f"{entry.entry_id}_{suffix}",
            config_entry=owner if wrong_owner else entry,
            suggested_object_id=object_id,
        )
        candidates.append((entry, entity))
    for entry, entity in candidates:
        async_migrate_rain_entity_ids(hass, entry)
        assert registry.async_get(entity.entity_id) == entity
