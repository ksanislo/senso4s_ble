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

"""Senso4s ActiveBluetoothProcessorCoordinator."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

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

PROOF_OF_LIFE_INTERVAL = timedelta(seconds=30)


class Senso4sCoordinator(ActiveBluetoothProcessorCoordinator[Senso4sDeviceData]):
    """ActiveBluetooth coordinator for a single Senso4s device."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address

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

        self.data: Senso4sDeviceData = Senso4sDeviceData()
        self.data.gas_capacity_kg = self.gas_capacity_kg
        self.data.empty_weight_kg = self.empty_weight_kg
        self.data.usage_mode = self.usage_mode

        self.history: list[HistoryRecord] = []
        self.last_history_update: Optional[datetime] = None

        last_setup_str = entry.data.get(CONF_LAST_SETUP_DATE)
        self._last_known_setup_date: Optional[datetime] = None
        if last_setup_str:
            try:
                dt = datetime.fromisoformat(last_setup_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                self._last_known_setup_date = dt
            except (ValueError, TypeError):
                pass

        self._poll_in_flight = False
        self._last_proof_of_life_time: Optional[float] = None
        self._cancel_proof_of_life: Any = None

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            mode=BluetoothScanningMode.ACTIVE,
            update_method=self._update_method,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll_history,
            connectable=True,
        )

    @property
    def device_name(self) -> str:
        suffix = self.address[-5:].replace(":", "")
        return f"Senso4s Gas Sensor ({suffix})"

    @property
    def name(self) -> str:
        # PassiveBluetoothProcessorEntity reads this for the device registry name.
        return self.device_name

    @property
    def device_info(self) -> dict[str, Any]:
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
        return self._last_service_info

    @property
    def last_known_setup_date(self) -> Optional[datetime]:
        return self._last_known_setup_date

    @callback
    def async_start_proof_of_life(self) -> None:
        if self._cancel_proof_of_life is not None:
            return
        self._cancel_proof_of_life = async_track_time_interval(
            self.hass, self._async_proof_of_life_tick, PROOF_OF_LIFE_INTERVAL
        )

    @callback
    def async_stop_proof_of_life(self) -> None:
        if self._cancel_proof_of_life is not None:
            self._cancel_proof_of_life()
            self._cancel_proof_of_life = None

    @callback
    def _async_proof_of_life_tick(self, _now: Any) -> None:
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=False
        )
        if info is None:
            _LOGGER.debug(
                "BLE RX [SCANNER] %s: no advertisement cached", self.address
            )
            return
        cur = info.time
        if (
            self._last_proof_of_life_time is not None
            and cur == self._last_proof_of_life_time
        ):
            _LOGGER.debug(
                "BLE RX [SCANNER] %s: device silent — no new advertisement "
                "in the last %.0fs",
                self.address,
                PROOF_OF_LIFE_INTERVAL.total_seconds(),
            )
            return
        gap = (
            cur - self._last_proof_of_life_time
            if self._last_proof_of_life_time is not None
            else 0.0
        )
        mfr_hex = " ".join(
            f"{mid:04x}:{bytes(payload).hex()}"
            for mid, payload in info.manufacturer_data.items()
        )
        _LOGGER.debug(
            "BLE RX [SCANNER] from %s via %s (RSSI: %s dBm, %.1fs since "
            "previous): %s",
            info.address,
            info.source,
            getattr(info, "rssi", "?"),
            gap,
            mfr_hex,
        )
        self._last_proof_of_life_time = cur

    @callback
    def _update_method(
        self, service_info: BluetoothServiceInfoBleak
    ) -> Senso4sDeviceData:
        mfr_hex = " ".join(
            f"{mid:04x}:{bytes(payload).hex()}"
            for mid, payload in service_info.manufacturer_data.items()
        )
        _LOGGER.debug(
            "BLE RX [DISPATCH] from %s via %s (RSSI: %s dBm): %s",
            service_info.address,
            service_info.source,
            getattr(service_info, "rssi", "?"),
            mfr_hex,
        )

        for mfr_id, mfr_data in service_info.manufacturer_data.items():
            parsed = parse_manufacturer_data(
                mfr_id, bytes(mfr_data), service_info.name
            )
            if parsed is None:
                continue
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
            self.data.gas_capacity_kg = self.gas_capacity_kg
            self.data.empty_weight_kg = self.empty_weight_kg
            _LOGGER.debug(
                "BLE RX [PARSED] level=%s%% battery=%d%% mode=%s model=%s "
                "needs_cal=%s has_error=%s anomalies=%s",
                self.data.gas_level_percent
                if self.data.gas_level_percent >= 0
                else "N/A",
                self.data.battery_percent,
                self.data.usage_mode.name,
                "PLUS" if self.data.is_plus_model else "BASIC",
                self.data.needs_calibration,
                self.data.has_error,
                self.data.anomaly_names if self.data.anomalies else "none",
            )
            break
        return self.data

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        if self.hass.is_stopping:
            return False
        if self._poll_in_flight:
            return False
        if self.history_poll_interval <= 0:
            return False
        if seconds_since_last_poll is None:
            return True
        return seconds_since_last_poll >= self.history_poll_interval * 60

    async def _async_poll_history(
        self, last_service_info: BluetoothServiceInfoBleak
    ) -> Senso4sDeviceData:
        from .ble_client import Senso4sBLEClient

        self._poll_in_flight = True
        client = Senso4sBLEClient(last_service_info)
        try:
            if not await client.connect():
                _LOGGER.debug("Poll: connect failed for %s", self.address)
                return self.data

            setup_date = await client.read_setup_date()
            if setup_date is None and self._last_known_setup_date is not None:
                setup_date = self._last_known_setup_date
            if setup_date is None:
                _LOGGER.debug(
                    "Poll: no setup date available; skipping history read"
                )
                return self.data

            self.update_setup_date(setup_date)
            history = await client.read_history(setup_date)
            self.update_history(history)
        finally:
            await client.disconnect()
            self._poll_in_flight = False
        return self.data

    def update_setup_date(self, setup_date: Optional[datetime]) -> bool:
        if setup_date is None:
            return False
        if self._last_known_setup_date is None:
            self._last_known_setup_date = setup_date
            return True
        if abs((setup_date - self._last_known_setup_date).total_seconds()) > 1:
            _LOGGER.debug(
                "Setup date changed: %s -> %s",
                self._last_known_setup_date,
                setup_date,
            )
            self._last_known_setup_date = setup_date
            return True
        return False

    def update_history(self, history: list[HistoryRecord]) -> None:
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

    def update_config(
        self,
        empty_weight_kg: Optional[float] = None,
        gas_capacity_kg: Optional[float] = None,
        usage_mode: Optional[UsageMode] = None,
        low_level_threshold: Optional[int] = None,
        weight_unit: Optional[str] = None,
        history_poll_interval: Optional[int] = None,
    ) -> None:
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
        return self.weight_unit == UNIT_LB

    def get_display_weight(self, kg_value: Optional[float]) -> Optional[float]:
        if kg_value is None:
            return None
        if self.use_pounds:
            return round(kg_to_lb(kg_value), 2)
        return round(kg_value, 2)

    @property
    def estimated_empty_date(self) -> Optional[datetime]:
        if len(self.history) < 2:
            _LOGGER.debug(
                "Estimated empty: not enough history (%d records, need 2+)",
                len(self.history),
            )
            return None

        recent = self.history[-min(10, len(self.history)):]
        n = len(recent)
        base_t = recent[0].timestamp
        xs = [(r.timestamp - base_t).total_seconds() for r in recent]
        ys = [r.remaining_gas_kg for r in recent]

        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)

        denom = n * sum_xx - sum_x * sum_x
        if denom <= 0:
            _LOGGER.debug(
                "Estimated empty: degenerate window (denom=%.2f, n=%d)",
                denom,
                n,
            )
            return None

        slope = (n * sum_xy - sum_x * sum_y) / denom
        if slope >= 0:
            _LOGGER.debug(
                "Estimated empty: slope >= 0, not consuming (slope=%.6g kg/s)",
                slope,
            )
            return None

        last_mass = ys[-1]
        if last_mass <= 0:
            _LOGGER.debug("Estimated empty: last recorded mass <= 0 (%s)", last_mass)
            return None

        seconds_until_empty = -last_mass / slope
        estimated = recent[-1].timestamp + timedelta(seconds=seconds_until_empty)
        _LOGGER.debug(
            "Estimated empty: n=%d, slope=%.6g kg/s, last_mass=%.3f kg @ %s → %s",
            n,
            slope,
            last_mass,
            recent[-1].timestamp,
            estimated,
        )
        return estimated


def process_service_info(
    service_info: BluetoothServiceInfoBleak,
) -> Optional[Senso4sDeviceData]:
    """Parse a service_info into Senso4sDeviceData (used by config flow)."""
    for mfr_id, mfr_data in service_info.manufacturer_data.items():
        parsed = parse_manufacturer_data(mfr_id, bytes(mfr_data), service_info.name)
        if parsed is not None:
            return parsed
    return None
