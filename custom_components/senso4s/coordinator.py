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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EMPTY_WEIGHT,
    CONF_ENABLE_HISTORY_POLLING,
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
    ISSUE_PASSIVE_SCANNING,
    MANUFACTURER,
    PASSIVE_HISTORY_MAX_POINTS,
    REFILL_THRESHOLD_PERCENT,
    UNIT_LB,
    UsageMode,
    kg_to_lb,
)
from .models import HistoryRecord, Senso4sDeviceData
from .parser import interpret_level_byte, parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)

PROOF_OF_LIFE_INTERVAL = timedelta(seconds=30)
PASSIVE_HISTORY_STORAGE_VERSION = 1


def _format_mfr(info: Optional[BluetoothServiceInfoBleak]) -> str:
    """Format a cache's manufacturer data for logging, or note its absence."""
    if info is None:
        return "no-cache"
    if not info.manufacturer_data:
        return "EMPTY"
    return " ".join(
        f"{mid:04x}:{bytes(p).hex()}" for mid, p in info.manufacturer_data.items()
    )


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
        self.enable_history_polling: bool = entry.options.get(
            CONF_ENABLE_HISTORY_POLLING,
            entry.data.get(CONF_ENABLE_HISTORY_POLLING, True),
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
        self._last_polled_gas_level: Optional[int] = None
        self._last_proof_of_life_time: Optional[float] = None
        self._cancel_proof_of_life: Any = None

        # Passive-scan detection: True once any advert carries manufacturer data.
        self._advert_mfr_seen = False
        self._passive_scan_empty_count = 0
        self._passive_scan_issue_active = False

        # Passive history: rolling window of advert-based percentage observations
        self._passive_history: list[dict] = []
        self._last_passive_pct: Optional[int] = None
        self._passive_store: Store = Store(
            hass,
            PASSIVE_HISTORY_STORAGE_VERSION,
            f"senso4s_{address.replace(':', '')}_passive_history",
        )

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            # ACTIVE so HA commands adapters/proxies to solicit the scan
            # response, which is where the Senso4s mass/battery bytes live.
            # PASSIVE starves us of all advert data on proxies that have no
            # other reason to active-scan.
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
    def async_start_periodic_poll(self) -> None:
        pass

    @callback
    def async_stop_periodic_poll(self) -> None:
        pass

    async def async_load_passive_history(self) -> None:
        data = await self._passive_store.async_load()
        if data and isinstance(data, list):
            self._passive_history = data
            if self._passive_history:
                self._last_passive_pct = self._passive_history[-1]["pct"]
                _LOGGER.debug(
                    "[%s] Loaded %d passive history points (last pct=%d)",
                    self.address,
                    len(self._passive_history),
                    self._last_passive_pct,
                )

    @callback
    def _record_passive_data_point(
        self, gas_level_percent: int, timestamp: datetime
    ) -> None:
        if gas_level_percent < 0 or gas_level_percent > 100:
            return

        if self._last_passive_pct == gas_level_percent:
            return

        # Detect refill: level increased significantly
        if (
            self._last_passive_pct is not None
            and gas_level_percent
            > self._last_passive_pct + REFILL_THRESHOLD_PERCENT
        ):
            _LOGGER.debug(
                "[%s] Passive history: refill detected (%d%% -> %d%%), "
                "resetting window",
                self.address,
                self._last_passive_pct,
                gas_level_percent,
            )
            self._passive_history.clear()

        self._passive_history.append(
            {"t": timestamp.isoformat(), "pct": gas_level_percent}
        )
        if len(self._passive_history) > PASSIVE_HISTORY_MAX_POINTS:
            self._passive_history = self._passive_history[
                -PASSIVE_HISTORY_MAX_POINTS:
            ]
        self._last_passive_pct = gas_level_percent

        _LOGGER.debug(
            "[%s] Passive history: recorded %d%% (%d points)",
            self.address,
            gas_level_percent,
            len(self._passive_history),
        )
        self.hass.async_create_task(
            self._passive_store.async_save(list(self._passive_history))
        )

    @callback
    def _async_proof_of_life_tick(self, _now: Any) -> None:
        noncon = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=False
        )
        con = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        self._check_passive_scanning(con, noncon)
        # Track the freshest of the two caches; the percent byte may arrive on
        # the connectable advert while the non-connectable mirror lags.
        info = noncon
        if con is not None and (info is None or con.time > info.time):
            info = con
        if info is None:
            _LOGGER.debug(
                "[%s] BLE RX [SCANNER] no advertisement cached", self.address
            )
            return
        cur = info.time
        if (
            self._last_proof_of_life_time is not None
            and cur == self._last_proof_of_life_time
        ):
            _LOGGER.debug(
                "[%s] BLE RX [SCANNER] device silent — no new advertisement "
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
        self._last_proof_of_life_time = cur
        _LOGGER.debug(
            "[%s] BLE RX [SCANNER] via %s (RSSI: %s dBm, %.1fs since previous) "
            "name=%r connectable=%s tx_power=%s mfr[con]=%s mfr[noncon]=%s "
            "svc_uuids=%s svc_data=%s",
            self.address,
            info.source,
            getattr(info, "rssi", "?"),
            gap,
            info.name,
            info.connectable,
            getattr(info, "tx_power", "?"),
            _format_mfr(con),
            _format_mfr(noncon),
            list(info.service_uuids),
            {k: bytes(v).hex() for k, v in info.service_data.items()},
        )

        # Check if history cache is stale while the device is alive
        if (
            self.enable_history_polling
            and self.history_poll_interval > 0
            and not self._poll_in_flight
            and self._last_service_info is not None
        ):
            now = dt_util.now()
            stale = (
                self.last_history_update is None
                or (now - self.last_history_update).total_seconds()
                >= self.history_poll_interval * 60
            )
            if stale:
                self._debounced_poll.async_schedule_call()

    @callback
    def _check_passive_scanning(
        self,
        con: Optional[BluetoothServiceInfoBleak],
        noncon: Optional[BluetoothServiceInfoBleak],
    ) -> None:
        """Warn when a device is only reachable via a passive-scanning adapter.

        Senso4s carries its mass/battery bytes in the scan response, which only
        active scanning retrieves. If we keep seeing the device advertise with
        no manufacturer data, the adapter/proxy in range is passive-scanning.
        """
        has_mfr = bool(con and con.manufacturer_data) or bool(
            noncon and noncon.manufacturer_data
        )
        if has_mfr or self._advert_mfr_seen:
            self._passive_scan_empty_count = 0
            self._clear_passive_scan_issue()
            return
        if con is None and noncon is None:
            return
        self._passive_scan_empty_count += 1
        if (
            self._passive_scan_empty_count >= 3
            and not self._passive_scan_issue_active
        ):
            _LOGGER.warning(
                "[%s] Advertisements carry no manufacturer data even though the "
                "integration requests active scanning — the adapter/proxy "
                "reaching this device isn't active-scanning (it may not support "
                "it). The live gas level and battery never arrive; falling back "
                "to slower connected reads.",
                self.address,
            )
            self._raise_passive_scan_issue()

    def _raise_passive_scan_issue(self) -> None:
        self._passive_scan_issue_active = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_PASSIVE_SCANNING}_{self.address}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PASSIVE_SCANNING,
            translation_placeholders={"device_name": self.device_name},
        )

    def _clear_passive_scan_issue(self) -> None:
        if not self._passive_scan_issue_active:
            return
        self._passive_scan_issue_active = False
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_PASSIVE_SCANNING}_{self.address}"
        )

    @callback
    def _update_method(
        self, service_info: BluetoothServiceInfoBleak
    ) -> Senso4sDeviceData:
        mfr_hex = " ".join(
            f"{mid:04x}:{bytes(payload).hex()}"
            for mid, payload in service_info.manufacturer_data.items()
        )
        _LOGGER.debug(
            "[%s] BLE RX [DISPATCH] via %s (RSSI: %s dBm): %s",
            self.address,
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
            self._advert_mfr_seen = True
            self._clear_passive_scan_issue()
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
                "[%s] BLE RX [PARSED] level=%s%% battery=%d%% mode=%s model=%s "
                "needs_cal=%s has_error=%s anomalies=%s",
                self.address,
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
            self._record_passive_data_point(
                parsed.gas_level_percent, self.data.last_seen
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
        if not self.enable_history_polling:
            return False
        # Only poll when gas level actually changed — dispatch can fire on
        # debug-byte or anomaly-flag changes that don't affect history.
        if (
            self._last_polled_gas_level is not None
            and self.data.gas_level_percent == self._last_polled_gas_level
        ):
            return False
        return True

    async def _async_poll_history(
        self, last_service_info: BluetoothServiceInfoBleak
    ) -> Senso4sDeviceData:
        from .ble_client import Senso4sBLEClient

        self._poll_in_flight = True
        client = Senso4sBLEClient(last_service_info)
        try:
            if not await client.connect():
                _LOGGER.debug(
                    "[%s] Poll: connect failed", self.address
                )
                return self.data

            setup_date = await client.read_setup_date()
            if setup_date is None and self._last_known_setup_date is not None:
                setup_date = self._last_known_setup_date
            if setup_date is None:
                _LOGGER.debug(
                    "[%s] Poll: no setup date available; skipping history read",
                    self.address,
                )
                return self.data

            if self.update_setup_date(setup_date):
                await self._sync_config_from_device(client, setup_date)

            history = await client.read_history(setup_date)
            self.update_history(history)

            # Passive-scanning adapters never deliver the advertisement mass
            # byte, so fill it from the level characteristic we can reach now.
            if not self._advert_mfr_seen or self.data.gas_level_percent < 0:
                mass_byte = await client.read_mass_level()
                if mass_byte is not None:
                    self._apply_mass_byte(mass_byte)

            self._last_polled_gas_level = self.data.gas_level_percent
        finally:
            await client.disconnect()
            self._poll_in_flight = False
        return self.data

    def _apply_mass_byte(self, level_byte: int) -> None:
        """Populate level state from a connected read of characteristic 00007082."""
        gas_level, needs_cal, has_error, error_code, anomalies = interpret_level_byte(
            level_byte
        )
        _LOGGER.debug(
            "[%s] Connected mass read: byte=0x%02X -> level=%s",
            self.address,
            level_byte,
            gas_level if gas_level >= 0 else "N/A",
        )
        self.data.gas_level_percent = gas_level
        self.data.needs_calibration = needs_cal
        self.data.has_error = has_error
        self.data.error_code = error_code
        self.data.anomalies = anomalies
        self.data.last_seen = dt_util.now()
        if gas_level >= 0:
            self._record_passive_data_point(gas_level, self.data.last_seen)

    async def _sync_config_from_device(
        self, client, setup_date: datetime
    ) -> None:
        """Read cylinder config from device and persist after external change."""
        config = await client.read_config()
        if config is None:
            return

        _LOGGER.info(
            "[%s] External config change detected: "
            "empty_weight=%.2f kg, gas_capacity=%.2f kg",
            self.address,
            config.empty_weight_kg,
            config.gas_capacity_kg,
        )
        self.update_config(
            empty_weight_kg=config.empty_weight_kg,
            gas_capacity_kg=config.gas_capacity_kg,
        )
        new_data = {
            **self.entry.data,
            CONF_LAST_SETUP_DATE: setup_date.isoformat(),
        }
        new_options = {
            **self.entry.options,
            CONF_EMPTY_WEIGHT: config.empty_weight_kg,
            CONF_GAS_CAPACITY: config.gas_capacity_kg,
        }
        try:
            self.hass.config_entries.async_update_entry(
                self.entry, data=new_data, options=new_options
            )
        except Exception as err:
            _LOGGER.warning(
                "[%s] Failed to persist config: %s", self.address, err
            )

    def update_setup_date(self, setup_date: Optional[datetime]) -> bool:
        if setup_date is None:
            return False
        if self._last_known_setup_date is None:
            self._last_known_setup_date = setup_date
            return True
        if abs((setup_date - self._last_known_setup_date).total_seconds()) > 1:
            _LOGGER.debug(
                "[%s] Setup date changed: %s -> %s",
                self.address,
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
            "[%s] History updated: %d records, first=%s, last=%s",
            self.address,
            len(history),
            history[0].timestamp if history else None,
            history[-1].timestamp if history else None,
        )
        if history:
            _LOGGER.debug(
                "[%s] History gas values: first=%.2f kg, last=%.2f kg",
                self.address,
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
        enable_history_polling: Optional[bool] = None,
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
        if enable_history_polling is not None:
            self.enable_history_polling = enable_history_polling
        if history_poll_interval is not None:
            self.history_poll_interval = history_poll_interval

    @callback
    def async_request_refresh(self) -> None:
        self._last_polled_gas_level = None
        self.last_history_update = None
        if self.enable_history_polling:
            self._debounced_poll.async_schedule_call()

    @property
    def use_pounds(self) -> bool:
        return self.weight_unit == UNIT_LB

    def get_display_weight(self, kg_value: Optional[float]) -> Optional[float]:
        if kg_value is None:
            return None
        if self.use_pounds:
            return round(kg_to_lb(kg_value), 2)
        return round(kg_value, 2)

    def _regression_empty_estimate(
        self,
        timestamps: list[datetime],
        kg_values: list[float],
        label: str,
    ) -> Optional[datetime]:
        n = len(timestamps)
        if n < 2:
            _LOGGER.debug(
                "[%s] Estimated empty [%s]: not enough data (%d points, need 2+)",
                self.address,
                label,
                n,
            )
            return None

        base_t = timestamps[0]
        xs = [(t - base_t).total_seconds() for t in timestamps]
        ys = kg_values

        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)

        denom = n * sum_xx - sum_x * sum_x
        if denom <= 0:
            _LOGGER.debug(
                "[%s] Estimated empty [%s]: degenerate window (denom=%.2f, n=%d)",
                self.address,
                label,
                denom,
                n,
            )
            return None

        slope = (n * sum_xy - sum_x * sum_y) / denom
        if slope >= 0:
            _LOGGER.debug(
                "[%s] Estimated empty [%s]: slope >= 0, not consuming "
                "(slope=%.6g kg/s)",
                self.address,
                label,
                slope,
            )
            return None

        last_mass = ys[-1]
        if last_mass <= 0:
            _LOGGER.debug(
                "[%s] Estimated empty [%s]: last recorded mass <= 0 (%s)",
                self.address,
                label,
                last_mass,
            )
            return None

        seconds_until_empty = -last_mass / slope
        estimated = timestamps[-1] + timedelta(seconds=seconds_until_empty)
        _LOGGER.debug(
            "[%s] Estimated empty [%s]: n=%d, slope=%.6g kg/s, last_mass=%.3f kg "
            "@ %s → %s",
            self.address,
            label,
            n,
            slope,
            last_mass,
            timestamps[-1],
            estimated,
        )
        return estimated

    @property
    def estimated_empty_date(self) -> Optional[datetime]:
        if self.enable_history_polling:
            if len(self.history) >= 2:
                recent = self.history[-min(10, len(self.history)):]
                return self._regression_empty_estimate(
                    [r.timestamp for r in recent],
                    [r.remaining_gas_kg for r in recent],
                    "active",
                )
            return None

        if len(self._passive_history) >= 2:
            timestamps = [
                datetime.fromisoformat(p["t"]) for p in self._passive_history
            ]
            kg_values = [
                p["pct"] / 100.0 * self.gas_capacity_kg
                for p in self._passive_history
            ]
            return self._regression_empty_estimate(
                timestamps, kg_values, "passive"
            )

        _LOGGER.debug(
            "[%s] Estimated empty: no data available "
            "(active=%d records, passive=%d points)",
            self.address,
            len(self.history),
            len(self._passive_history),
        )
        return None


def process_service_info(
    service_info: BluetoothServiceInfoBleak,
) -> Optional[Senso4sDeviceData]:
    """Parse a service_info into Senso4sDeviceData (used by config flow)."""
    for mfr_id, mfr_data in service_info.manufacturer_data.items():
        parsed = parse_manufacturer_data(mfr_id, bytes(mfr_data), service_info.name)
        if parsed is not None:
            return parsed
    return None
