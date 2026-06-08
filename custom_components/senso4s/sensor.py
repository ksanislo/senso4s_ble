# Copyright 2026 Ken Sanislo
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sensor platform for Senso4s integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, USAGE_MODE_NAMES
from .coordinator import Senso4sDataUpdateCoordinator

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="gas_level",
        translation_key="gas_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,  # Shows similar icon
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:propane-tank",
    ),
    SensorEntityDescription(
        key="gas_remaining",
        translation_key="gas_remaining",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:propane-tank",
    ),
    SensorEntityDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="usage_mode",
        translation_key="usage_mode",
        icon="mdi:fire",
    ),
    SensorEntityDescription(
        key="estimated_empty",
        translation_key="estimated_empty",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
    ),
    SensorEntityDescription(
        key="last_setup",
        translation_key="last_setup",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:calendar-check",
    ),
    SensorEntityDescription(
        key="rssi",
        translation_key="rssi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="empty_weight",
        translation_key="empty_weight",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:weight-kilogram",
    ),
    SensorEntityDescription(
        key="gas_capacity",
        translation_key="gas_capacity",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:propane-tank",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Senso4s sensors from a config entry."""
    coordinator: Senso4sDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[Senso4sSensorEntity] = []

    for description in SENSOR_DESCRIPTIONS:
        entities.append(Senso4sSensorEntity(coordinator, description))

    async_add_entities(entities)


class Senso4sSensorEntity(SensorEntity):
    """Representation of a Senso4s sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: Senso4sDataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.entity_description = description

        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(**coordinator.device_info)

        # Register for updates
        self._update_callback_remove = None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        # Subscribe to coordinator updates via bluetooth callback
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.coordinator.address}_update",
                self._handle_coordinator_update,
            )
        )
        # Re-evaluate availability periodically so entities transition to
        # unavailable when advertisements stop arriving.
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_periodic_refresh,
                timedelta(minutes=5),
            )
        )

    @callback
    def _handle_periodic_refresh(self, _now: Any) -> None:
        """Force HA to re-read the `available` property."""
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        Defers to HA's per-device bluetooth tracker, which counts every advert
        the scanner sees regardless of whether the payload bytes changed.
        Using our own last_seen would falsely flip to unavailable whenever the
        device's reading was steady and habluetooth deduped the broadcasts.
        """
        if not bluetooth.async_address_present(
            self.hass, self.coordinator.address, connectable=False
        ):
            return False
        if self.entity_description.key in ("gas_level", "gas_remaining"):
            return self.coordinator.data.is_available
        return True

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        data = self.coordinator.data
        key = self.entity_description.key

        if key == "gas_level":
            if data.gas_level_percent >= 0:
                return data.gas_level_percent
            return None

        if key == "gas_remaining":
            # History records carry mass in dag (10 g) units. Prefer the most
            # recent history value when available — the advertised level byte
            # is only whole-percent resolution.
            if self.coordinator.history:
                return round(self.coordinator.history[-1].remaining_gas_kg, 2)
            if data.gas_remaining_kg is not None:
                return round(data.gas_remaining_kg, 2)
            return None

        if key == "battery":
            return data.battery_percent

        if key == "usage_mode":
            return USAGE_MODE_NAMES.get(data.usage_mode, str(data.usage_mode))

        if key == "estimated_empty":
            return self.coordinator.estimated_empty_date

        if key == "last_setup":
            return self.coordinator.last_known_setup_date

        if key == "rssi":
            # Read fresh from HA's bluetooth cache — that gets updated on
            # every advert the scanner sees, whereas coordinator.service_info
            # only updates when our callback fires (which habluetooth dedupe
            # suppresses for byte-identical broadcasts).
            info = bluetooth.async_last_service_info(
                self.hass, self.coordinator.address, connectable=False
            )
            if info is not None:
                return info.rssi
            return None

        if key == "empty_weight":
            return self.coordinator.empty_weight_kg

        if key == "gas_capacity":
            return self.coordinator.gas_capacity_kg

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        data = self.coordinator.data
        key = self.entity_description.key

        attrs: dict[str, Any] = {}

        if key == "gas_level":
            # Show weights in user's preferred unit
            unit_suffix = "lb" if self.coordinator.use_pounds else "kg"
            attrs[f"empty_weight_{unit_suffix}"] = self.coordinator.get_display_weight(
                self.coordinator.empty_weight_kg
            )
            attrs[f"gas_capacity_{unit_suffix}"] = self.coordinator.get_display_weight(
                self.coordinator.gas_capacity_kg
            )
            if data.error_code is not None:
                attrs["error_code"] = data.error_code
                attrs["error_description"] = data.error_description
            if data.anomalies:
                attrs["anomalies"] = data.anomaly_names

        if key == "gas_remaining":
            if data.gas_capacity_kg:
                unit_suffix = "lb" if self.coordinator.use_pounds else "kg"
                attrs[f"gas_capacity_{unit_suffix}"] = self.coordinator.get_display_weight(
                    data.gas_capacity_kg
                )

        if key == "usage_mode":
            attrs["is_plus_model"] = data.is_plus_model

        if key == "battery":
            if data.last_seen:
                attrs["last_seen"] = data.last_seen.isoformat()

        if key == "estimated_empty":
            if self.coordinator.history:
                attrs["history_records"] = len(self.coordinator.history)
                attrs["last_history_update"] = (
                    self.coordinator.last_history_update.isoformat()
                    if self.coordinator.last_history_update
                    else None
                )

        return attrs
