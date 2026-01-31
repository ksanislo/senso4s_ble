"""Diagnostics support for Senso4s integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import DOMAIN, USAGE_MODE_NAMES
from .coordinator import Senso4sDataUpdateCoordinator

# Keys to redact from diagnostics for privacy
TO_REDACT = {CONF_ADDRESS, "address", "mac_address"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: Senso4sDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data

    diagnostics_data = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "device": {
            "address": coordinator.address,
            "mac_address": data.mac_address,
            "name": data.name,
            "is_plus_model": data.is_plus_model,
        },
        "state": {
            "gas_level_percent": data.gas_level_percent,
            "gas_remaining_kg": data.gas_remaining_kg,
            "battery_percent": data.battery_percent,
            "usage_mode": USAGE_MODE_NAMES.get(data.usage_mode, str(data.usage_mode)),
            "is_available": data.is_available,
        },
        "status": {
            "needs_calibration": data.needs_calibration,
            "has_error": data.has_error,
            "error_code": data.error_code,
            "error_description": data.error_description,
            "anomalies": data.anomaly_names,
            "has_anomaly": data.has_anomaly,
        },
        "configuration": {
            "empty_weight_kg": coordinator.empty_weight_kg,
            "gas_capacity_kg": coordinator.gas_capacity_kg,
            "usage_mode": USAGE_MODE_NAMES.get(
                coordinator.usage_mode, str(coordinator.usage_mode)
            ),
            "low_level_threshold": coordinator.low_level_threshold,
        },
        "timing": {
            "last_seen": (
                data.last_seen.isoformat() if data.last_seen else None
            ),
            "setup_date": (
                data.setup_date.isoformat() if data.setup_date else None
            ),
            "last_history_update": (
                coordinator.last_history_update.isoformat()
                if coordinator.last_history_update
                else None
            ),
            "estimated_empty_date": (
                coordinator.estimated_empty_date.isoformat()
                if coordinator.estimated_empty_date
                else None
            ),
        },
        "history": {
            "record_count": len(coordinator.history),
            "records": [
                {
                    "timestamp": rec.timestamp.isoformat(),
                    "remaining_gas_kg": rec.remaining_gas_kg,
                }
                for rec in coordinator.history[-20:]  # Last 20 records
            ],
        },
    }

    return async_redact_data(diagnostics_data, TO_REDACT)
