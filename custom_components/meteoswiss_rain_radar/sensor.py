from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfTime

from .const import DOMAIN
from .entity import MeteoSwissHailEntity, MeteoSwissRainEntity
from .models import RAIN_HEALTH


@dataclass(frozen=True, kw_only=True)
class RainSensorEntityDescription(SensorEntityDescription):
    """Name rain metrics explicitly while retaining their registry identities."""

    unique_id_suffix: str


RAIN_SENSORS = (
    RainSensorEntityDescription(
        key="rain_distance",
        unique_id_suffix="distance",
        name="Rain qualifying distance",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        # No distance device class: preserve legacy km display on imperial systems.
        icon="mdi:map-marker-distance",
    ),
    RainSensorEntityDescription(
        key="rain_observation",
        unique_id_suffix="last_radar",
        name="Rain observation",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-outline",
    ),
    RainSensorEntityDescription(
        key="rain_age",
        unique_id_suffix="rain_age",
        name="Rain data age",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    RainSensorEntityDescription(
        key="rain_health",
        unique_id_suffix="rain_health",
        name="Rain data health",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=list(RAIN_HEALTH),
    ),
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
        icon="mdi:map-marker-distance",
    ),
    SensorEntityDescription(
        key="hail_observation",
        name="Hail observation",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-outline",
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


class RainSensor(MeteoSwissRainEntity, SensorEntity):
    entity_description: RainSensorEntityDescription

    def __init__(self, coordinator, entry, description):
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.unique_id_suffix}"

    @property
    def available(self):
        if self.entity_description.entity_category == EntityCategory.DIAGNOSTIC:
            return True
        return super().available

    @property
    def native_value(self):
        result = self.coordinator.current_result
        match self.entity_description.key:
            case "rain_distance":
                return result.distance_km
            case "rain_observation":
                return result.last_update
            case "rain_age":
                return result.age_minutes(datetime.now(UTC))
            case "rain_health":
                return result.health
        return None


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
            *(
                RainSensor(coordinator, entry, description)
                for description in RAIN_SENSORS
            ),
            *(
                HailSensor(coordinator.hail_coordinator, entry, description)
                for description in HAIL_SENSORS
            ),
        ]
    )
