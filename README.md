# Senso4s Home Assistant Integration

A Home Assistant integration for the Senso4s LP/Propane Gas Cylinder Level Sensor.

## Features

- **Passive BLE Monitoring**: Receives real-time updates from BLE advertisements without connecting to the device
- **Gas Level Tracking**: Monitor remaining gas percentage and calculated remaining weight
- **Battery Monitoring**: Track device battery level
- **Anomaly Detection**: Alerts for temperature, incline, and motion anomalies
- **Calibration Support**: Calibrate the scale directly from Home Assistant
- **Consumption History**: View historical gas usage data
- **Estimated Empty Date**: Calculate when the tank will be empty based on consumption rate
- **OEM App Compatible**: Works alongside the official Senso4s app—use either for calibration and configuration

## Supported Devices

- Senso4s Standard Model
- Senso4s Plus Model (supports Caravanning mode)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add `https://github.com/ksanislo/senso4s_ble` and select "Integration" as the category
6. Search for "Senso4s" and install

### Manual Installation

1. Copy the `custom_components/senso4s` folder from this repository to your Home Assistant `config/custom_components` directory
2. Restart Home Assistant

**Repository structure:**
```
custom_components/
└── senso4s/
    ├── __init__.py
    ├── binary_sensor.py
    ├── ble_client.py
    ├── config_flow.py
    ├── const.py
    ├── coordinator.py
    ├── diagnostics.py
    ├── manifest.json
    ├── models.py
    ├── parser.py
    ├── repairs.py
    ├── sensor.py
    ├── services.yaml
    ├── strings.json
    └── translations/
        └── en.json
```

## Configuration

### Automatic Discovery

The integration will automatically discover Senso4s devices broadcasting via Bluetooth. When a device is found:

1. Go to **Settings** → **Devices & Services**
2. Click on the discovered Senso4s device
3. Configure the cylinder parameters:
   - **Empty Cylinder Weight**: Weight of the empty cylinder (kg)
   - **Gas Capacity**: Maximum gas the cylinder holds (kg)
   - **Usage Mode**: BBQ, Camping, Caravanning, Heating, or Household
   - **Low Level Threshold**: Percentage that triggers the low gas warning

### Manual Setup

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "Senso4s"
4. Select your device from the list

## Entities

### Sensors

| Entity | Description |
|--------|-------------|
| Gas Level | Current gas level percentage (0-100%) |
| Gas Remaining | Calculated remaining gas in kg |
| Battery | Device battery level |
| Usage Mode | Current usage mode preset |
| Estimated Empty | Predicted date/time when tank will be empty |

### Binary Sensors

| Entity | Description |
|--------|-------------|
| Needs Calibration | True when device requires calibration |
| Error | True when device reports an error |
| Anomaly | True when any anomaly is detected |
| Low Gas | True when gas level is below threshold |
| Temperature Anomaly | True when temperature is out of range |
| Incline Anomaly | True when device is tilted |
| Motion Anomaly | True when motion is detected |

## Services

### `senso4s.calibrate`

Calibrate the scale sensor. **WARNING: The gas cylinder must be REMOVED from the scale before calibrating!**

| Field | Description |
|-------|-------------|
| entry_id | Config entry ID of the device |

### `senso4s.refresh_history`

Fetch consumption history from the device via BLE connection.

| Field | Description |
|-------|-------------|
| entry_id | Config entry ID of the device |

### `senso4s.write_config`

Write cylinder configuration to the device.

| Field | Description |
|-------|-------------|
| entry_id | Config entry ID |
| empty_weight_kg | Empty cylinder weight (optional) |
| gas_capacity_kg | Gas capacity (optional) |
| usage_mode | Usage mode 1-5 (optional) |

### `senso4s.set_setup_date`

Set the measurement start date on the device, resetting history.

| Field | Description |
|-------|-------------|
| entry_id | Config entry ID |
| datetime | Date/time to set (optional, defaults to now) |

## Troubleshooting

### Device Not Found

- Ensure Bluetooth is enabled on your Home Assistant host
- Check that the Senso4s device is powered on and in range
- The device broadcasts every few seconds; wait a moment and try again

### Needs Calibration

When the "Needs Calibration" sensor is on:
1. **Remove the gas cylinder from the scale** (scale must be empty!)
2. Press the "Calibrate" button or call the calibrate service
3. Wait for calibration to complete
4. Place the gas cylinder back on the scale

### Inaccurate Readings

- Verify the empty cylinder weight and gas capacity are correctly configured
- Ensure the device is on a level surface
- Check for motion or incline anomalies

### Connection Failures

- BLE connections can be unreliable; the integration will retry automatically
- Ensure no other device is connected to the sensor
- Move the Home Assistant Bluetooth adapter closer to the sensor

## Common Cylinder Sizes

| Size | Empty Weight (kg) | Gas Capacity (kg) |
|------|-------------------|-------------------|
| 5 kg | 5-7 | 5 |
| 11 kg | 10-12 | 11 |
| 15 kg | 14-16 | 15 |
| 33 kg | 28-32 | 33 |

## License

This integration is provided under the Apache License 2.0. See [LICENSE](LICENSE) for details.
