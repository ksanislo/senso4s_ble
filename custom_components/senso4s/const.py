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

"""Constants for the Senso4s integration."""
from enum import IntEnum
from typing import Final

DOMAIN: Final = "senso4s"

# BLE UUIDs
SERVICE_UUID: Final = "00007081-a20b-4d4d-a4de-7f071dbbc1d8"
SCAN_FILTER_UUID: Final = "00007081-0000-1000-8000-00805f9b34fb"
CHAR_LEVEL_UUID: Final = "00007082-a20b-4d4d-a4de-7f071dbbc1d8"
CHAR_CONFIG_UUID: Final = "00007083-a20b-4d4d-a4de-7f071dbbc1d8"
CHAR_HISTORY_UUID: Final = "00007085-a20b-4d4d-a4de-7f071dbbc1d8"
CHAR_CALIBRATION_UUID: Final = "00007086-a20b-4d4d-a4de-7f071dbbc1d8"
CHAR_SETUP_DATE_UUID: Final = "00007087-a20b-4d4d-a4de-7f071dbbc1d8"

# Device identification
DEVICE_NAME: Final = "SENSO4S"
MANUFACTURER_IDS: Final = frozenset({0x0059, 0x09CC})
MANUFACTURER: Final = "Senso4s"

# Configuration keys
CONF_EMPTY_WEIGHT: Final = "empty_weight_kg"
CONF_GAS_CAPACITY: Final = "gas_capacity_kg"
CONF_USAGE_MODE: Final = "usage_mode"
CONF_LOW_LEVEL_THRESHOLD: Final = "low_level_threshold"
CONF_WEIGHT_UNIT: Final = "weight_unit"
CONF_HISTORY_POLL_INTERVAL: Final = "history_poll_interval"
CONF_LAST_SETUP_DATE: Final = "last_setup_date"  # ISO format string for persistence

# Weight units
UNIT_KG: Final = "kg"
UNIT_LB: Final = "lb"

# Conversion factor
KG_TO_LB: Final = 2.20462
LB_TO_KG: Final = 0.453592

# Default values
DEFAULT_EMPTY_WEIGHT: Final = 10.0
DEFAULT_GAS_CAPACITY: Final = 11.0
DEFAULT_USAGE_MODE: Final = 5  # Household
DEFAULT_LOW_LEVEL_THRESHOLD: Final = 20
DEFAULT_WEIGHT_UNIT: Final = UNIT_KG
DEFAULT_HISTORY_POLL_INTERVAL: Final = 30  # minutes, 0 = disabled


def kg_to_lb(kg: float) -> float:
    """Convert kilograms to pounds."""
    return kg * KG_TO_LB


def lb_to_kg(lb: float) -> float:
    """Convert pounds to kilograms."""
    return lb * LB_TO_KG

# Timing
CYCLE_DURATION_MINUTES: Final = 15
UPDATE_INTERVAL_SECONDS: Final = 60
CONNECTION_TIMEOUT: Final = 30.0
NOTIFICATION_TIMEOUT: Final = 5.0


class UsageMode(IntEnum):
    """Usage mode for the gas cylinder."""

    BBQ = 1
    CAMPING = 2
    CARAVANNING = 3
    HEATING = 4
    HOUSEHOLD = 5

    @classmethod
    def from_value(cls, value: int) -> "UsageMode":
        """Get usage mode from value with fallback."""
        try:
            return cls(value)
        except ValueError:
            return cls.HOUSEHOLD


USAGE_MODE_NAMES: Final = {
    UsageMode.BBQ: "BBQ",
    UsageMode.CAMPING: "Camping",
    UsageMode.CARAVANNING: "Caravanning",
    UsageMode.HEATING: "Heating",
    UsageMode.HOUSEHOLD: "Household",
}


class AnomalyType(IntEnum):
    """Anomaly types detected by the sensor."""

    TEMPERATURE = 1
    INCLINE = 2
    MOTION = 4


ANOMALY_NAMES: Final = {
    AnomalyType.TEMPERATURE: "Temperature",
    AnomalyType.INCLINE: "Incline",
    AnomalyType.MOTION: "Motion",
}


class DeviceError(IntEnum):
    """Device error codes."""

    MEASUREMENT = 251
    SCALE = 252
    SENSOR = 253
    BATTERY = 254
    NEEDS_CALIBRATION = 255


ERROR_DESCRIPTIONS: Final = {
    DeviceError.MEASUREMENT: "Measurement error - check cylinder placement",
    DeviceError.SCALE: "Scale error - device malfunction",
    DeviceError.SENSOR: "Sensor error - device malfunction",
    DeviceError.BATTERY: "Battery critically low",
    DeviceError.NEEDS_CALIBRATION: "Calibration required",
}

# Repair issue IDs
ISSUE_NEEDS_CALIBRATION: Final = "needs_calibration"

# Calibration timeouts
CALIBRATION_LEVEL_WAIT_TIMEOUT: Final = 5.0  # seconds
