from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import MeteoSwissRainRadarCoordinator
from .hail_coordinator import MeteoSwissHailCoordinator

type MeteoSwissRainRadarConfigEntry = ConfigEntry[MeteoSwissRainRadarCoordinator]


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
