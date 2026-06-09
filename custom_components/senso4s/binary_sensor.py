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

"""Binary sensor platform for Senso4s integration."""
from __future__ import annotations

from time import monotonic
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AVAILABILITY_TIMEOUT_MINUTES,
    CONF_IS_PLUS,
    DOMAIN,
    MANUFACTURER,
    AnomalyType,
)
from .coordinator import Senso4sCoordinator
from .models import Senso4sDeviceData


BINARY_SENSOR_DESCRIPTIONS: dict[str, BinarySensorEntityDescription] = {
    "needs_calibration": BinarySensorEntityDescription(
        key="needs_calibration",
        translation_key="needs_calibration",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:scale-balance",
    ),
    "has_error": BinarySensorEntityDescription(
        key="has_error",
        translation_key="has_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle",
    ),
    "low_gas": BinarySensorEntityDescription(
        key="low_gas",
        translation_key="low_gas",
        device_class=BinarySensorDeviceClass.GAS,
        icon="mdi:propane-tank-outline",
    ),
}

# Only meaningful on PLUS models; disabled by default.
PLUS_MODEL_DESCRIPTIONS: dict[str, BinarySensorEntityDescription] = {
    "has_anomaly": BinarySensorEntityDescription(
        key="has_anomaly",
        translation_key="has_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:alert",
    ),
    "temperature_anomaly": BinarySensorEntityDescription(
        key="temperature_anomaly",
        translation_key="temperature_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:thermometer-alert",
    ),
    "incline_anomaly": BinarySensorEntityDescription(
        key="incline_anomaly",
        translation_key="incline_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:angle-acute",
    ),
    "motion_anomaly": BinarySensorEntityDescription(
        key="motion_anomaly",
        translation_key="motion_anomaly",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:vibrate",
    ),
}


def _build_data_update(
    coordinator: Senso4sCoordinator,
    is_plus_entry: bool,
    data: Senso4sDeviceData,
) -> PassiveBluetoothDataUpdate[Any]:
    """Convert the coordinator's snapshot into a PassiveBluetoothDataUpdate."""
    address = coordinator.address
    device_info = DeviceInfo(
        identifiers={(DOMAIN, address)},
        name=coordinator.device_name,
        manufacturer=MANUFACTURER,
        model="Senso4s PLUS" if data.is_plus_model else "Senso4s BASIC",
        configuration_url="https://github.com/ksanislo/senso4s_ble",
    )

    descriptions = dict(BINARY_SENSOR_DESCRIPTIONS)
    if is_plus_entry:
        descriptions.update(PLUS_MODEL_DESCRIPTIONS)

    return PassiveBluetoothDataUpdate(
        devices={None: device_info},
        entity_descriptions={
            PassiveBluetoothEntityKey(key, None): desc
            for key, desc in descriptions.items()
        },
        entity_data={},  # Values are computed live in the entity.
        entity_names={
            PassiveBluetoothEntityKey(key, None): None
            for key in descriptions
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Senso4s binary sensors from a config entry."""
    coordinator: Senso4sCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Driven by entry.data (set at config-flow creation, backfilled on first
    # load for pre-rc8 entries) so it's independent of advert timing.
    is_plus_entry = bool(entry.data.get(CONF_IS_PLUS, False))

    processor = PassiveBluetoothDataProcessor(
        update_method=lambda data: _build_data_update(
            coordinator, is_plus_entry, data
        ),
        restore_key=f"{entry.entry_id}_binary_sensor",
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(
            Senso4sBinarySensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, BinarySensorEntityDescription)
    )

    # Seed an initial update so entities render immediately on cache-warm starts.
    processor.async_handle_update(coordinator.data, was_available=True)


class Senso4sBinarySensorEntity(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[Any, Senso4sDeviceData]
    ],
    BinarySensorEntity,
):
    """Senso4s binary sensor backed by the bluetooth processor framework."""

    def __init__(
        self,
        processor: PassiveBluetoothDataProcessor[Any, Senso4sDeviceData],
        entity_key: PassiveBluetoothEntityKey,
        description: BinarySensorEntityDescription,
        context: Any = None,
    ) -> None:
        super().__init__(processor, entity_key, description, context)
        address = processor.coordinator.address
        # Preserve pre-rc8 unique_id format and add our domain identifier
        # alongside the framework's bluetooth-domain one. See sensor.py.
        self._attr_unique_id = f"{address}_{entity_key.key}"
        merged = dict(self._attr_device_info or {})
        identifiers = set(merged.get("identifiers", set()))
        identifiers.add((DOMAIN, address))
        merged["identifiers"] = identifiers
        self._attr_device_info = merged

    @property
    def _coordinator(self) -> Senso4sCoordinator:
        return self.processor.coordinator  # type: ignore[return-value]

    async def async_added_to_hass(self) -> None:
        # See sensor.py for the rationale.
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_force_state_refresh,
                timedelta(minutes=1),
            )
        )

    @callback
    def _async_force_state_refresh(self, _now: Any) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # See sensor.py for the rationale.
        info = bluetooth.async_last_service_info(
            self.hass, self._coordinator.address, connectable=False
        )
        if info is None:
            return False
        age = monotonic() - info.time
        return age <= AVAILABILITY_TIMEOUT_MINUTES * 60

    @property
    def is_on(self) -> bool | None:
        coord = self._coordinator
        data = coord.data
        key = self.entity_key.key

        if key == "needs_calibration":
            return data.needs_calibration
        if key == "has_error":
            return data.has_error
        if key == "has_anomaly":
            return data.has_anomaly
        if key == "low_gas":
            if 0 <= data.gas_level_percent <= 100:
                return data.gas_level_percent <= coord.low_level_threshold
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
        coord = self._coordinator
        data = coord.data
        key = self.entity_key.key
        attrs: dict[str, Any] = {}

        if key == "has_error":
            if data.error_code is not None:
                attrs["error_code"] = data.error_code
                attrs["error_description"] = data.error_description
        elif key == "has_anomaly":
            if data.anomalies:
                attrs["anomalies"] = data.anomaly_names
        elif key == "low_gas":
            attrs["threshold"] = coord.low_level_threshold

        return attrs
