"""BLE advertisement and characteristic data parser for Senso4s devices."""
from __future__ import annotations

import logging
import struct
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.util import dt as dt_util

from .const import (
    CYCLE_DURATION_MINUTES,
    MANUFACTURER_IDS,
    AnomalyType,
    UsageMode,
)
from .models import CylinderConfig, HistoryRecord, Senso4sDeviceData

_LOGGER = logging.getLogger(__name__)


def parse_manufacturer_data(
    mfr_id: int, data: bytes, device_name: str = ""
) -> Optional[Senso4sDeviceData]:
    """
    Parse BLE manufacturer advertisement data.

    Args:
        mfr_id: Manufacturer ID from advertisement
        data: Manufacturer data bytes (without the ID)
        device_name: Device name from advertisement

    Returns:
        Parsed device data or None if invalid
    """
    if mfr_id not in MANUFACTURER_IDS:
        _LOGGER.debug(
            "Ignoring manufacturer ID 0x%04X (not in known IDs: %s)",
            mfr_id,
            [f"0x{m:04X}" for m in MANUFACTURER_IDS],
        )
        return None

    if len(data) < 11:
        _LOGGER.debug(
            "BLE PARSE: Manufacturer data too short: %d bytes (need at least 11)",
            len(data),
        )
        return None

    flags_byte = data[0]
    level_byte = data[1]
    battery_raw = data[4]
    mac_bytes = data[6:12] if len(data) >= 12 else bytes(6)

    _LOGGER.debug(
        "BLE PARSE [BYTES] flags=0x%02X (model=%s, mode_raw=%d), "
        "level=0x%02X (%d), battery_raw=%d, mac=%s",
        flags_byte,
        "BASIC" if (flags_byte >> 4) == 0x8 else "PLUS",
        flags_byte & 0x0F,
        level_byte,
        level_byte,
        battery_raw,
        mac_bytes.hex(":"),
    )

    # Parse model type (bit 7 of flags)
    is_plus = (flags_byte >> 4) != 0x8

    # Parse usage mode (lower nibble)
    usage_value = flags_byte & 0x0F
    usage_mode = UsageMode.from_value(usage_value)

    # Caravanning only valid for Plus models
    if not is_plus and usage_mode == UsageMode.CARAVANNING:
        usage_mode = UsageMode.HOUSEHOLD

    # Parse battery (round up to nearest 5)
    battery = battery_raw
    if battery % 5 != 0:
        battery = (battery // 5) * 5 + 5
    battery = min(100, max(0, battery))

    # Parse MAC address
    mac_address = ":".join(f"{b:02X}" for b in mac_bytes)

    # Determine status
    gas_level = level_byte
    needs_calibration = False
    has_error = False
    error_code: Optional[int] = None
    anomalies: list[AnomalyType] = []

    if level_byte == 255:
        needs_calibration = True
        gas_level = -1
    elif 251 <= level_byte <= 254:
        has_error = True
        error_code = level_byte
        gas_level = -1
    elif 241 <= level_byte <= 247:
        # Anomaly codes - extract anomaly flags
        anomaly_flags = level_byte - 240
        for anomaly in AnomalyType:
            if anomaly_flags & anomaly.value:
                anomalies.append(anomaly)
        gas_level = -1
    elif level_byte > 100:
        gas_level = -1

    return Senso4sDeviceData(
        mac_address=mac_address,
        name=device_name,
        gas_level_percent=gas_level,
        battery_percent=battery,
        usage_mode=usage_mode,
        is_plus_model=is_plus,
        needs_calibration=needs_calibration,
        has_error=has_error,
        error_code=error_code,
        anomalies=anomalies,
        last_seen=dt_util.now(),
    )


def parse_cylinder_config(data: bytes) -> Optional[CylinderConfig]:
    """
    Parse cylinder configuration characteristic value.

    Args:
        data: 5 bytes from the cylinder config characteristic

    Returns:
        Parsed configuration or None if invalid
    """
    if len(data) != 5:
        return None

    empty_weight_dag = struct.unpack("<h", data[0:2])[0]
    gas_capacity_dag = struct.unpack("<h", data[2:4])[0]
    usage_value = data[4]

    usage_mode = UsageMode.from_value(usage_value)

    return CylinderConfig(
        empty_weight_kg=empty_weight_dag / 100.0,
        gas_capacity_kg=gas_capacity_dag / 100.0,
        usage_mode=usage_mode,
    )


def parse_setup_date(data: bytes) -> Optional[datetime]:
    """
    Parse setup date characteristic value.

    Args:
        data: 7 bytes from setup date characteristic

    Returns:
        Datetime or None if invalid/unset
    """
    if len(data) != 7:
        return None

    # All zeros means not set
    if all(b == 0 for b in data):
        return None

    year = struct.unpack("<H", data[0:2])[0]
    month = data[2]
    day = data[3]
    hour = data[4]
    minute = data[5]
    second = data[6]

    try:
        # Device stores local time, not UTC
        return datetime(year, month, day, hour, minute, second, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    except ValueError:
        return None


def parse_history_data(
    data: bytes, setup_date: datetime
) -> list[HistoryRecord]:
    """
    Parse consumption history data.

    Args:
        data: Raw history bytes (multiple of 4)
        setup_date: When measurement was started

    Returns:
        List of history records with timestamps
    """
    if len(data) % 4 != 0:
        return []

    records: list[HistoryRecord] = []
    cumulative_cycles = 0
    cycle_duration = timedelta(minutes=CYCLE_DURATION_MINUTES)

    for i in range(0, len(data), 4):
        chunk = data[i : i + 4]
        mass_dag = struct.unpack("<h", chunk[0:2])[0]
        cycles = struct.unpack("<h", chunk[2:4])[0]

        # First record with non-zero cycle - add initial point at zero
        if i == 0 and cycles != 0:
            records.append(
                HistoryRecord(
                    remaining_gas_kg=mass_dag / 100.0,
                    timestamp=setup_date,
                )
            )

        cumulative_cycles += cycles
        timestamp = setup_date + (cumulative_cycles * cycle_duration)

        records.append(
            HistoryRecord(
                remaining_gas_kg=mass_dag / 100.0,
                timestamp=timestamp,
            )
        )

    return records


def build_cylinder_config(
    empty_weight_kg: float,
    gas_capacity_kg: float,
    usage_mode: UsageMode,
) -> bytes:
    """
    Build cylinder configuration bytes for writing.

    Args:
        empty_weight_kg: Empty cylinder weight in kilograms
        gas_capacity_kg: Gas capacity in kilograms
        usage_mode: Usage mode enum

    Returns:
        5 bytes for writing to characteristic
    """
    empty_dag = int(empty_weight_kg * 100)
    capacity_dag = int(gas_capacity_kg * 100)

    data = bytearray(5)
    struct.pack_into("<h", data, 0, empty_dag)
    struct.pack_into("<h", data, 2, capacity_dag)
    data[4] = usage_mode.value

    return bytes(data)


def build_setup_date(dt: datetime) -> bytes:
    """
    Build setup date bytes for writing.

    Args:
        dt: Datetime to set

    Returns:
        7 bytes for writing to characteristic
    """
    # Convert to local time since device stores local time
    local_dt = dt_util.as_local(dt)

    data = bytearray(7)
    struct.pack_into("<H", data, 0, local_dt.year)
    data[2] = local_dt.month
    data[3] = local_dt.day
    data[4] = local_dt.hour
    data[5] = local_dt.minute
    data[6] = 0  # Seconds always 0

    return bytes(data)


def decode_anomaly_level(level: int) -> list[AnomalyType]:
    """
    Decode anomaly flags from special level values.

    Level values 241-247 encode anomalies:
    - The lower nibble of (level - 240) contains flags

    Args:
        level: Level value from device

    Returns:
        List of detected anomalies
    """
    if not (241 <= level <= 247):
        return []

    flags = level - 240
    anomalies: list[AnomalyType] = []
    for anomaly in AnomalyType:
        if flags & anomaly.value:
            anomalies.append(anomaly)
    return anomalies
