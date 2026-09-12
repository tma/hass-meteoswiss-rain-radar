from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)

from .const import DOMAIN
from .entity import MeteoSwissHailEntity, MeteoSwissRainEntity


class RainBinarySensor(MeteoSwissRainEntity, BinarySensorEntity):
    _attr_name = "Rain"
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rain"

    @property
    def is_on(self):
        # Unusable data is unknown, never a claim of no rain.
        return self.coordinator.current_result.rain


class HailBinarySensor(MeteoSwissHailEntity, BinarySensorEntity):
    _attr_name = "Hail"
    _attr_icon = "mdi:weather-hail"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_hail"

    @property
    def is_on(self):
        return self.coordinator.current_result.analysis.detected


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RainBinarySensor(coordinator, entry),
            HailBinarySensor(coordinator.hail_coordinator, entry),
        ]
    )
