from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import DOMAIN


class MeteoSwissRadarEntity(
    CoordinatorEntity,
):
    _attr_has_entity_name = True
    _attr_attribution = "Source: MeteoSwiss"

    def __init__(
        self,
        coordinator,
        entry,
    ):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    self._entry.entry_id,
                )
            },
            name="MeteoSwiss Rain Radar",
            manufacturer="MeteoSwiss",
            model="Radar Rain Detection",
            configuration_url="https://github.com/deltaecho07/hass-meteoswiss-rain-radar",
        )

    @property
    def available(self):
        return self.coordinator.last_update_success


class MeteoSwissRainEntity(MeteoSwissRadarEntity):
    """Rain entities: health covers update success and observation age only.

    The legacy rain reader reports no coverage evidence, so rain deliberately has
    no `coverage_complete` attribute and no coverage health state.
    """

    @property
    def extra_state_attributes(self):
        result = self.coordinator.current_result
        return {
            "data_health": result.health,
            "observation": result.last_update,
        }


class MeteoSwissHailEntity(MeteoSwissRadarEntity):
    @property
    def extra_state_attributes(self):
        result = self.coordinator.current_result
        return {
            "data_health": result.analysis.health,
            "coverage_complete": result.analysis.coverage_complete,
            "observation": result.observation,
        }
