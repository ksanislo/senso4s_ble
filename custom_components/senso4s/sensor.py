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
from time import monotonic
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfMass,
)
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AVAILABILITY_TIMEOUT_MINUTES,
    DOMAIN,
    MANUFACTURER,
    USAGE_MODE_NAMES,
)
from .coordinator import Senso4sCoordinator
from .models import Senso4sDeviceData


# Keys here are referenced from the entity_registry; do not rename.
SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    "gas_level": SensorEntityDescription(
        key="gas_level",
        translation_key="gas_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:propane-tank",
    ),
    "gas_remaining": SensorEntityDescription(
        key="gas_remaining",
        translation_key="gas_remaining",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:propane-tank",
    ),
    "battery": SensorEntityDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "usage_mode": SensorEntityDescription(
        key="usage_mode",
        translation_key="usage_mode",
        icon="mdi:fire",
    ),
    "estimated_empty": SensorEntityDescription(
        key="estimated_empty",
        translation_key="estimated_empty",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
    ),
    "last_setup": SensorEntityDescription(
        key="last_setup",
        translation_key="last_setup",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:calendar-check",
    ),
    "rssi": SensorEntityDescription(
        key="rssi",
        translation_key="rssi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    "empty_weight": SensorEntityDescription(
        key="empty_weight",
        translation_key="empty_weight",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:weight-kilogram",
    ),
    "gas_capacity": SensorEntityDescription(
        key="gas_capacity",
        translation_key="gas_capacity",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:propane-tank",
    ),
}

# Keys whose value lives in processor.entity_data. The rest are computed
# live in the entity from coordinator state.
ADVERT_VALUE_KEYS: tuple[str, ...] = ("gas_level", "battery", "usage_mode")


def _build_data_update(
    coordinator: Senso4sCoordinator,
    data: Senso4sDeviceData,
) -> PassiveBluetoothDataUpdate[Any]:
    """Convert a Senso4sDeviceData snapshot into a PassiveBluetoothDataUpdate."""
    address = coordinator.address
    device_info = DeviceInfo(
        identifiers={(DOMAIN, address)},
        name=coordinator.device_name,
        manufacturer=MANUFACTURER,
        model="Senso4s PLUS" if data.is_plus_model else "Senso4s BASIC",
        configuration_url="https://github.com/ksanislo/senso4s_ble",
    )

    entity_data: dict[PassiveBluetoothEntityKey, Any] = {}
    if data.gas_level_percent is not None and 0 <= data.gas_level_percent <= 100:
        entity_data[PassiveBluetoothEntityKey("gas_level", None)] = data.gas_level_percent
    else:
        entity_data[PassiveBluetoothEntityKey("gas_level", None)] = None
    entity_data[PassiveBluetoothEntityKey("battery", None)] = data.battery_percent
    entity_data[PassiveBluetoothEntityKey("usage_mode", None)] = USAGE_MODE_NAMES.get(
        data.usage_mode, str(data.usage_mode)
    )

    return PassiveBluetoothDataUpdate(
        devices={None: device_info},
        entity_descriptions={
            PassiveBluetoothEntityKey(key, None): desc
            for key, desc in SENSOR_DESCRIPTIONS.items()
        },
        entity_data=entity_data,
        entity_names={
            PassiveBluetoothEntityKey(key, None): None
            for key in SENSOR_DESCRIPTIONS
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Senso4s sensors from a config entry."""
    coordinator: Senso4sCoordinator = hass.data[DOMAIN][entry.entry_id]

    processor = PassiveBluetoothDataProcessor(
        update_method=lambda data: _build_data_update(coordinator, data),
        restore_key=entry.entry_id,
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(
            Senso4sSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )

    # Seed an initial update so entities render immediately on cache-warm starts.
    processor.async_handle_update(coordinator.data, was_available=True)


class Senso4sSensorEntity(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[Any, Senso4sDeviceData]
    ],
    SensorEntity,
):
    """Senso4s sensor backed by the bluetooth processor framework."""

    def __init__(
        self,
        processor: PassiveBluetoothDataProcessor[Any, Senso4sDeviceData],
        entity_key: PassiveBluetoothEntityKey,
        description: SensorEntityDescription,
        context: Any = None,
    ) -> None:
        super().__init__(processor, entity_key, description, context)
        address = processor.coordinator.address
        # Preserve the underscore unique_id format from pre-rc8 entries.
        self._attr_unique_id = f"{address}_{entity_key.key}"
        # Union our domain into identifiers so pre-rc8 device registry rows
        # are still found alongside the framework's bluetooth-domain entry.
        merged = dict(self._attr_device_info or {})
        identifiers = set(merged.get("identifiers", set()))
        identifiers.add((DOMAIN, address))
        merged["identifiers"] = identifiers
        self._attr_device_info = merged

    @property
    def _coordinator(self) -> Senso4sCoordinator:
        return self.processor.coordinator  # type: ignore[return-value]

    async def async_added_to_hass(self) -> None:
        # Periodic state-refresh ticks so `available` (which reads the BLE
        # cache directly) gets re-evaluated even when habluetooth has
        # nothing new to dispatch. RSSI uses a longer interval because it
        # actually changes value on every tick.
        await super().async_added_to_hass()
        interval = (
            timedelta(minutes=5)
            if self.entity_key.key == "rssi"
            else timedelta(minutes=1)
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_force_state_refresh,
                interval,
            )
        )

    @callback
    def _async_force_state_refresh(self, _now: Any) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Custom threshold (AVAILABILITY_TIMEOUT_MINUTES); habluetooth's
        # auto-tuned 60-second tracker is too aggressive for a device whose
        # cycle is 15 min. Read the scanner cache directly so dedupe doesn't
        # mask a still-alive radio.
        info = bluetooth.async_last_service_info(
            self.hass, self._coordinator.address, connectable=False
        )
        if info is None:
            return False
        age = monotonic() - info.time
        return age <= AVAILABILITY_TIMEOUT_MINUTES * 60

    @property
    def native_value(self) -> Any:
        # Advert-derived values come from the processor; everything else is
        # read live from the coordinator (history, bluetooth cache, config).
        key = self.entity_key.key
        coord = self._coordinator

        if key in ADVERT_VALUE_KEYS:
            return self.processor.entity_data.get(self.entity_key)

        if key == "gas_remaining":
            if coord.history:
                return round(coord.history[-1].remaining_gas_kg, 2)
            if coord.data.gas_remaining_kg is not None:
                return round(coord.data.gas_remaining_kg, 2)
            return None

        if key == "estimated_empty":
            return coord.estimated_empty_date

        if key == "last_setup":
            return coord.last_known_setup_date

        if key == "rssi":
            info = bluetooth.async_last_service_info(
                self.hass, coord.address, connectable=False
            )
            if info is not None:
                return info.rssi
            return None

        if key == "empty_weight":
            return coord.empty_weight_kg

        if key == "gas_capacity":
            return coord.gas_capacity_kg

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self._coordinator
        data = coord.data
        key = self.entity_key.key
        attrs: dict[str, Any] = {}

        if key == "gas_level":
            unit_suffix = "lb" if coord.use_pounds else "kg"
            attrs[f"empty_weight_{unit_suffix}"] = coord.get_display_weight(
                coord.empty_weight_kg
            )
            attrs[f"gas_capacity_{unit_suffix}"] = coord.get_display_weight(
                coord.gas_capacity_kg
            )
            if data.error_code is not None:
                attrs["error_code"] = data.error_code
                attrs["error_description"] = data.error_description
            if data.anomalies:
                attrs["anomalies"] = data.anomaly_names

        elif key == "gas_remaining":
            if data.gas_capacity_kg:
                unit_suffix = "lb" if coord.use_pounds else "kg"
                attrs[f"gas_capacity_{unit_suffix}"] = coord.get_display_weight(
                    data.gas_capacity_kg
                )

        elif key == "usage_mode":
            attrs["is_plus_model"] = data.is_plus_model

        elif key == "battery":
            if data.last_seen:
                attrs["last_seen"] = data.last_seen.isoformat()

        elif key == "estimated_empty":
            if coord.history:
                attrs["history_records"] = len(coord.history)
                attrs["last_history_update"] = (
                    coord.last_history_update.isoformat()
                    if coord.last_history_update
                    else None
                )

        return attrs
