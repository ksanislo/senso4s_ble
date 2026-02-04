"""Data coordinator for Senso4s integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.util import dt as dt_util
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_EMPTY_WEIGHT,
    CONF_GAS_CAPACITY,
    CONF_HISTORY_POLL_INTERVAL,
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
    DOMAIN,
    MANUFACTURER,
    UNIT_LB,
    UsageMode,
    kg_to_lb,
)
from .models import HistoryRecord, Senso4sDeviceData
from .parser import parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)


class Senso4sDataUpdateCoordinator:
    """Coordinator for Senso4s device data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.address = address

        # Configuration
        self.empty_weight_kg: float = entry.options.get(
            CONF_EMPTY_WEIGHT,
            entry.data.get(CONF_EMPTY_WEIGHT, DEFAULT_EMPTY_WEIGHT),
        )
        self.gas_capacity_kg: float = entry.options.get(
            CONF_GAS_CAPACITY,
            entry.data.get(CONF_GAS_CAPACITY, DEFAULT_GAS_CAPACITY),
        )
        self.usage_mode: UsageMode = UsageMode.from_value(
            entry.options.get(
                CONF_USAGE_MODE,
                entry.data.get(CONF_USAGE_MODE, DEFAULT_USAGE_MODE),
            )
        )
        self.low_level_threshold: int = entry.options.get(
            CONF_LOW_LEVEL_THRESHOLD,
            entry.data.get(CONF_LOW_LEVEL_THRESHOLD, DEFAULT_LOW_LEVEL_THRESHOLD),
        )
        self.weight_unit: str = entry.options.get(
            CONF_WEIGHT_UNIT,
            entry.data.get(CONF_WEIGHT_UNIT, DEFAULT_WEIGHT_UNIT),
        )
        self.history_poll_interval: int = entry.options.get(
            CONF_HISTORY_POLL_INTERVAL,
            entry.data.get(CONF_HISTORY_POLL_INTERVAL, DEFAULT_HISTORY_POLL_INTERVAL),
        )

        # Current device data
        self.data: Senso4sDeviceData = Senso4sDeviceData()
        self.data.gas_capacity_kg = self.gas_capacity_kg
        self.data.empty_weight_kg = self.empty_weight_kg
        self.data.usage_mode = self.usage_mode

        # History data (from active connection)
        self.history: list[HistoryRecord] = []
        self.last_history_update: Optional[datetime] = None

        # Last known setup date (for change detection)
        # Restore from config entry data if available
        last_setup_str = entry.data.get(CONF_LAST_SETUP_DATE)
        self._last_known_setup_date: Optional[datetime] = None
        if last_setup_str:
            try:
                dt = datetime.fromisoformat(last_setup_str)
                # Ensure timezone-aware (assume UTC if missing)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                self._last_known_setup_date = dt
            except (ValueError, TypeError):
                pass

        # Service info for active connections
        self._service_info: Optional[BluetoothServiceInfoBleak] = None

        # Listeners
        self._listeners: list = []

    @property
    def device_name(self) -> str:
        """Get device name."""
        # Use a friendly name with last 4 chars of MAC for uniqueness
        suffix = self.address[-5:].replace(":", "")
        return f"Senso4s Gas Sensor ({suffix})"

    @property
    def device_info(self) -> dict[str, Any]:
        """Get device info for Home Assistant."""
        model = "Senso4s PLUS" if self.data.is_plus_model else "Senso4s BASIC"
        return {
            "identifiers": {(DOMAIN, self.address)},
            "name": self.device_name,
            "manufacturer": MANUFACTURER,
            "model": model,
            "connections": {("bluetooth", self.address)},
            "configuration_url": "https://github.com/ksanislo/senso4s_ble",
        }

    @property
    def service_info(self) -> Optional[BluetoothServiceInfoBleak]:
        """Get current service info."""
        return self._service_info

    @property
    def last_known_setup_date(self) -> Optional[datetime]:
        """Get the last known setup date."""
        return self._last_known_setup_date

    def update_setup_date(self, setup_date: Optional[datetime]) -> bool:
        """Update the setup date and return True if it changed."""
        if setup_date is None:
            return False

        # Check if this is a new date
        if self._last_known_setup_date is None:
            self._last_known_setup_date = setup_date
            return True

        # Compare dates (allow 1 second tolerance for rounding)
        if abs((setup_date - self._last_known_setup_date).total_seconds()) > 1:
            _LOGGER.debug(
                "Setup date changed: %s -> %s",
                self._last_known_setup_date,
                setup_date,
            )
            self._last_known_setup_date = setup_date
            return True

        return False

    def update_from_advertisement(
        self, service_info: BluetoothServiceInfoBleak
    ) -> bool:
        """Update data from BLE advertisement."""
        self._service_info = service_info

        # Log raw advertisement receipt
        rssi = getattr(service_info, "rssi", None)
        _LOGGER.debug(
            "BLE RX [ADVERTISEMENT] from %s (RSSI: %s dBm): %d manufacturer data entries",
            service_info.address,
            rssi if rssi is not None else "N/A",
            len(service_info.manufacturer_data),
        )

        # Parse manufacturer data
        for mfr_id, mfr_data in service_info.manufacturer_data.items():
            mfr_bytes = bytes(mfr_data)
            _LOGGER.debug(
                "BLE RX [MFR_DATA] mfr_id=0x%04X (%d): %s (%d bytes)",
                mfr_id,
                mfr_id,
                mfr_bytes.hex(" "),
                len(mfr_bytes),
            )

            parsed = parse_manufacturer_data(mfr_id, mfr_bytes, service_info.name)
            if parsed is not None:
                # Merge parsed data with current data
                self.data.mac_address = parsed.mac_address
                self.data.name = parsed.name or self.data.name
                self.data.gas_level_percent = parsed.gas_level_percent
                self.data.battery_percent = parsed.battery_percent
                self.data.usage_mode = parsed.usage_mode
                self.data.is_plus_model = parsed.is_plus_model
                self.data.needs_calibration = parsed.needs_calibration
                self.data.has_error = parsed.has_error
                self.data.error_code = parsed.error_code
                self.data.anomalies = parsed.anomalies
                self.data.last_seen = dt_util.now()

                # Keep configured values
                self.data.gas_capacity_kg = self.gas_capacity_kg
                self.data.empty_weight_kg = self.empty_weight_kg

                _LOGGER.debug(
                    "BLE RX [PARSED] level=%s%%, battery=%d%%, "
                    "gas_remaining=%.2f kg, mode=%s, model=%s, "
                    "needs_cal=%s, has_error=%s, anomalies=%s",
                    self.data.gas_level_percent if self.data.gas_level_percent >= 0 else "N/A",
                    self.data.battery_percent,
                    self.data.gas_remaining_kg or 0,
                    self.data.usage_mode.name,
                    "PLUS" if self.data.is_plus_model else "BASIC",
                    self.data.needs_calibration,
                    self.data.has_error,
                    self.data.anomaly_names if self.data.anomalies else "none",
                )
                return True

        return False

    def update_config(
        self,
        empty_weight_kg: Optional[float] = None,
        gas_capacity_kg: Optional[float] = None,
        usage_mode: Optional[UsageMode] = None,
        low_level_threshold: Optional[int] = None,
        weight_unit: Optional[str] = None,
        history_poll_interval: Optional[int] = None,
    ) -> None:
        """Update configuration values."""
        if empty_weight_kg is not None:
            self.empty_weight_kg = empty_weight_kg
            self.data.empty_weight_kg = empty_weight_kg
        if gas_capacity_kg is not None:
            self.gas_capacity_kg = gas_capacity_kg
            self.data.gas_capacity_kg = gas_capacity_kg
        if usage_mode is not None:
            self.usage_mode = usage_mode
        if low_level_threshold is not None:
            self.low_level_threshold = low_level_threshold
        if weight_unit is not None:
            self.weight_unit = weight_unit
        if history_poll_interval is not None:
            self.history_poll_interval = history_poll_interval

    @property
    def use_pounds(self) -> bool:
        """Return True if user prefers pounds."""
        return self.weight_unit == UNIT_LB

    def get_display_weight(self, kg_value: Optional[float]) -> Optional[float]:
        """Convert kg value to user's preferred unit."""
        if kg_value is None:
            return None
        if self.use_pounds:
            return round(kg_to_lb(kg_value), 2)
        return round(kg_value, 2)

    def update_history(self, history: list[HistoryRecord]) -> None:
        """Update history data."""
        self.history = history
        self.last_history_update = dt_util.now()
        _LOGGER.debug(
            "History updated: %d records, first=%s, last=%s",
            len(history),
            history[0].timestamp if history else None,
            history[-1].timestamp if history else None,
        )
        if history:
            _LOGGER.debug(
                "History gas values: first=%.2f kg, last=%.2f kg",
                history[0].remaining_gas_kg,
                history[-1].remaining_gas_kg,
            )

    @property
    def estimated_empty_date(self) -> Optional[datetime]:
        """Calculate estimated empty date based on consumption rate."""
        if len(self.history) < 2:
            _LOGGER.debug(
                "Estimated empty: not enough history (%d records, need at least 2). "
                "Use 'Refresh History' button to fetch history from device.",
                len(self.history),
            )
            return None

        # Get consumption rate from recent history
        recent = self.history[-10:]  # Last 10 records
        if len(recent) < 2:
            _LOGGER.debug("Estimated empty: recent history too short")
            return None

        first = recent[0]
        last = recent[-1]

        time_delta = (last.timestamp - first.timestamp).total_seconds()
        if time_delta <= 0:
            _LOGGER.debug("Estimated empty: time_delta <= 0")
            return None

        mass_delta = first.remaining_gas_kg - last.remaining_gas_kg
        if mass_delta <= 0:
            _LOGGER.debug(
                "Estimated empty: no consumption detected (mass_delta=%.2f kg)",
                mass_delta,
            )
            return None  # Not consuming

        # Calculate consumption rate (kg per second)
        rate_per_second = mass_delta / time_delta
        rate_per_day = rate_per_second * 86400

        _LOGGER.debug(
            "Estimated empty: consumption rate=%.3f kg/day over %.1f hours",
            rate_per_day,
            time_delta / 3600,
        )

        # Time to empty
        if self.data.gas_remaining_kg is not None and rate_per_second > 0:
            seconds_to_empty = self.data.gas_remaining_kg / rate_per_second
            estimated = dt_util.now() + timedelta(seconds=seconds_to_empty)
            _LOGGER.debug(
                "Estimated empty: %.2f kg remaining at %.3f kg/day = %s",
                self.data.gas_remaining_kg,
                rate_per_day,
                estimated,
            )
            return estimated

        _LOGGER.debug(
            "Estimated empty: gas_remaining_kg=%s, rate=%s",
            self.data.gas_remaining_kg,
            rate_per_second,
        )
        return None


def process_service_info(
    service_info: BluetoothServiceInfoBleak,
) -> Optional[Senso4sDeviceData]:
    """Process service info and extract device data."""
    for mfr_id, mfr_data in service_info.manufacturer_data.items():
        parsed = parse_manufacturer_data(mfr_id, bytes(mfr_data), service_info.name)
        if parsed is not None:
            return parsed
    return None
