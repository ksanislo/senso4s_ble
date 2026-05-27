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
                _LOGGER.debug("Connected to %s", self._service_info.address)
                return True
            except (BleakError, TimeoutError) as err:
                _LOGGER.warning(
                    "Failed to connect to %s: %s",
                    self._service_info.address,
                    err,
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
        _LOGGER.debug("Disconnected from %s", self._service_info.address)
        self._client = None
        if self._disconnect_callback:
            self._disconnect_callback()

    async def read_config(self) -> Optional[CylinderConfig]:
        """Read cylinder configuration from device."""
        if not self.is_connected or self._client is None:
            return None

        try:
            _LOGGER.debug("BLE: Reading CONFIG characteristic")
            data = await self._client.read_gatt_char(CHAR_CONFIG_UUID)
            _LOGGER.debug("BLE RX [CONFIG]: %s (%d bytes)", data.hex(" "), len(data))
            config = parse_cylinder_config(data)
            if config:
                _LOGGER.debug(
                    "Config: empty_weight=%.2f kg, gas_capacity=%.2f kg, mode=%s",
                    config.empty_weight_kg,
                    config.gas_capacity_kg,
                    config.usage_mode.name,
                )
            return config
        except BleakError as err:
            _LOGGER.warning("Failed to read config: %s", err)
            return None

    async def read_setup_date(self) -> Optional[datetime]:
        """Read setup date from device."""
        if not self.is_connected or self._client is None:
            return None

        try:
            _LOGGER.debug("BLE: Reading SETUP_DATE characteristic")
            data = await self._client.read_gatt_char(CHAR_SETUP_DATE_UUID)
            _LOGGER.debug("BLE RX [SETUP_DATE]: %s (%d bytes)", data.hex(" "), len(data))
            setup_date = parse_setup_date(data)
            _LOGGER.debug("Setup date: parsed as %s", setup_date)
            return setup_date
        except BleakError as err:
            _LOGGER.warning("Failed to read setup date: %s", err)
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
            _LOGGER.debug("BLE: Reading SETUP_DATE to check if configured")
            data = await self._client.read_gatt_char(CHAR_SETUP_DATE_UUID)
            _LOGGER.debug("BLE RX [SETUP_DATE]: %s (%d bytes)", data.hex(" "), len(data))

            if len(data) != 7:
                _LOGGER.warning("Setup date has unexpected length: %d", len(data))
                return None

            # Check if all zeros (unconfigured)
            if all(b == 0 for b in data):
                _LOGGER.info("Device is unconfigured (setup date is all zeros)")
                return False

            _LOGGER.debug("Device is configured (setup date has values)")
            return True
        except BleakError as err:
            _LOGGER.warning("Failed to read setup date: %s", err)
            return None

    async def read_level(self, timeout: float = NOTIFICATION_TIMEOUT) -> Optional[int]:
        """Read current level via notifications."""
        if not self.is_connected or self._client is None:
            return None

        result: Optional[int] = None
        event = asyncio.Event()

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result
            _LOGGER.debug("BLE RX [LEVEL]: %s", data.hex(" ") if data else "(empty)")
            if data and len(data) > 0 and data[0] != 255:
                result = data[0]
                event.set()

        try:
            _LOGGER.debug("BLE: Starting notifications on LEVEL characteristic")
            await self._client.start_notify(CHAR_LEVEL_UUID, notification_handler)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("Timeout waiting for level notification")
            finally:
                await self._client.stop_notify(CHAR_LEVEL_UUID)
            return result
        except BleakError as err:
            _LOGGER.warning("Failed to read level: %s", err)
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

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal last_receive_time
            _LOGGER.debug("BLE RX [HISTORY]: %s", data.hex(" ") if data else "(empty)")
            if data:
                collected_data.extend(data)
                last_receive_time = dt_util.now()

        try:
            _LOGGER.debug("BLE: Starting notifications on HISTORY characteristic")
            await self._client.start_notify(CHAR_HISTORY_UUID, notification_handler)

            # Trigger history read
            write_data = bytes([0x00, 0x00])
            _LOGGER.debug("BLE TX [HISTORY]: %s", write_data.hex(" "))
            await self._client.write_gatt_char(CHAR_HISTORY_UUID, write_data)

            # Wait for data with timeout after last received chunk
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
                "History: received %d bytes of raw data: %s",
                len(collected_data),
                collected_data.hex(" ") if len(collected_data) <= 100 else f"{collected_data[:100].hex(' ')}... (truncated)",
            )

            records = parse_history_data(bytes(collected_data), setup_date)
            _LOGGER.debug(
                "History: parsed %d records from setup_date=%s",
                len(records),
                setup_date,
            )
            return records

        except BleakError as err:
            _LOGGER.warning("Failed to read history: %s", err)
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

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result
            _LOGGER.debug("BLE RX [CALIBRATION]: %s", data.hex(" ") if data else "(empty)")
            if data and len(data) > 0:
                # Per protocol §3.4 the result byte after zeroing is:
                #   0x00         = success
                #   0x01         = unknown scenario (BASIC and PLUS)
                #   0x10/20/40   = anomaly bits in upper nibble (PLUS only)
                # Accept any value here; success vs failure is decided below.
                result = data[0]
                event.set()

        try:
            _LOGGER.debug("BLE: Starting notifications on CALIBRATION characteristic")
            await self._client.start_notify(
                CHAR_CALIBRATION_UUID, notification_handler
            )

            # Start calibration
            write_data = bytes([0x01])
            _LOGGER.debug("BLE TX [CALIBRATION]: %s", write_data.hex(" "))
            await self._client.write_gatt_char(CHAR_CALIBRATION_UUID, write_data)

            success = False
            anomalies: list[AnomalyType] = []

            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                # Protocol §3.4: 0x00 = success, anything else = failure.
                # On failure, the upper nibble carries the warning bits
                # (0x40=MOTION, 0x20=INCLINE, 0x10=TEMP) for PLUS, while a
                # bare 0x01 means generic "unknown scenario".
                if result == 0:
                    success = True
                elif result is not None:
                    upper = (result >> 4) & 0x0F
                    for anomaly in AnomalyType:
                        if upper & anomaly.value:
                            anomalies.append(anomaly)
            except asyncio.TimeoutError:
                _LOGGER.warning("Calibration timed out")

            await self._client.stop_notify(CHAR_CALIBRATION_UUID)

            return success, anomalies

        except BleakError as err:
            _LOGGER.warning("Failed to calibrate: %s", err)
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
            _LOGGER.debug("BLE TX [CONFIG]: %s", config_bytes.hex(" "))
            await self._client.write_gatt_char(CHAR_CONFIG_UUID, config_bytes)
            return True
        except BleakError as err:
            _LOGGER.warning("Failed to write config: %s", err)
            return False

    async def write_setup_date(self, dt: datetime) -> bool:
        """Write setup date to device."""
        if not self.is_connected or self._client is None:
            return False

        try:
            date_bytes = build_setup_date(dt)
            _LOGGER.debug("BLE TX [SETUP_DATE]: %s", date_bytes.hex(" "))
            await self._client.write_gatt_char(CHAR_SETUP_DATE_UUID, date_bytes)
            return True
        except BleakError as err:
            _LOGGER.warning("Failed to write setup date: %s", err)
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

        def notification_handler(_sender: int, data: bytearray) -> None:
            nonlocal result_level
            _LOGGER.debug("BLE RX [LEVEL]: %s", data.hex(" ") if data else "(empty)")
            if data and len(data) > 0:
                level = data[0]
                if level != 255:  # Not still-needs-calibration
                    result_level = level
                    event.set()

        try:
            _LOGGER.debug("BLE: Starting notifications on LEVEL characteristic (wait_for_valid_level)")
            await self._client.start_notify(CHAR_LEVEL_UUID, notification_handler)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("Timeout waiting for valid level notification")
                return False, None, []
            finally:
                try:
                    await self._client.stop_notify(CHAR_LEVEL_UUID)
                except BleakError:
                    pass

            if result_level is None:
                return False, None, []

            # Interpret the level value
            if 0 <= result_level <= 100:
                # Normal level
                return True, result_level, []
            elif 241 <= result_level <= 247:
                # Anomaly codes - extract flags
                anomaly_flags = result_level - 240
                anomalies: list[AnomalyType] = []
                for anomaly in AnomalyType:
                    if anomaly_flags & anomaly.value:
                        anomalies.append(anomaly)
                return False, None, anomalies
            elif 251 <= result_level <= 254:
                # Error codes
                _LOGGER.warning("Device reported error code: %d", result_level)
                return False, None, []
            else:
                # Unknown value
                _LOGGER.warning("Device reported unknown level: %d", result_level)
                return False, None, []

        except BleakError as err:
            _LOGGER.warning("Failed to wait for level: %s", err)
            return False, None, []
