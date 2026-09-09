from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfTime

from .const import DOMAIN
from .entity import MeteoSwissHailEntity, MeteoSwissRainRadarEntity


class DistanceSensor(
    MeteoSwissRainRadarEntity,
    SensorEntity,
):
    _attr_name = "Distance"

    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(
        self,
        coordinator,
        entry,
    ):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_distance"

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.distance_km


class LastUpdateSensor(
    MeteoSwissRainRadarEntity,
    SensorEntity,
):
    _attr_name = "Last Radar Image"

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_radar"

    @property
    def native_value(self):
        return (
            self.coordinator.data.last_update
            if self.coordinator.data is not None
            else None
        )


HAIL_SENSORS = (
    SensorEntityDescription(
        key="hail_max_poh",
        name="Hail maximum POH",
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="hail_distance",
        name="Hail qualifying distance",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
    ),
    SensorEntityDescription(
        key="hail_observation",
        name="Hail observation",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="hail_age",
        name="Hail data age",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="hail_health",
        name="Hail data health",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[
            "ok",
            "partial_coverage",
            "outside_grid",
            "no_cells",
            "all_nodata",
            "empty",
            "off_season",
            "missing",
            "stale",
            "future",
            "error",
        ],
    ),
)


class HailSensor(MeteoSwissHailEntity, SensorEntity):
    def __init__(self, coordinator, entry, description):
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def available(self):
        if self.entity_description.entity_category == EntityCategory.DIAGNOSTIC:
            return True
        return super().available

    @property
    def native_value(self):
        result = self.coordinator.current_result
        match self.entity_description.key:
            case "hail_max_poh":
                return result.analysis.max_poh
            case "hail_distance":
                return result.analysis.distance_km
            case "hail_observation":
                return result.observation
            case "hail_age":
                return result.age_minutes(datetime.now(UTC))
            case "hail_health":
                return result.analysis.health
        return None


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            DistanceSensor(
                coordinator,
                entry,
            ),
            LastUpdateSensor(
                coordinator,
                entry,
            ),
            *(
                HailSensor(coordinator.hail_coordinator, entry, description)
                for description in HAIL_SENSORS
            ),
        ]
    )
