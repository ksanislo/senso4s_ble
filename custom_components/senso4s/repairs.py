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

"""Repair flows for Senso4s integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .ble_client import Senso4sBLEClient
from .const import (
    ANOMALY_NAMES,
    DOMAIN,
    ISSUE_NEEDS_CALIBRATION,
)

if TYPE_CHECKING:
    from .coordinator import Senso4sDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class CalibrationRepairFlow(RepairsFlow):
    """Handler for calibration repair flow."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the repair flow."""
        super().__init__()
        self._entry_id = entry_id
        self._client: Senso4sBLEClient | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step - explain calibration process."""
        if user_input is not None:
            # User acknowledged, proceed to cylinder removal step
            return await self.async_step_remove_cylinder()

        return self.async_show_form(step_id="init")

    async def async_step_remove_cylinder(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the cylinder removal confirmation step."""
        if user_input is not None:
            # User confirmed cylinder is removed, proceed to calibration
            return await self.async_step_calibrating()

        return self.async_show_form(step_id="remove_cylinder")

    async def async_step_calibrating(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Perform the calibration."""
        coordinator = self._get_coordinator()
        if coordinator is None:
            return self.async_abort(reason="device_not_found")

        if not coordinator.service_info:
            return self.async_abort(reason="no_bluetooth_connection")

        # Connect and calibrate
        self._client = Senso4sBLEClient(coordinator.service_info)
        try:
            if not await self._client.connect():
                return self.async_abort(reason="connection_failed")

            # Check for external config changes before calibrating
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                from . import _async_check_and_sync_config
                await _async_check_and_sync_config(
                    self.hass, entry, coordinator, self._client
                )

            success, anomalies = await self._client.calibrate()
            if not success:
                await self._client.disconnect()
                self._client = None
                return self.async_abort(reason="calibration_failed")

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                _LOGGER.info("Calibration completed with anomalies: %s", anomaly_names)

            # Calibration succeeded, proceed to replace tank step
            return await self.async_step_replace_tank()

        except Exception as err:
            _LOGGER.exception("Error during calibration: %s", err)
            if self._client:
                await self._client.disconnect()
                self._client = None
            return self.async_abort(reason="calibration_failed")

    async def async_step_replace_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the replace tank step."""
        if user_input is not None:
            # User confirmed tank is replaced, write config
            return await self.async_step_write_config()

        return self.async_show_form(step_id="replace_tank")

    async def async_step_write_config(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Write configuration to device."""
        coordinator = self._get_coordinator()
        if coordinator is None:
            await self._cleanup()
            return self.async_abort(reason="device_not_found")

        if self._client is None or not self._client.is_connected:
            await self._cleanup()
            return self.async_abort(reason="connection_lost")

        try:
            # Write the cylinder config
            success = await self._client.write_config(
                coordinator.empty_weight_kg,
                coordinator.gas_capacity_kg,
                coordinator.usage_mode,
            )
            if not success:
                await self._cleanup()
                return self.async_abort(reason="config_write_failed")

            # Write the setup date to now
            success = await self._client.write_setup_date(dt_util.now())
            if not success:
                await self._cleanup()
                return self.async_abort(reason="setup_date_write_failed")

            # Proceed to verification
            return await self.async_step_verify()

        except Exception as err:
            _LOGGER.exception("Error writing config: %s", err)
            await self._cleanup()
            return self.async_abort(reason="config_write_failed")

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Verify calibration was successful by waiting for valid level."""
        coordinator = self._get_coordinator()
        if coordinator is None:
            await self._cleanup()
            return self.async_abort(reason="device_not_found")

        if self._client is None or not self._client.is_connected:
            await self._cleanup()
            return self.async_abort(reason="connection_lost")

        try:
            success, level, anomalies = await self._client.wait_for_valid_level()

            if anomalies:
                anomaly_names = [ANOMALY_NAMES.get(a, str(a)) for a in anomalies]
                _LOGGER.warning("Calibration verification reported anomalies: %s", anomaly_names)
                await self._cleanup()
                return self.async_abort(
                    reason="verification_anomaly",
                    description_placeholders={"anomalies": ", ".join(anomaly_names)},
                )

            if not success:
                await self._cleanup()
                return self.async_abort(reason="verification_timeout")

            _LOGGER.info(
                "Calibration successful for %s, level: %d%%",
                coordinator.device_name,
                level,
            )

            # Reset history freshness so it gets refetched
            coordinator.last_history_update = None

            # Delete the repair issue
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_NEEDS_CALIBRATION}_{coordinator.address}",
            )

            await self._cleanup()
            return self.async_create_entry(data={})

        except Exception as err:
            _LOGGER.exception("Error during verification: %s", err)
            await self._cleanup()
            return self.async_abort(reason="verification_failed")

    def _get_coordinator(self):
        """Get the coordinator for this entry."""
        from .coordinator import Senso4sDataUpdateCoordinator

        if DOMAIN not in self.hass.data:
            return None
        return self.hass.data[DOMAIN].get(self._entry_id)

    async def _cleanup(self) -> None:
        """Clean up BLE connection."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create a repair flow for the given issue."""
    if issue_id.startswith(ISSUE_NEEDS_CALIBRATION):
        entry_id = data.get("entry_id") if data else None
        if entry_id:
            return CalibrationRepairFlow(entry_id)

    raise data_entry_flow.UnknownHandler
