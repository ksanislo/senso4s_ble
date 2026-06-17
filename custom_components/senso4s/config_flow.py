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

"""Config flow for Senso4s integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.util import dt as dt_util

from .ble_client import Senso4sBLEClient
from .const import (
    ANOMALY_NAMES,
    CONF_EMPTY_WEIGHT,
    CONF_ENABLE_HISTORY_POLLING,
    CONF_GAS_CAPACITY,
    CONF_HISTORY_POLL_INTERVAL,
    CONF_IS_PLUS,
    CONF_LAST_SETUP_DATE,
    CONF_LOW_LEVEL_THRESHOLD,
    CONF_USAGE_MODE,
    CONF_WEIGHT_UNIT,
    DEFAULT_EMPTY_WEIGHT,
    DEFAULT_GAS_CAPACITY,
    DEFAULT_HISTORY_POLL_INTERVAL,
    DEFAULT_LOW_LEVEL_THRESHOLD,
    DEFAULT_USAGE_MODE,
    DEFAULT_WEIGHT_UNIT,
    DEVICE_NAME,
    DOMAIN,
    MANUFACTURER_IDS,
    UNIT_KG,
    UNIT_LB,
    USAGE_MODE_NAMES,
    UsageMode,
    kg_to_lb,
    lb_to_kg,
)
from .coordinator import process_service_info
from .models import CylinderConfig

_LOGGER = logging.getLogger(__name__)


def _is_senso4s_device(service_info: BluetoothServiceInfoBleak) -> bool:
    """Check if a service info is from a Senso4s device."""
    matched_mfr_id = None
    matched_name = False

    for mfr_id in service_info.manufacturer_data:
        if mfr_id in MANUFACTURER_IDS:
            matched_mfr_id = mfr_id
            break

    if service_info.name and service_info.name.upper() == DEVICE_NAME:
        matched_name = True

    if matched_mfr_id is not None or matched_name:
        _LOGGER.debug(
            "[%s] Senso4s device detected - name: %s, "
            "matched_by_mfr_id: %s, matched_by_name: %s",
            service_info.address,
            service_info.name,
            f"{matched_mfr_id} (0x{matched_mfr_id:04X})" if matched_mfr_id else None,
            matched_name,
        )
        return True

    return False


class Senso4sConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Senso4s."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._client: Optional[Senso4sBLEClient] = None
        # User-entered config for unconfigured devices
        self._user_config: dict[str, Any] = {}

    async def _cleanup_client(self) -> None:
        """Clean up BLE client connection."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        _LOGGER.debug(
            "[%s] Bluetooth discovery triggered (name: %s)",
            discovery_info.address,
            discovery_info.name,
        )

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not _is_senso4s_device(discovery_info):
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info

        self.context["title_placeholders"] = {
            "name": discovery_info.name,
            "address": discovery_info.address,
        }

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm the discovered device, then run setup."""
        assert self._discovery_info is not None

        if user_input is None:
            return self.async_show_form(
                step_id="bluetooth_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "name": self._discovery_info.name,
                    "address": self._discovery_info.address,
                },
            )

        # User confirmed — connect to device and check if configured
        self._client = Senso4sBLEClient(self._discovery_info)

        try:
            if not await self._client.connect():
                await self._cleanup_client()
                return self.async_abort(reason="cannot_connect")

            # Check if device has been configured
            is_configured = await self._client.is_device_configured()

            if is_configured is None:
                # Failed to read
                await self._cleanup_client()
                return self.async_abort(reason="cannot_connect")

            if is_configured:
                # Device is already configured - read config and auto-add
                config = await self._client.read_config()
                setup_date = await self._client.read_setup_date()
                await self._cleanup_client()

                if config and config.gas_capacity_kg > 0:
                    _LOGGER.info(
                        "[%s] Auto-adding configured device: "
                        "empty_weight=%.2f kg, gas_capacity=%.2f kg, mode=%s",
                        self._discovery_info.address,
                        config.empty_weight_kg,
                        config.gas_capacity_kg,
                        config.usage_mode.name,
                    )

                    device_data = process_service_info(self._discovery_info)
                    data = {
                        CONF_ADDRESS: self._discovery_info.address,
                        CONF_EMPTY_WEIGHT: round(config.empty_weight_kg, 2),
                        CONF_GAS_CAPACITY: round(config.gas_capacity_kg, 2),
                        CONF_USAGE_MODE: config.usage_mode.value,
                        CONF_LOW_LEVEL_THRESHOLD: DEFAULT_LOW_LEVEL_THRESHOLD,
                        CONF_WEIGHT_UNIT: DEFAULT_WEIGHT_UNIT,
                        CONF_HISTORY_POLL_INTERVAL: DEFAULT_HISTORY_POLL_INTERVAL,
                        CONF_IS_PLUS: bool(
                            device_data and device_data.is_plus_model
                        ),
                    }
                    if setup_date:
                        data[CONF_LAST_SETUP_DATE] = setup_date.isoformat()

                    return self.async_create_entry(
                        title=f"Senso4s ({self._discovery_info.address[-5:].replace(':', '')})",
                        data=data,
                    )
                else:
                    # Config read failed but device says configured - use defaults
                    _LOGGER.warning(
                        "[%s] Device reports configured but config read failed",
                        self._discovery_info.address,
                    )
                    await self._cleanup_client()
                    return self.async_abort(reason="cannot_connect")

            else:
                # Device is NOT configured - need guided setup
                _LOGGER.info(
                    "[%s] Device needs initial setup (setup date is all zeros)",
                    self._discovery_info.address,
                )
                # Keep client connected for the setup flow
                return await self.async_step_needs_setup()

        except Exception as err:
            _LOGGER.exception("[%s] Error during device check: %s", self._discovery_info.address, err)
            await self._cleanup_client()
            return self.async_abort(reason="cannot_connect")

    async def async_step_needs_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step for unconfigured device - collect cylinder configuration."""
        assert self._discovery_info is not None

        if user_input is not None:
            # Store user config for later
            weight_unit = user_input[CONF_WEIGHT_UNIT]
            empty_weight = user_input[CONF_EMPTY_WEIGHT]
            gas_capacity = user_input[CONF_GAS_CAPACITY]

            # Convert to kg if entered in lb
            if weight_unit == UNIT_LB:
                empty_weight = lb_to_kg(empty_weight)
                gas_capacity = lb_to_kg(gas_capacity)

            self._user_config = {
                CONF_EMPTY_WEIGHT: round(empty_weight, 2),
                CONF_GAS_CAPACITY: round(gas_capacity, 2),
                CONF_USAGE_MODE: user_input[CONF_USAGE_MODE],
                CONF_WEIGHT_UNIT: weight_unit,
            }

            return await self.async_step_remove_cylinder()

        # Get device data to check if it's a Plus model
        device_data = process_service_info(self._discovery_info)
        is_plus = device_data.is_plus_model if device_data else False

        usage_options = {
            mode.value: name
            for mode, name in USAGE_MODE_NAMES.items()
            if is_plus or mode != UsageMode.CARAVANNING
        }

        weight_unit_options = {
            UNIT_KG: "Kilograms (kg)",
            UNIT_LB: "Pounds (lb)",
        }

        return self.async_show_form(
            step_id="needs_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_WEIGHT_UNIT, default=DEFAULT_WEIGHT_UNIT
                    ): vol.In(weight_unit_options),
                    vol.Required(
                        CONF_EMPTY_WEIGHT, default=DEFAULT_EMPTY_WEIGHT
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_GAS_CAPACITY, default=DEFAULT_GAS_CAPACITY
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_USAGE_MODE, default=DEFAULT_USAGE_MODE
                    ): vol.In(usage_options),
                }
            ),
            description_placeholders={
                "name": self._discovery_info.name,
                "address": self._discovery_info.address,
            },
        )

    async def async_step_remove_cylinder(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Tell user to remove cylinder before calibration."""
        if user_input is not None:
            return await self.async_step_calibrating()

        return self.async_show_form(step_id="remove_cylinder")

    async def async_step_calibrating(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Perform calibration."""
        assert self._discovery_info is not None

        # Ensure we're still connected
        if self._client is None or not self._client.is_connected:
            self._client = Senso4sBLEClient(self._discovery_info)
            if not await self._client.connect():
                await self._cleanup_client()
                return self.async_abort(reason="connection_failed")

        try:
            success, anomalies = await self._client.calibrate()
            if not success:
                await self._cleanup_client()
                return self.async_abort(reason="calibration_failed")

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                _LOGGER.info("[%s] Calibration completed with anomalies: %s", self._discovery_info.address, anomaly_names)

            return await self.async_step_replace_cylinder()

        except Exception as err:
            _LOGGER.exception("[%s] Error during calibration: %s", self._discovery_info.address, err)
            await self._cleanup_client()
            return self.async_abort(reason="calibration_failed")

    async def async_step_replace_cylinder(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Tell user to replace cylinder after calibration."""
        if user_input is not None:
            return await self.async_step_write_config()

        return self.async_show_form(step_id="replace_cylinder")

    async def async_step_write_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Write configuration to device."""
        assert self._discovery_info is not None

        if self._client is None or not self._client.is_connected:
            await self._cleanup_client()
            return self.async_abort(reason="connection_lost")

        try:
            # Write cylinder config
            success = await self._client.write_config(
                self._user_config[CONF_EMPTY_WEIGHT],
                self._user_config[CONF_GAS_CAPACITY],
                UsageMode.from_value(self._user_config[CONF_USAGE_MODE]),
            )
            if not success:
                await self._cleanup_client()
                return self.async_abort(reason="config_write_failed")

            # Write setup date
            setup_date = dt_util.now()
            success = await self._client.write_setup_date(setup_date)
            if not success:
                await self._cleanup_client()
                return self.async_abort(reason="setup_date_write_failed")

            # Store setup date for later
            self._user_config[CONF_LAST_SETUP_DATE] = setup_date.isoformat()

            return await self.async_step_verify()

        except Exception as err:
            _LOGGER.exception("[%s] Error writing config: %s", self._discovery_info.address, err)
            await self._cleanup_client()
            return self.async_abort(reason="config_write_failed")

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Verify calibration by waiting for valid level."""
        assert self._discovery_info is not None

        if self._client is None or not self._client.is_connected:
            await self._cleanup_client()
            return self.async_abort(reason="connection_lost")

        try:
            success, level, anomalies = await self._client.wait_for_valid_level()

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                _LOGGER.warning("[%s] Verification reported anomalies: %s", self._discovery_info.address, anomaly_names)
                await self._cleanup_client()
                return self.async_abort(
                    reason="verification_anomaly",
                    description_placeholders={"anomalies": ", ".join(anomaly_names)},
                )

            if not success:
                await self._cleanup_client()
                return self.async_abort(reason="verification_timeout")

            _LOGGER.info(
                "[%s] Setup successful, level: %d%%",
                self._discovery_info.address,
                level,
            )

            await self._cleanup_client()

            # Create the config entry
            device_data = process_service_info(self._discovery_info)
            return self.async_create_entry(
                title=f"Senso4s ({self._discovery_info.address[-5:].replace(':', '')})",
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_EMPTY_WEIGHT: self._user_config[CONF_EMPTY_WEIGHT],
                    CONF_GAS_CAPACITY: self._user_config[CONF_GAS_CAPACITY],
                    CONF_USAGE_MODE: self._user_config[CONF_USAGE_MODE],
                    CONF_LOW_LEVEL_THRESHOLD: DEFAULT_LOW_LEVEL_THRESHOLD,
                    CONF_WEIGHT_UNIT: self._user_config[CONF_WEIGHT_UNIT],
                    CONF_HISTORY_POLL_INTERVAL: DEFAULT_HISTORY_POLL_INTERVAL,
                    CONF_LAST_SETUP_DATE: self._user_config.get(CONF_LAST_SETUP_DATE, ""),
                    CONF_IS_PLUS: bool(
                        device_data and device_data.is_plus_model
                    ),
                },
            )

        except Exception as err:
            _LOGGER.exception("[%s] Error during verification: %s", self._discovery_info.address, err)
            await self._cleanup_client()
            return self.async_abort(reason="verification_failed")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._discovery_info = self._discovered_devices[address]
            # User picked this device explicitly — skip the confirm form
            # and proceed straight to the setup work.
            return await self.async_step_bluetooth_confirm(user_input={})

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass):
            if discovery_info.address in current_addresses:
                continue
            if _is_senso4s_device(discovery_info):
                self._discovered_devices[discovery_info.address] = discovery_info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        device_options = {}
        for address, info in self._discovered_devices.items():
            rssi = getattr(info, "rssi", None)
            rssi_part = f" · {rssi} dBm" if rssi is not None else ""
            device_options[address] = f"{info.name} ({address}){rssi_part}"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(device_options)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return Senso4sOptionsFlow()


class Senso4sOptionsFlow(OptionsFlow):
    """Handle options flow for Senso4s."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._user_input: dict[str, Any] = {}
        self._client: Optional[Senso4sBLEClient] = None

    def _is_plus_model(self) -> bool:
        """Check if the device is a Plus model."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator and coordinator.data:
            return coordinator.data.is_plus_model
        return True  # Default to Plus (show all options) if unknown

    def _get_current_values(self) -> dict[str, Any]:
        """Get current config values with fallbacks."""
        poll_interval = self.config_entry.options.get(
            CONF_HISTORY_POLL_INTERVAL,
            self.config_entry.data.get(
                CONF_HISTORY_POLL_INTERVAL, DEFAULT_HISTORY_POLL_INTERVAL
            ),
        )
        return {
            CONF_WEIGHT_UNIT: self.config_entry.options.get(
                CONF_WEIGHT_UNIT,
                self.config_entry.data.get(CONF_WEIGHT_UNIT, DEFAULT_WEIGHT_UNIT),
            ),
            CONF_EMPTY_WEIGHT: self.config_entry.options.get(
                CONF_EMPTY_WEIGHT,
                self.config_entry.data.get(CONF_EMPTY_WEIGHT, DEFAULT_EMPTY_WEIGHT),
            ),
            CONF_GAS_CAPACITY: self.config_entry.options.get(
                CONF_GAS_CAPACITY,
                self.config_entry.data.get(CONF_GAS_CAPACITY, DEFAULT_GAS_CAPACITY),
            ),
            CONF_USAGE_MODE: self.config_entry.options.get(
                CONF_USAGE_MODE,
                self.config_entry.data.get(CONF_USAGE_MODE, DEFAULT_USAGE_MODE),
            ),
            CONF_LOW_LEVEL_THRESHOLD: self.config_entry.options.get(
                CONF_LOW_LEVEL_THRESHOLD,
                self.config_entry.data.get(
                    CONF_LOW_LEVEL_THRESHOLD, DEFAULT_LOW_LEVEL_THRESHOLD
                ),
            ),
            CONF_ENABLE_HISTORY_POLLING: self.config_entry.options.get(
                CONF_ENABLE_HISTORY_POLLING,
                self.config_entry.data.get(CONF_ENABLE_HISTORY_POLLING, True),
            ),
            CONF_HISTORY_POLL_INTERVAL: poll_interval,
        }

    def _get_ha_options_schema(self, current: dict[str, Any]) -> vol.Schema:
        """Get schema for HA-only options."""
        weight_unit_options = {
            UNIT_KG: "Kilograms (kg)",
            UNIT_LB: "Pounds (lb)",
        }
        return vol.Schema(
            {
                vol.Required(
                    CONF_WEIGHT_UNIT, default=current[CONF_WEIGHT_UNIT]
                ): vol.In(weight_unit_options),
                vol.Required(
                    CONF_LOW_LEVEL_THRESHOLD, default=current[CONF_LOW_LEVEL_THRESHOLD]
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=50)),
                vol.Required(
                    CONF_ENABLE_HISTORY_POLLING,
                    default=current[CONF_ENABLE_HISTORY_POLLING],
                ): bool,
                vol.Required(
                    CONF_HISTORY_POLL_INTERVAL,
                    default=current[CONF_HISTORY_POLL_INTERVAL],
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            }
        )

    def _get_full_options_schema(self, current: dict[str, Any]) -> vol.Schema:
        """Get schema for all options including cylinder config."""
        weight_unit = current[CONF_WEIGHT_UNIT]
        if weight_unit == UNIT_LB:
            display_empty = kg_to_lb(current[CONF_EMPTY_WEIGHT])
            display_capacity = kg_to_lb(current[CONF_GAS_CAPACITY])
        else:
            display_empty = current[CONF_EMPTY_WEIGHT]
            display_capacity = current[CONF_GAS_CAPACITY]

        weight_unit_options = {
            UNIT_KG: "Kilograms (kg)",
            UNIT_LB: "Pounds (lb)",
        }
        # Filter out Caravanning for BASIC models
        is_plus = self._is_plus_model()
        usage_options = {
            mode.value: name
            for mode, name in USAGE_MODE_NAMES.items()
            if is_plus or mode != UsageMode.CARAVANNING
        }

        return vol.Schema(
            {
                vol.Required(
                    CONF_WEIGHT_UNIT, default=weight_unit
                ): vol.In(weight_unit_options),
                vol.Required(
                    CONF_EMPTY_WEIGHT, default=round(display_empty, 2)
                ): vol.Coerce(float),
                vol.Required(
                    CONF_GAS_CAPACITY, default=round(display_capacity, 2)
                ): vol.Coerce(float),
                vol.Required(
                    CONF_USAGE_MODE, default=current[CONF_USAGE_MODE]
                ): vol.In(usage_options),
                vol.Required(
                    CONF_LOW_LEVEL_THRESHOLD, default=current[CONF_LOW_LEVEL_THRESHOLD]
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=50)),
                vol.Required(
                    CONF_ENABLE_HISTORY_POLLING,
                    default=current[CONF_ENABLE_HISTORY_POLLING],
                ): bool,
                vol.Required(
                    CONF_HISTORY_POLL_INTERVAL,
                    default=current[CONF_HISTORY_POLL_INTERVAL],
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            }
        )

    async def _cleanup_client(self) -> None:
        """Clean up BLE client connection."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _get_ble_client(self) -> Optional[Senso4sBLEClient]:
        """Get a connected BLE client."""
        from homeassistant.components.bluetooth import async_ble_device_from_address

        address = self.config_entry.data.get(CONF_ADDRESS)
        if not address:
            return None

        ble_device = async_ble_device_from_address(self.hass, address)
        if not ble_device:
            return None

        from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

        # Create a minimal service info for the client
        service_info = BluetoothServiceInfoBleak(
            name=DEVICE_NAME,
            address=address,
            rssi=-60,
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            source="local",
            device=ble_device,
            advertisement=None,
            connectable=True,
            time=0,
            tx_power=None,
        )

        self._client = Senso4sBLEClient(service_info)
        if await self._client.connect():
            return self._client

        self._client = None
        return None

    def _save_options_entry(self) -> FlowResult:
        """Save the options entry with current user input."""
        weight_unit = self._user_input.get(CONF_WEIGHT_UNIT, DEFAULT_WEIGHT_UNIT)
        empty_weight = self._user_input.get(CONF_EMPTY_WEIGHT)
        gas_capacity = self._user_input.get(CONF_GAS_CAPACITY)

        # Convert to kg if entered in lb
        if empty_weight is not None and weight_unit == UNIT_LB:
            empty_weight = lb_to_kg(empty_weight)
        if gas_capacity is not None and weight_unit == UNIT_LB:
            gas_capacity = lb_to_kg(gas_capacity)

        # Build options data
        current = self._get_current_values()
        data = {
            CONF_WEIGHT_UNIT: weight_unit,
            CONF_LOW_LEVEL_THRESHOLD: self._user_input.get(
                CONF_LOW_LEVEL_THRESHOLD, current[CONF_LOW_LEVEL_THRESHOLD]
            ),
            CONF_ENABLE_HISTORY_POLLING: self._user_input.get(
                CONF_ENABLE_HISTORY_POLLING, current[CONF_ENABLE_HISTORY_POLLING]
            ),
            CONF_HISTORY_POLL_INTERVAL: self._user_input.get(
                CONF_HISTORY_POLL_INTERVAL, current[CONF_HISTORY_POLL_INTERVAL]
            ),
            CONF_EMPTY_WEIGHT: round(empty_weight, 2)
            if empty_weight is not None
            else current[CONF_EMPTY_WEIGHT],
            CONF_GAS_CAPACITY: round(gas_capacity, 2)
            if gas_capacity is not None
            else current[CONF_GAS_CAPACITY],
            CONF_USAGE_MODE: self._user_input.get(
                CONF_USAGE_MODE, current[CONF_USAGE_MODE]
            ),
        }

        if CONF_LAST_SETUP_DATE in self._user_input:
            new_data = {
                **self.config_entry.data,
                CONF_LAST_SETUP_DATE: self._user_input[CONF_LAST_SETUP_DATE],
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

        return self.async_create_entry(title="", data=data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Initial menu to choose action."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["refill_tank", "recalibrate", "settings"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Settings: HA-only options, no device interaction."""
        if user_input is not None:
            self._user_input = user_input
            return self._save_options_entry()

        current = self._get_current_values()
        return self.async_show_form(
            step_id="settings",
            data_schema=self._get_ha_options_schema(current),
        )

    async def async_step_refill_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refill tank: HA options, then write existing config + date to device."""
        if user_input is not None:
            self._user_input = user_input
            return await self.async_step_refill_write()

        current = self._get_current_values()
        return self.async_show_form(
            step_id="refill_tank",
            data_schema=self._get_ha_options_schema(current),
        )

    async def async_step_refill_write(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Write config and date for refill."""
        client = await self._get_ble_client()
        if not client:
            return self.async_abort(reason="connection_failed")

        try:
            # Get current cylinder config (we keep the existing values for refill)
            current = self._get_current_values()
            empty_weight = current[CONF_EMPTY_WEIGHT]
            gas_capacity = current[CONF_GAS_CAPACITY]
            usage_mode = UsageMode.from_value(current[CONF_USAGE_MODE])

            # Write config
            if not await client.write_config(empty_weight, gas_capacity, usage_mode):
                await self._cleanup_client()
                return self.async_abort(reason="config_write_failed")

            # Write setup date
            setup_date = dt_util.now()
            if not await client.write_setup_date(setup_date):
                await self._cleanup_client()
                return self.async_abort(reason="setup_date_write_failed")

            # Verify
            success, level, anomalies = await client.wait_for_valid_level()
            await self._cleanup_client()

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                return self.async_abort(
                    reason="verification_anomaly",
                    description_placeholders={"anomalies": ", ".join(anomaly_names)},
                )

            if not success:
                return self.async_abort(reason="verification_timeout")

            self._user_input[CONF_LAST_SETUP_DATE] = setup_date.isoformat()

            return self._save_options_entry()

        except Exception as err:
            _LOGGER.exception("[%s] Error during refill: %s", self.config_entry.data.get(CONF_ADDRESS, "?"), err)
            await self._cleanup_client()
            return self.async_abort(reason="connection_failed")

    async def async_step_recalibrate(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Recalibrate: all options, remove tank instruction."""
        if user_input is not None:
            self._user_input = user_input
            return await self.async_step_recalibrate_calibrating()

        current = self._get_current_values()
        return self.async_show_form(
            step_id="recalibrate",
            data_schema=self._get_full_options_schema(current),
        )

    async def async_step_recalibrate_calibrating(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Perform calibration."""
        client = await self._get_ble_client()
        if not client:
            return self.async_abort(reason="connection_failed")

        try:
            success, anomalies = await client.calibrate()
            if not success:
                await self._cleanup_client()
                return self.async_abort(reason="calibration_failed")

            # Keep client connected for next step
            return await self.async_step_recalibrate_replace()

        except Exception as err:
            _LOGGER.exception("[%s] Error during calibration: %s", self.config_entry.data.get(CONF_ADDRESS, "?"), err)
            await self._cleanup_client()
            return self.async_abort(reason="calibration_failed")

    async def async_step_recalibrate_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Tell user to replace tank."""
        if user_input is not None:
            return await self.async_step_recalibrate_write()

        return self.async_show_form(step_id="recalibrate_replace")

    async def async_step_recalibrate_write(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Write config and date after recalibration."""
        # Reconnect if needed
        if self._client is None or not self._client.is_connected:
            client = await self._get_ble_client()
            if not client:
                return self.async_abort(reason="connection_failed")
        else:
            client = self._client

        try:
            # Get cylinder config from user input
            weight_unit = self._user_input.get(CONF_WEIGHT_UNIT, DEFAULT_WEIGHT_UNIT)
            empty_weight = self._user_input[CONF_EMPTY_WEIGHT]
            gas_capacity = self._user_input[CONF_GAS_CAPACITY]

            # Convert to kg if entered in lb
            if weight_unit == UNIT_LB:
                empty_weight = lb_to_kg(empty_weight)
                gas_capacity = lb_to_kg(gas_capacity)

            usage_mode = UsageMode.from_value(self._user_input[CONF_USAGE_MODE])

            # Write config
            if not await client.write_config(empty_weight, gas_capacity, usage_mode):
                await self._cleanup_client()
                return self.async_abort(reason="config_write_failed")

            # Write setup date
            setup_date = dt_util.now()
            if not await client.write_setup_date(setup_date):
                await self._cleanup_client()
                return self.async_abort(reason="setup_date_write_failed")

            # Verify
            success, level, anomalies = await client.wait_for_valid_level()
            await self._cleanup_client()

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                return self.async_abort(
                    reason="verification_anomaly",
                    description_placeholders={"anomalies": ", ".join(anomaly_names)},
                )

            if not success:
                return self.async_abort(reason="verification_timeout")

            self._user_input[CONF_LAST_SETUP_DATE] = setup_date.isoformat()

            return self._save_options_entry()

        except Exception as err:
            _LOGGER.exception("[%s] Error during recalibrate write: %s", self.config_entry.data.get(CONF_ADDRESS, "?"), err)
            await self._cleanup_client()
            return self.async_abort(reason="connection_failed")
