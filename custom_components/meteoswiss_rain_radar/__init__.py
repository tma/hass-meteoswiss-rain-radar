from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import MeteoSwissRainRadarCoordinator
from .hail_coordinator import MeteoSwissHailCoordinator

type MeteoSwissRainRadarConfigEntry = ConfigEntry[MeteoSwissRainRadarCoordinator]

_LOGGER = logging.getLogger(__name__)


@callback
def async_migrate_rain_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rename legacy default IDs, never identities or user metadata."""
    registry = er.async_get(hass)
    for unique_suffix, old_suffix, new_suffix in (
        ("distance", "distance", "rain_qualifying_distance"),
        ("last_radar", "last_radar_image", "rain_observation"),
    ):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{unique_suffix}"
        )
        if entity_id is None:
            continue
        registered = registry.async_get(entity_id)
        if registered is None or registered.config_entry_id != entry.entry_id:
            continue
        match = re.fullmatch(
            rf"sensor\.{DOMAIN}_{old_suffix}(_(?:[2-9]|[1-9][0-9]+))?", entity_id
        )
        if match is None:
            continue
        new_object_id = f"{DOMAIN}_{new_suffix}"
        preferred_object_id = new_object_id + (match[1] or "")
        new_entity_id = registry.async_generate_entity_id("sensor", preferred_object_id)
        if new_entity_id != f"sensor.{preferred_object_id}":
            # Keep ordinary numeric suffixes, even when the old ID had one already.
            new_entity_id = registry.async_generate_entity_id("sensor", new_object_id)
        registry.async_update_entity(entity_id, new_entity_id=new_entity_id)
        _LOGGER.info("Renamed rain entity %s to %s", entity_id, new_entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissRainRadarConfigEntry,
) -> bool:
    """Set up MeteoSwiss Rain Radar."""

    coordinator = MeteoSwissRainRadarCoordinator(
        hass,
        entry,
    )
    hail = coordinator.hail_coordinator = MeteoSwissHailCoordinator(hass, entry)
    setup_task = asyncio.current_task()

    async def stop_on_shutdown(_event):
        if setup_task is not None:
            # The setup exception handler owns cleanup until setup completes.
            setup_task.cancel()
            try:
                await setup_task
            except asyncio.CancelledError:
                pass
            return
        await hail.stop()
        await coordinator.stop()

    remove_stop_listener = hass.bus.async_listen(
        EVENT_HOMEASSISTANT_STOP, stop_on_shutdown
    )
    try:
        await coordinator.async_config_entry_first_refresh()
        await hail.async_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        entry.runtime_data = coordinator
        async_migrate_rain_entity_ids(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.start()
    except BaseException:
        remove_stop_listener()
        await hail.stop()
        await coordinator.stop()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if hasattr(entry, "runtime_data"):
            del entry.runtime_data
        raise

    setup_task = None
    entry.async_on_unload(remove_stop_listener)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeteoSwissRainRadarConfigEntry,
) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator = entry.runtime_data
    if coordinator.hail_coordinator is not None:
        await coordinator.hail_coordinator.stop()
    await coordinator.stop()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
