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

import logging
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from .ble_client import Senso4sBLEClient
from .const import (
    CONF_EMPTY_WEIGHT,
    CONF_ENABLE_HISTORY_POLLING,
    CONF_GAS_CAPACITY,
    CONF_HISTORY_POLL_INTERVAL,
    CONF_IS_PLUS,
    CONF_LAST_SETUP_DATE,
    CONF_LOW_LEVEL_THRESHOLD,
    CONF_USAGE_MODE,
    CONF_WEIGHT_UNIT,
    DEFAULT_HISTORY_POLL_INTERVAL,
    DOMAIN,
    ISSUE_NEEDS_CALIBRATION,
    UsageMode,
)
from .coordinator import Senso4sCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

_CALIBRATION_STATE: dict[str, bool] = {}


def _check_calibration_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sCoordinator,
) -> None:
    """Create / clear the calibration repair issue as the device state changes."""
    address = coordinator.address
    needs_calibration = coordinator.data.needs_calibration
    previous_state = _CALIBRATION_STATE.get(address)
    _CALIBRATION_STATE[address] = needs_calibration

    if previous_state == needs_calibration:
        return

    issue_id = f"{ISSUE_NEEDS_CALIBRATION}_{address}"
    if needs_calibration:
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
        _LOGGER.debug("Deleting calibration issue for %s", address)
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def _check_and_sync_usage_mode(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sCoordinator,
) -> None:
    """If the advertisement's usage mode differs from our stored one, persist it."""
    adv_usage_mode = coordinator.data.usage_mode
    if adv_usage_mode == coordinator.usage_mode:
        return

    _LOGGER.info(
        "Usage mode changed externally for %s: %s -> %s",
        coordinator.device_name,
        coordinator.usage_mode.name,
        adv_usage_mode.name,
    )
    coordinator.update_config(usage_mode=adv_usage_mode)
    new_options = {**entry.options, CONF_USAGE_MODE: adv_usage_mode.value}
    try:
        hass.config_entries.async_update_entry(entry, options=new_options)
    except Exception as err:
        _LOGGER.warning("[%s] Failed to persist usage mode change: %s", coordinator.address, err)


async def _async_check_and_sync_config(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Senso4sCoordinator,
    client: Senso4sBLEClient,
) -> datetime | None:
    """Detect external setup-date changes and persist the new cylinder config."""
    try:
        setup_date = await client.read_setup_date()
    except Exception as err:
        _LOGGER.debug("[%s] Failed to read setup date for sync check: %s", coordinator.address, err)
        return None
    if setup_date is None:
        return None

    coordinator.data.setup_date = setup_date
    if not coordinator.update_setup_date(setup_date):
        return setup_date

    _LOGGER.info(
        "External config modification detected (setup date changed) for %s",
        coordinator.device_name,
    )
    try:
        config = await client.read_config()
    except Exception as err:
        _LOGGER.warning("[%s] Failed to read config after setup date change: %s", coordinator.address, err)
        return setup_date
    if config is None:
        return setup_date

    coordinator.update_config(
        empty_weight_kg=config.empty_weight_kg,
        gas_capacity_kg=config.gas_capacity_kg,
    )
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
        _LOGGER.warning("[%s] Failed to persist config entry update: %s", coordinator.address, err)
    coordinator.last_history_update = None
    return setup_date


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries forward."""
    _LOGGER.debug("Migrating from version %s.%s", entry.version, entry.minor_version)
    if entry.version > 1:
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Senso4s from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    coordinator = Senso4sCoordinator(hass, entry, address)

    await coordinator.async_load_passive_history()

    # Seed from cache so entities render on warm restarts, the backfill
    # below has data to read, and the proof-of-life backstop can trigger
    # an initial history poll without waiting for a dispatch.
    seed_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if seed_info is not None:
        coordinator._update_method(seed_info)
        coordinator._last_service_info = seed_info

    # Backfill for entries created before rc8.
    if CONF_IS_PLUS not in entry.data:
        is_plus = bool(coordinator.data.mac_address) and coordinator.data.is_plus_model
        try:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_IS_PLUS: is_plus}
            )
        except Exception as err:
            _LOGGER.warning("Failed to backfill CONF_IS_PLUS for %s: %s", address, err)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(coordinator.async_start())
    coordinator.async_start_proof_of_life()
    entry.async_on_unload(coordinator.async_stop_proof_of_life)
    coordinator.async_start_periodic_poll()
    entry.async_on_unload(coordinator.async_stop_periodic_poll)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    if coordinator.data.mac_address:
        _check_calibration_issue(hass, entry, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    address = entry.data[CONF_ADDRESS]
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        _CALIBRATION_STATE.pop(address, None)
        ir.async_delete_issue(
            hass, DOMAIN, f"{ISSUE_NEEDS_CALIBRATION}_{address}"
        )
    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push option changes into the coordinator without a reload."""
    coordinator: Senso4sCoordinator = hass.data[DOMAIN][entry.entry_id]
    poll_interval = entry.options.get(
        CONF_HISTORY_POLL_INTERVAL,
        entry.data.get(CONF_HISTORY_POLL_INTERVAL, DEFAULT_HISTORY_POLL_INTERVAL),
    )
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
        enable_history_polling=entry.options.get(
            CONF_ENABLE_HISTORY_POLLING,
            entry.data.get(CONF_ENABLE_HISTORY_POLLING, True),
        ),
        history_poll_interval=poll_interval,
    )

    new_setup_date_str = entry.data.get(CONF_LAST_SETUP_DATE)
    setup_date_changed = False
    if new_setup_date_str:
        try:
            new_setup_date = datetime.fromisoformat(new_setup_date_str)
        except (ValueError, TypeError):
            new_setup_date = None
        if new_setup_date and coordinator.update_setup_date(new_setup_date):
            setup_date_changed = True

    coordinator.async_set_updated_data(coordinator.data)

    if setup_date_changed:
        coordinator.async_request_refresh()


def _get_coordinators_and_entries_from_service_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[Senso4sCoordinator, ConfigEntry]]:
    """Translate a device_id argument to (coordinator, entry) tuples."""
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
    results: list[tuple[Senso4sCoordinator, ConfigEntry]] = []
    for device_id in device_ids:
        device = device_registry.async_get(device_id)
        if not device:
            continue
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


CALIBRATE_SCHEMA = vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])})
REFRESH_HISTORY_SCHEMA = vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])})
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
    """Register integration services (idempotent across multiple entries)."""

    async def handle_calibrate(call: ServiceCall) -> None:
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
                    await _async_check_and_sync_config(hass, entry, coordinator, client)
                    success, anomalies = await client.calibrate()
                    if success:
                        _LOGGER.info(
                            "Calibration completed for %s. Anomalies: %s",
                            coordinator.device_name,
                            [a.name for a in anomalies],
                        )
                        coordinator.async_request_refresh()
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
                    if await client.write_config(empty_weight, gas_capacity, usage_mode):
                        _LOGGER.info(
                            "Configuration written to %s", coordinator.device_name
                        )
                        coordinator.update_config(
                            empty_weight_kg=empty_weight,
                            gas_capacity_kg=gas_capacity,
                            usage_mode=usage_mode,
                        )
                        coordinator.async_request_refresh()
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
        results = _get_coordinators_and_entries_from_service_call(hass, call)
        setup_datetime = call.data.get("datetime") or dt_util.now()
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
                    if await client.write_setup_date(setup_datetime):
                        _LOGGER.info(
                            "Setup date set to %s for %s",
                            setup_datetime,
                            coordinator.device_name,
                        )
                        coordinator.async_request_refresh()
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

    if not hass.services.has_service(DOMAIN, "calibrate"):
        hass.services.async_register(
            DOMAIN, "calibrate", handle_calibrate, schema=CALIBRATE_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, "refresh_history"):
        hass.services.async_register(
            DOMAIN,
            "refresh_history",
            handle_refresh_history,
            schema=REFRESH_HISTORY_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, "write_config"):
        hass.services.async_register(
            DOMAIN, "write_config", handle_write_config, schema=WRITE_CONFIG_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, "set_setup_date"):
        hass.services.async_register(
            DOMAIN, "set_setup_date", handle_set_setup_date, schema=SET_SETUP_DATE_SCHEMA
        )
