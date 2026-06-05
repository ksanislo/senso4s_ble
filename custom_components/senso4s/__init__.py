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

"""The Senso4s Gas Cylinder Sensor integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.util import dt as dt_util
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .ble_client import Senso4sBLEClient
from .const import (
    CONF_EMPTY_WEIGHT,
    CONF_GAS_CAPACITY,
    CONF_HISTORY_POLL_INTERVAL,
    CONF_LAST_SETUP_DATE,
    CONF_LOW_LEVEL_THRESHOLD,
    CONF_USAGE_MODE,
    CONF_WEIGHT_UNIT,
    DEFAULT_HISTORY_POLL_INTERVAL,
    DOMAIN,
    ISSUE_NEEDS_CALIBRATION,
    UsageMode,
)
from .coordinator import Senso4sDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Track history fetch locks per entry to prevent concurrent fetches
_HISTORY_LOCKS: dict[str, asyncio.Lock] = {}

# Track calibration state per device to detect transitions
_CALIBRATION_STATE: dict[str, bool] = {}


def _check_calibration_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: "Senso4sDataUpdateCoordinator",
) -> None:
    """Check and manage calibration issue based on device state."""
    address = coordinator.address
    needs_calibration = coordinator.data.needs_calibration
    previous_state = _CALIBRATION_STATE.get(address)

    # Update tracked state
    _CALIBRATION_STATE[address] = needs_calibration

    # Only act on state changes (or first observation)
    if previous_state == needs_calibration:
        return

    issue_id = f"{ISSUE_NEEDS_CALIBRATION}_{address}"

    if needs_calibration:
        # Create a repair issue
        _LOGGER.debug("Creating calibration issue for %s", address)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NEEDS_CALIBRATION,
            translation_placeholders={"device_name": coordinator.device_name},
            data={"entry_id": entry.entry_id},
        )
    else:
        # Delete the issue (calibration completed, possibly via OEM app)
        _LOGGER.debug("Deleting calibration issue for %s", address)
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_check_and_sync_config(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sDataUpdateCoordinator,
    client: Senso4sBLEClient,
) -> datetime | None:
    """Check if setup date changed and sync config if so.

    Returns the setup_date read from device (also stored in coordinator.data.setup_date),
    or None if read failed. Callers can reuse this to avoid duplicate reads.
    """

    # Read setup date from device
    try:
        setup_date = await client.read_setup_date()
    except Exception as err:
        _LOGGER.debug("Failed to read setup date for sync check: %s", err)
        return None

    if setup_date is None:
        _LOGGER.debug("Could not read setup date for sync check")
        return None

    # Update coordinator data for the sensor
    coordinator.data.setup_date = setup_date

    # Check if setup date changed - if not, just return the date
    if not coordinator.update_setup_date(setup_date):
        return setup_date

    _LOGGER.info(
        "External config modification detected (setup date changed) for %s",
        coordinator.device_name,
    )

    # Read config from device (empty_weight, gas_capacity)
    # Note: usage_mode is already tracked from advertisements
    try:
        config = await client.read_config()
    except Exception as err:
        _LOGGER.warning("Failed to read config after setup date change: %s", err)
        return setup_date  # Return the date we read even if config sync failed

    if config is None:
        _LOGGER.warning("Could not read config after setup date change")
        return setup_date  # Return the date we read even if config sync failed

    # Update coordinator values
    coordinator.update_config(
        empty_weight_kg=config.empty_weight_kg,
        gas_capacity_kg=config.gas_capacity_kg,
    )

    # Persist to config entry
    new_data = {**entry.data, CONF_LAST_SETUP_DATE: setup_date.isoformat()}
    new_options = {
        **entry.options,
        CONF_EMPTY_WEIGHT: config.empty_weight_kg,
        CONF_GAS_CAPACITY: config.gas_capacity_kg,
    }

    try:
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options
        )
    except Exception as err:
        _LOGGER.warning("Failed to persist config entry update: %s", err)

    # Reset history freshness (history may be invalid after config change)
    coordinator.last_history_update = None

    # Dispatch update to refresh entities
    async_dispatcher_send(hass, f"{DOMAIN}_{coordinator.address}_update")

    return setup_date


def _check_and_sync_usage_mode(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sDataUpdateCoordinator,
) -> None:
    """Check if usage mode changed from advertisement and sync if needed."""
    # Get usage mode from advertisement data
    adv_usage_mode = coordinator.data.usage_mode

    # Compare to stored usage mode in coordinator (from options)
    if adv_usage_mode == coordinator.usage_mode:
        return

    _LOGGER.info(
        "Usage mode changed externally for %s: %s -> %s",
        coordinator.device_name,
        coordinator.usage_mode.name,
        adv_usage_mode.name,
    )

    # Update coordinator
    coordinator.update_config(usage_mode=adv_usage_mode)

    # Persist to config entry options
    new_options = {**entry.options, CONF_USAGE_MODE: adv_usage_mode.value}
    try:
        hass.config_entries.async_update_entry(entry, options=new_options)
    except Exception as err:
        _LOGGER.warning("Failed to persist usage mode change: %s", err)


async def _async_refresh_history_if_stale(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sDataUpdateCoordinator,
) -> None:
    """Refresh history if it's stale or missing."""
    # Check if history refresh is disabled
    if coordinator.history_poll_interval <= 0:
        return

    # Check if history is stale
    now = dt_util.now()
    if coordinator.last_history_update is not None:
        age = (now - coordinator.last_history_update).total_seconds() / 60
        if age < coordinator.history_poll_interval:
            _LOGGER.debug(
                "History is fresh (%.1f min old, threshold %d min)",
                age,
                coordinator.history_poll_interval,
            )
            return

    # Get or create lock for this entry
    entry_id = coordinator.entry.entry_id
    if entry_id not in _HISTORY_LOCKS:
        _HISTORY_LOCKS[entry_id] = asyncio.Lock()

    # Try to acquire lock (non-blocking) - skip if already fetching
    lock = _HISTORY_LOCKS[entry_id]
    if lock.locked():
        _LOGGER.debug("History fetch already in progress, skipping")
        return

    async with lock:
        if not coordinator.service_info:
            _LOGGER.debug("No service info available for history fetch")
            return

        _LOGGER.debug("Fetching history for %s (stale or missing)", coordinator.address)
        client = Senso4sBLEClient(coordinator.service_info)
        try:
            if await client.connect():
                # Check for external config changes and get setup_date in one read
                setup_date = await _async_check_and_sync_config(
                    hass, entry, coordinator, client
                )
                if setup_date:
                    history = await client.read_history(setup_date)
                    coordinator.update_history(history)
                    _LOGGER.debug("Fetched %d history records", len(history))
                    async_dispatcher_send(
                        hass, f"{DOMAIN}_{coordinator.address}_update"
                    )
                else:
                    _LOGGER.debug("Could not read setup date")
        except Exception as err:
            _LOGGER.debug("Error fetching history: %s", err)
        finally:
            await client.disconnect()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry to current version."""
    _LOGGER.debug("Migrating from version %s.%s", entry.version, entry.minor_version)

    if entry.version > 1:
        # Future version - can't downgrade
        return False

    # Version 1 is current - no migration needed
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Senso4s from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    # Create coordinator
    coordinator = Senso4sDataUpdateCoordinator(hass, entry, address)

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up bluetooth callback for passive updates
    @callback
    def _async_update_callback(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle bluetooth advertisement update."""
        if coordinator.update_from_advertisement(service_info):
            async_dispatcher_send(hass, f"{DOMAIN}_{address}_update")
            # Check if calibration issue needs to be created/deleted
            _check_calibration_issue(hass, entry, coordinator)
            # Check if usage mode changed externally (from advertisement)
            _check_and_sync_usage_mode(hass, entry, coordinator)
            # Trigger history refresh if stale (runs in background)
            hass.async_create_task(
                _async_refresh_history_if_stale(hass, entry, coordinator)
            )

    # Register for bluetooth updates. connectable=False means "accept adverts
    # regardless of connectable state" — without it the matcher defaults to
    # requiring connectable=True and silently drops the non-connectable adverts
    # the proxy emits in the moments after an active connection cycle, leaving
    # the integration starved of updates.
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_callback,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=False),
            BluetoothScanningMode.ACTIVE,
        )
    )

    # Try to get initial data from current advertisement
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info:
        coordinator.update_from_advertisement(service_info)
        # Check if calibration issue needs to be created
        _check_calibration_issue(hass, entry, coordinator)

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await _async_setup_services(hass)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Do initial history fetch after a short delay
    async def _initial_history_fetch() -> None:
        await asyncio.sleep(10)  # Wait for BLE to stabilize
        await _async_refresh_history_if_stale(hass, entry, coordinator)

    hass.async_create_task(_initial_history_fetch())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    address = entry.data[CONF_ADDRESS]

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        # Clean up history lock
        _HISTORY_LOCKS.pop(entry.entry_id, None)
        # Clean up calibration state
        _CALIBRATION_STATE.pop(address, None)
        # Delete any calibration issue
        ir.async_delete_issue(
            hass, DOMAIN, f"{ISSUE_NEEDS_CALIBRATION}_{address}"
        )

    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: Senso4sDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    coordinator.update_config(
        empty_weight_kg=entry.options.get(
            CONF_EMPTY_WEIGHT, entry.data.get(CONF_EMPTY_WEIGHT)
        ),
        gas_capacity_kg=entry.options.get(
            CONF_GAS_CAPACITY, entry.data.get(CONF_GAS_CAPACITY)
        ),
        usage_mode=UsageMode.from_value(
            entry.options.get(CONF_USAGE_MODE, entry.data.get(CONF_USAGE_MODE))
        ),
        low_level_threshold=entry.options.get(
            CONF_LOW_LEVEL_THRESHOLD, entry.data.get(CONF_LOW_LEVEL_THRESHOLD)
        ),
        weight_unit=entry.options.get(
            CONF_WEIGHT_UNIT, entry.data.get(CONF_WEIGHT_UNIT)
        ),
        history_poll_interval=entry.options.get(
            CONF_HISTORY_POLL_INTERVAL,
            entry.data.get(CONF_HISTORY_POLL_INTERVAL, DEFAULT_HISTORY_POLL_INTERVAL),
        ),
    )

    async_dispatcher_send(hass, f"{DOMAIN}_{entry.data[CONF_ADDRESS]}_update")


def _get_coordinators_and_entries_from_service_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[Senso4sDataUpdateCoordinator, ConfigEntry]]:
    """Get coordinators and their config entries from service call device targets."""
    device_ids = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    if not device_ids:
        raise ServiceValidationError(
            "No device specified. Please select a Senso4s device.",
            translation_domain=DOMAIN,
            translation_key="no_device_specified",
        )

    device_registry = dr.async_get(hass)
    results: list[tuple[Senso4sDataUpdateCoordinator, ConfigEntry]] = []

    for device_id in device_ids:
        device = device_registry.async_get(device_id)
        if not device:
            continue

        # Find the config entry for this device
        for entry_id in device.config_entries:
            if entry_id in hass.data.get(DOMAIN, {}):
                coordinator = hass.data[DOMAIN][entry_id]
                entry = hass.config_entries.async_get_entry(entry_id)
                if entry:
                    results.append((coordinator, entry))
                break

    if not results:
        raise ServiceValidationError(
            "No valid Senso4s device found.",
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )

    return results


# Service schemas
CALIBRATE_SCHEMA = vol.Schema({
    vol.Optional("device_id"): vol.Any(str, [str]),
})

REFRESH_HISTORY_SCHEMA = vol.Schema({
    vol.Optional("device_id"): vol.Any(str, [str]),
})

WRITE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): vol.Any(str, [str]),
        vol.Optional("empty_weight_kg"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=50)
        ),
        vol.Optional("gas_capacity_kg"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=50)
        ),
        vol.Optional("usage_mode"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=5)
        ),
    }
)

SET_SETUP_DATE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): vol.Any(str, [str]),
        vol.Optional("datetime"): cv.datetime,
    }
)


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up integration services."""

    async def handle_calibrate(call: ServiceCall) -> None:
        """Handle the calibrate service call."""
        results = _get_coordinators_and_entries_from_service_call(hass, call)

        for coordinator, entry in results:
            if not coordinator.service_info:
                raise ServiceValidationError(
                    f"No Bluetooth connection available for {coordinator.device_name}",
                    translation_domain=DOMAIN,
                    translation_key="no_bluetooth_connection",
                )

            client = Senso4sBLEClient(coordinator.service_info)
            try:
                if await client.connect():
                    # Check for external config changes
                    await _async_check_and_sync_config(hass, entry, coordinator, client)

                    success, anomalies = await client.calibrate()
                    if success:
                        _LOGGER.info(
                            "Calibration completed for %s. Anomalies: %s",
                            coordinator.device_name,
                            [a.name for a in anomalies],
                        )
                    else:
                        raise ServiceValidationError(
                            f"Calibration failed for {coordinator.device_name}",
                            translation_domain=DOMAIN,
                            translation_key="calibration_failed",
                        )
                else:
                    raise ServiceValidationError(
                        f"Failed to connect to {coordinator.device_name}",
                        translation_domain=DOMAIN,
                        translation_key="connection_failed",
                    )
            finally:
                await client.disconnect()

    async def handle_refresh_history(call: ServiceCall) -> None:
        """Handle the refresh history service call."""
        results = _get_coordinators_and_entries_from_service_call(hass, call)

        for coordinator, entry in results:
            if not coordinator.service_info:
                raise ServiceValidationError(
                    f"No Bluetooth connection available for {coordinator.device_name}",
                    translation_domain=DOMAIN,
                    translation_key="no_bluetooth_connection",
                )

            client = Senso4sBLEClient(coordinator.service_info)
            try:
                if await client.connect():
                    # Check for external config changes and get setup_date in one read
                    setup_date = await _async_check_and_sync_config(
                        hass, entry, coordinator, client
                    )
                    if setup_date:
                        history = await client.read_history(setup_date)
                        coordinator.update_history(history)
                        _LOGGER.info(
                            "Retrieved %d history records for %s",
                            len(history),
                            coordinator.device_name,
                        )
                        async_dispatcher_send(
                            hass, f"{DOMAIN}_{coordinator.address}_update"
                        )
                    else:
                        raise ServiceValidationError(
                            f"Could not read setup date from {coordinator.device_name}",
                            translation_domain=DOMAIN,
                            translation_key="setup_date_read_failed",
                        )
                else:
                    raise ServiceValidationError(
                        f"Failed to connect to {coordinator.device_name}",
                        translation_domain=DOMAIN,
                        translation_key="connection_failed",
                    )
            finally:
                await client.disconnect()

    async def handle_write_config(call: ServiceCall) -> None:
        """Handle the write config service call."""
        # Note: This is a write operation, so we skip config sync
        results = _get_coordinators_and_entries_from_service_call(hass, call)

        for coordinator, _entry in results:
            if not coordinator.service_info:
                raise ServiceValidationError(
                    f"No Bluetooth connection available for {coordinator.device_name}",
                    translation_domain=DOMAIN,
                    translation_key="no_bluetooth_connection",
                )

            empty_weight = call.data.get("empty_weight_kg", coordinator.empty_weight_kg)
            gas_capacity = call.data.get("gas_capacity_kg", coordinator.gas_capacity_kg)
            usage_mode = UsageMode.from_value(
                call.data.get("usage_mode", coordinator.usage_mode.value)
            )

            client = Senso4sBLEClient(coordinator.service_info)
            try:
                if await client.connect():
                    success = await client.write_config(
                        empty_weight, gas_capacity, usage_mode
                    )
                    if success:
                        _LOGGER.info(
                            "Configuration written to %s", coordinator.device_name
                        )
                        coordinator.update_config(
                            empty_weight_kg=empty_weight,
                            gas_capacity_kg=gas_capacity,
                            usage_mode=usage_mode,
                        )
                    else:
                        raise ServiceValidationError(
                            f"Failed to write configuration to {coordinator.device_name}",
                            translation_domain=DOMAIN,
                            translation_key="config_write_failed",
                        )
                else:
                    raise ServiceValidationError(
                        f"Failed to connect to {coordinator.device_name}",
                        translation_domain=DOMAIN,
                        translation_key="connection_failed",
                    )
            finally:
                await client.disconnect()

    async def handle_set_setup_date(call: ServiceCall) -> None:
        """Handle the set setup date service call."""
        # Note: This is a write operation, so we skip config sync
        results = _get_coordinators_and_entries_from_service_call(hass, call)

        setup_datetime = call.data.get("datetime")
        if setup_datetime is None:
            setup_datetime = dt_util.now()

        for coordinator, _entry in results:
            if not coordinator.service_info:
                raise ServiceValidationError(
                    f"No Bluetooth connection available for {coordinator.device_name}",
                    translation_domain=DOMAIN,
                    translation_key="no_bluetooth_connection",
                )

            client = Senso4sBLEClient(coordinator.service_info)
            try:
                if await client.connect():
                    success = await client.write_setup_date(setup_datetime)
                    if success:
                        _LOGGER.info(
                            "Setup date set to %s for %s",
                            setup_datetime,
                            coordinator.device_name,
                        )
                    else:
                        raise ServiceValidationError(
                            f"Failed to set setup date on {coordinator.device_name}",
                            translation_domain=DOMAIN,
                            translation_key="setup_date_write_failed",
                        )
                else:
                    raise ServiceValidationError(
                        f"Failed to connect to {coordinator.device_name}",
                        translation_domain=DOMAIN,
                        translation_key="connection_failed",
                    )
            finally:
                await client.disconnect()

    # Register services if not already registered
    if not hass.services.has_service(DOMAIN, "calibrate"):
        hass.services.async_register(
            DOMAIN, "calibrate", handle_calibrate, schema=CALIBRATE_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, "refresh_history"):
        hass.services.async_register(
            DOMAIN, "refresh_history", handle_refresh_history, schema=REFRESH_HISTORY_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, "write_config"):
        hass.services.async_register(
            DOMAIN, "write_config", handle_write_config, schema=WRITE_CONFIG_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, "set_setup_date"):
        hass.services.async_register(
            DOMAIN, "set_setup_date", handle_set_setup_date, schema=SET_SETUP_DATE_SCHEMA
        )
