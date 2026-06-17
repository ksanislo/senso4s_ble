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

"""BLE client for active connections to Senso4s devices."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime  # For type hints only
from typing import Callable, Optional

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.util import dt as dt_util

from .const import (
    CALIBRATION_LEVEL_WAIT_TIMEOUT,
    CHAR_CALIBRATION_UUID,
    CHAR_CONFIG_UUID,
    CHAR_HISTORY_UUID,
    CHAR_LEVEL_UUID,
    CHAR_SETUP_DATE_UUID,
    CONNECTION_TIMEOUT,
    NOTIFICATION_TIMEOUT,
    AnomalyType,
    DeviceError,
    UsageMode,
)
from .models import CylinderConfig, HistoryRecord
from .parser import (
    build_cylinder_config,
    build_setup_date,
    parse_cylinder_config,
    parse_history_data,
    parse_setup_date,
)

_LOGGER = logging.getLogger(__name__)


class Senso4sBLEClient:
    """Client for connected BLE operations with Senso4s devices."""

    def __init__(
        self,
        service_info: BluetoothServiceInfoBleak,
        disconnect_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Initialize the client."""
        self._service_info = service_info
        self._client: Optional[BleakClient] = None
        self._disconnect_callback = disconnect_callback
        self._lock = asyncio.Lock()
        self._addr = service_info.address

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._client is not None and self._client.is_connected

    async def connect(self) -> bool:
        """Connect to the device."""
        async with self._lock:
            if self.is_connected:
                return True

            try:
                self._client = await establish_connection(
                    BleakClient,
                    self._service_info.device,
                    self._service_info.address,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=3,
                )
                _LOGGER.debug("[%s] Connected", self._addr)
                return True
            except (BleakError, TimeoutError) as err:
                _LOGGER.warning(
                    "[%s] Failed to connect: %s", self._addr, err
                )
                self._client = None
                return False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except BleakError:
                    pass
                self._client = None

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Handle disconnection."""
        _LOGGER.debug("[%s] Disconnected", self._addr)
        self._client = None
        if self._disconnect_callback:
            self._disconnect_callback()

    async def read_config(self) -> Optional[CylinderConfig]:
        """Read cylinder configuration from device."""
        if not self.is_connected or self._client is None:
            return None

        try:
            _LOGGER.debug("[%s] BLE: Reading CONFIG characteristic", self._addr)
            data = await self._client.read_gatt_char(CHAR_CONFIG_UUID)
            _LOGGER.debug("[%s] BLE RX [CONFIG]: %s (%d bytes)", self._addr, data.hex(" "), len(data))
            config = parse_cylinder_config(data)
            if config:
                _LOGGER.debug(
                    "[%s] Config: empty_weight=%.2f kg, gas_capacity=%.2f kg, mode=%s",
                    self._addr,
                    config.empty_weight_kg,
                    config.gas_capacity_kg,
                    config.usage_mode.name,
                )
            return config
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to read config: %s", self._addr, err)
            return None

    async def read_setup_date(self) -> Optional[datetime]:
        """Read setup date from device."""
        if not self.is_connected or self._client is None:
            return None

        try:
            _LOGGER.debug("[%s] BLE: Reading SETUP_DATE characteristic", self._addr)
            data = await self._client.read_gatt_char(CHAR_SETUP_DATE_UUID)
            _LOGGER.debug("[%s] BLE RX [SETUP_DATE]: %s (%d bytes)", self._addr, data.hex(" "), len(data))
            setup_date = parse_setup_date(data)
            _LOGGER.debug("[%s] Setup date: parsed as %s", self._addr, setup_date)
            return setup_date
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to read setup date: %s", self._addr, err)
            return None

    async def is_device_configured(self) -> Optional[bool]:
        """Check if device has been configured (setup date is not all zeros).

        Returns:
            True if configured (setup date has real values)
            False if unconfigured (setup date is all zeros)
            None if failed to read
        """
        if not self.is_connected or self._client is None:
            return None

        try:
            _LOGGER.debug("[%s] BLE: Reading SETUP_DATE to check if configured", self._addr)
            data = await self._client.read_gatt_char(CHAR_SETUP_DATE_UUID)
            _LOGGER.debug("[%s] BLE RX [SETUP_DATE]: %s (%d bytes)", self._addr, data.hex(" "), len(data))

            if len(data) != 7:
                _LOGGER.warning("[%s] Setup date has unexpected length: %d", self._addr, len(data))
                return None

            if all(b == 0 for b in data):
                _LOGGER.info("[%s] Device is unconfigured (setup date is all zeros)", self._addr)
                return False

            _LOGGER.debug("[%s] Device is configured (setup date has values)", self._addr)
            return True
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to read setup date: %s", self._addr, err)
            return None

    async def read_level(self, timeout: float = NOTIFICATION_TIMEOUT) -> Optional[int]:
        """Read current level via notifications."""
        if not self.is_connected or self._client is None:
            return None

        result: Optional[int] = None
        event = asyncio.Event()

        addr = self._addr

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result
            _LOGGER.debug("[%s] BLE RX [LEVEL]: %s", addr, data.hex(" ") if data else "(empty)")
            if data and len(data) > 0 and data[0] != 255:
                result = data[0]
                event.set()

        try:
            _LOGGER.debug("[%s] BLE: Starting notifications on LEVEL characteristic", self._addr)
            await self._client.start_notify(CHAR_LEVEL_UUID, notification_handler)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("[%s] Timeout waiting for level notification", self._addr)
            finally:
                await self._client.stop_notify(CHAR_LEVEL_UUID)
            return result
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to read level: %s", self._addr, err)
            return None

    async def read_history(
        self, setup_date: datetime, timeout: float = NOTIFICATION_TIMEOUT
    ) -> list[HistoryRecord]:
        """Read consumption history from device."""
        if not self.is_connected or self._client is None:
            return []

        collected_data = bytearray()
        last_receive_time = dt_util.now()
        start_time = dt_util.now()
        addr = self._addr

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal last_receive_time
            _LOGGER.debug("[%s] BLE RX [HISTORY]: %s", addr, data.hex(" ") if data else "(empty)")
            if data:
                collected_data.extend(data)
                last_receive_time = dt_util.now()

        try:
            _LOGGER.debug("[%s] BLE: Starting notifications on HISTORY characteristic", self._addr)
            await self._client.start_notify(CHAR_HISTORY_UUID, notification_handler)

            write_data = bytes([0x00, 0x00])
            _LOGGER.debug("[%s] BLE TX [HISTORY]: %s", self._addr, write_data.hex(" "))
            await self._client.write_gatt_char(CHAR_HISTORY_UUID, write_data)

            while True:
                await asyncio.sleep(0.1)
                now = dt_util.now()
                elapsed_since_data = (now - last_receive_time).total_seconds()
                elapsed_total = (now - start_time).total_seconds()

                if len(collected_data) > 0 and elapsed_since_data > 1.0:
                    break
                if elapsed_total > timeout:
                    break

            await self._client.stop_notify(CHAR_HISTORY_UUID)

            _LOGGER.debug(
                "[%s] History: received %d bytes of raw data: %s",
                self._addr,
                len(collected_data),
                collected_data.hex(" ") if len(collected_data) <= 100 else f"{collected_data[:100].hex(' ')}... (truncated)",
            )

            records = parse_history_data(bytes(collected_data), setup_date)
            _LOGGER.debug(
                "[%s] History: parsed %d records from setup_date=%s",
                self._addr,
                len(records),
                setup_date,
            )
            return records

        except BleakError as err:
            _LOGGER.warning("[%s] Failed to read history: %s", self._addr, err)
            return []

    async def calibrate(
        self, timeout: float = NOTIFICATION_TIMEOUT
    ) -> tuple[bool, list[AnomalyType]]:
        """
        Perform device calibration.

        Returns:
            Tuple of (success, list of anomalies detected)
        """
        if not self.is_connected or self._client is None:
            return False, []

        result: Optional[int] = None
        event = asyncio.Event()
        addr = self._addr

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result
            _LOGGER.debug("[%s] BLE RX [CALIBRATION]: %s", addr, data.hex(" ") if data else "(empty)")
            if data and len(data) > 0:
                result = data[0]
                event.set()

        try:
            _LOGGER.debug("[%s] BLE: Starting notifications on CALIBRATION characteristic", self._addr)
            await self._client.start_notify(
                CHAR_CALIBRATION_UUID, notification_handler
            )

            write_data = bytes([0x01])
            _LOGGER.debug("[%s] BLE TX [CALIBRATION]: %s", self._addr, write_data.hex(" "))
            await self._client.write_gatt_char(CHAR_CALIBRATION_UUID, write_data)

            success = False
            anomalies: list[AnomalyType] = []

            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                if result == 0:
                    success = True
                elif result is not None:
                    upper = (result >> 4) & 0x0F
                    for anomaly in AnomalyType:
                        if upper & anomaly.value:
                            anomalies.append(anomaly)
            except asyncio.TimeoutError:
                _LOGGER.warning("[%s] Calibration timed out", self._addr)

            await self._client.stop_notify(CHAR_CALIBRATION_UUID)

            return success, anomalies

        except BleakError as err:
            _LOGGER.warning("[%s] Failed to calibrate: %s", self._addr, err)
            return False, []

    async def write_config(
        self,
        empty_weight_kg: float,
        gas_capacity_kg: float,
        usage_mode: UsageMode,
    ) -> bool:
        """Write cylinder configuration to device."""
        if not self.is_connected or self._client is None:
            return False

        try:
            config_bytes = build_cylinder_config(
                empty_weight_kg, gas_capacity_kg, usage_mode
            )
            _LOGGER.debug("[%s] BLE TX [CONFIG]: %s", self._addr, config_bytes.hex(" "))
            await self._client.write_gatt_char(CHAR_CONFIG_UUID, config_bytes)
            return True
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to write config: %s", self._addr, err)
            return False

    async def write_setup_date(self, dt: datetime) -> bool:
        """Write setup date to device."""
        if not self.is_connected or self._client is None:
            return False

        try:
            date_bytes = build_setup_date(dt)
            _LOGGER.debug("[%s] BLE TX [SETUP_DATE]: %s", self._addr, date_bytes.hex(" "))
            await self._client.write_gatt_char(CHAR_SETUP_DATE_UUID, date_bytes)
            return True
        except BleakError as err:
            _LOGGER.warning("[%s] Failed to write setup date: %s", self._addr, err)
            return False

    async def wait_for_valid_level(
        self, timeout: float = CALIBRATION_LEVEL_WAIT_TIMEOUT
    ) -> tuple[bool, int | None, list[AnomalyType]]:
        """
        Subscribe to level characteristic and wait for a valid level value.

        Waits for a level value != 255 (needs calibration), interpreting the
        result according to the device protocol.

        Args:
            timeout: Maximum time to wait for a valid level

        Returns:
            Tuple of (success, level_value, anomalies)
            - success: True if a valid level (0-100) was received
            - level_value: The level percentage (0-100), or None if error/anomaly
            - anomalies: List of anomalies if level indicates anomaly condition
        """
        if not self.is_connected or self._client is None:
            return False, None, []

        result_level: int | None = None
        event = asyncio.Event()
        addr = self._addr

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result_level
            _LOGGER.debug("[%s] BLE RX [LEVEL]: %s", addr, data.hex(" ") if data else "(empty)")
            if data and len(data) > 0:
                level = data[0]
                if level != 255:
                    result_level = level
                    event.set()

        try:
            _LOGGER.debug("[%s] BLE: Starting notifications on LEVEL (wait_for_valid_level)", self._addr)
            await self._client.start_notify(CHAR_LEVEL_UUID, notification_handler)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("[%s] Timeout waiting for valid level notification", self._addr)
                return False, None, []
            finally:
                try:
                    await self._client.stop_notify(CHAR_LEVEL_UUID)
                except BleakError:
                    pass

            if result_level is None:
                return False, None, []

            if 0 <= result_level <= 100:
                return True, result_level, []
            elif 241 <= result_level <= 247:
                anomaly_flags = result_level - 240
                anomalies: list[AnomalyType] = []
                for anomaly in AnomalyType:
                    if anomaly_flags & anomaly.value:
                        anomalies.append(anomaly)
                return False, None, anomalies
            elif 251 <= result_level <= 254:
                _LOGGER.warning("[%s] Device reported error code: %d", self._addr, result_level)
                return False, None, []
            else:
                _LOGGER.warning("[%s] Device reported unknown level: %d", self._addr, result_level)
                return False, None, []

        except BleakError as err:
            _LOGGER.warning("[%s] Failed to wait for level: %s", self._addr, err)
            return False, None, []
