"""Data models for the Senso4s integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .const import AnomalyType, UsageMode


@dataclass
class Senso4sDeviceData:
    """Data parsed from BLE advertisements or active connection."""

    # Identification
    mac_address: str = ""
    name: str = ""

    # Current readings
    gas_level_percent: int = -1  # 0-100, -1 if unavailable
    battery_percent: int = 0
    usage_mode: UsageMode = UsageMode.HOUSEHOLD

    # Device info
    is_plus_model: bool = False

    # Status
    needs_calibration: bool = False
    has_error: bool = False
    error_code: Optional[int] = None
    anomalies: list[AnomalyType] = field(default_factory=list)

    # Configuration (from active connection)
    empty_weight_kg: Optional[float] = None
    gas_capacity_kg: Optional[float] = None
    setup_date: Optional[datetime] = None

    # Computed values
    last_seen: Optional[datetime] = None

    @property
    def gas_remaining_kg(self) -> Optional[float]:
        """Calculate remaining gas in kg."""
        if (
            self.gas_level_percent >= 0
            and self.gas_level_percent <= 100
            and self.gas_capacity_kg is not None
            and self.gas_capacity_kg > 0
        ):
            return (self.gas_level_percent / 100.0) * self.gas_capacity_kg
        return None

    @property
    def is_available(self) -> bool:
        """Check if valid data is available."""
        return self.gas_level_percent >= 0 and self.gas_level_percent <= 100

    @property
    def has_anomaly(self) -> bool:
        """Check if any anomaly is present."""
        return len(self.anomalies) > 0

    @property
    def error_description(self) -> Optional[str]:
        """Get human-readable error description."""
        from .const import ERROR_DESCRIPTIONS, DeviceError

        if self.error_code is not None:
            try:
                return ERROR_DESCRIPTIONS.get(DeviceError(self.error_code))
            except ValueError:
                return f"Unknown error: {self.error_code}"
        return None

    @property
    def anomaly_names(self) -> list[str]:
        """Get list of anomaly names."""
        from .const import ANOMALY_NAMES

        return [ANOMALY_NAMES.get(a, str(a)) for a in self.anomalies]


@dataclass
class HistoryRecord:
    """Single consumption history data point."""

    remaining_gas_kg: float
    timestamp: datetime


@dataclass
class CylinderConfig:
    """Cylinder configuration from device."""

    empty_weight_kg: float
    gas_capacity_kg: float
    usage_mode: UsageMode
