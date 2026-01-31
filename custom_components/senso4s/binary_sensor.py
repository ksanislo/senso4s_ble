"""Binary sensor platform for Senso4s integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, AnomalyType
from .coordinator import Senso4sDataUpdateCoordinator

# Sensors available on all models
BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="needs_calibration",
        translation_key="needs_calibration",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:scale-balance",
    ),
    BinarySensorEntityDescription(
        key="has_error",
        translation_key="has_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle",
    ),
    BinarySensorEntityDescription(
        key="has_anomaly",
        translation_key="has_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert",
    ),
    BinarySensorEntityDescription(
        key="low_gas",
        translation_key="low_gas",
        device_class=BinarySensorDeviceClass.GAS,
        icon="mdi:propane-tank-outline",
    ),
)

# Sensors only available on Plus models (which have internal sensors)
PLUS_MODEL_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="temperature_anomaly",
        translation_key="temperature_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:thermometer-alert",
    ),
    BinarySensorEntityDescription(
        key="incline_anomaly",
        translation_key="incline_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:angle-acute",
    ),
    BinarySensorEntityDescription(
        key="motion_anomaly",
        translation_key="motion_anomaly",
        device_class=BinarySensorDeviceClass.VIBRATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:vibrate",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Senso4s binary sensors from a config entry."""
    coordinator: Senso4sDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[Senso4sBinarySensorEntity] = []

    # Add sensors available on all models
    for description in BINARY_SENSOR_DESCRIPTIONS:
        entities.append(Senso4sBinarySensorEntity(coordinator, description))

    # Add Plus-only sensors if this is a Plus model (which has internal sensors)
    # Default to False if we don't have data yet - HA can add sensors later,
    # but removing them requires user interaction
    is_plus = coordinator.data.is_plus_model if coordinator.data.mac_address else False
    if is_plus:
        for description in PLUS_MODEL_SENSOR_DESCRIPTIONS:
            entities.append(Senso4sBinarySensorEntity(coordinator, description))

    async_add_entities(entities)


class Senso4sBinarySensorEntity(BinarySensorEntity):
    """Representation of a Senso4s binary sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: Senso4sDataUpdateCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        self.coordinator = coordinator
        self.entity_description = description

        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(**coordinator.device_info)

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        # Subscribe to coordinator updates
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.coordinator.address}_update",
                self._handle_coordinator_update,
            )
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        data = self.coordinator.data
        key = self.entity_description.key

        if key == "needs_calibration":
            return data.needs_calibration

        if key == "has_error":
            return data.has_error

        if key == "has_anomaly":
            return data.has_anomaly

        if key == "low_gas":
            if data.gas_level_percent >= 0:
                return data.gas_level_percent <= self.coordinator.low_level_threshold
            return None

        if key == "temperature_anomaly":
            return AnomalyType.TEMPERATURE in data.anomalies

        if key == "incline_anomaly":
            return AnomalyType.INCLINE in data.anomalies

        if key == "motion_anomaly":
            return AnomalyType.MOTION in data.anomalies

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        data = self.coordinator.data
        key = self.entity_description.key

        attrs: dict[str, Any] = {}

        if key == "has_error":
            if data.error_code is not None:
                attrs["error_code"] = data.error_code
                attrs["error_description"] = data.error_description

        if key == "has_anomaly":
            if data.anomalies:
                attrs["anomalies"] = data.anomaly_names

        if key == "low_gas":
            attrs["threshold"] = self.coordinator.low_level_threshold

        return attrs
