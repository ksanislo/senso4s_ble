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
3. Search for "Senso4s" and install
4. Restart Home Assistant

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
3. Follow the prompts to configure your cylinder

If the device has already been configured (e.g., via the OEM app), the integration will import the existing settings automatically. For a new unconfigured device, you will be guided through initial setup and calibration (see below).

### Initial Setup (New Device)

When adding an unconfigured device for the first time:

1. Enter your cylinder configuration:
   - **Weight Unit**: kg or lb (weights will display in your chosen unit)
   - **Empty Cylinder Weight**: Weight of the empty cylinder
   - **Gas Capacity**: Maximum gas the cylinder holds
   - **Usage Mode**: BBQ, Camping, Caravanning, Heating, or Household
2. **Remove the gas cylinder** from the scale when prompted — the scale must be completely empty
3. Wait for calibration to complete automatically
4. **Place the cylinder back** on the scale when prompted
5. The integration verifies a valid reading before finishing setup

### Manual Setup

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "Senso4s"
4. Select your device from the list

## Device Options

Access the options menu from **Settings** → **Devices & Services** → **Senso4s** → **Configure**. The menu offers three choices:

### Refill Tank

Use this when you have **refilled or replaced your gas cylinder with the same size tank**. This resets the measurement start date, which is needed for accurate consumption history and estimated empty calculations.

1. **Place the full tank on the scale** before submitting
2. Optionally adjust display settings:
   - **Weight Unit**: kg or lb
   - **Low Level Threshold**: Percentage (5–50%) that triggers the low gas warning
   - **History Poll Interval**: How often to fetch consumption history (0 to disable, up to 1440 minutes)
3. The integration writes the new start date to the device and verifies a valid level reading

No calibration is performed — cylinder configuration (empty weight, gas capacity, usage mode) stays unchanged.

### Recalibrate Scale

Use this when **switching to a different tank size** or if **readings are inaccurate**. This performs a full calibration and lets you update the cylinder configuration.

1. **Remove the gas cylinder** from the scale before submitting — the scale must be completely empty
2. Enter your cylinder configuration (all fields can be changed):
   - **Weight Unit**: kg or lb
   - **Empty Cylinder Weight**: Weight of the empty cylinder
   - **Gas Capacity**: Maximum gas the cylinder holds
   - **Usage Mode**: BBQ, Camping, Caravanning, Heating, or Household
   - **Low Level Threshold**: Percentage (5–50%) that triggers the low gas warning
   - **History Poll Interval**: How often to fetch consumption history (0 to disable, up to 1440 minutes)
3. Wait for calibration to complete automatically
4. **Place the cylinder back** on the scale when prompted
5. The integration writes the new configuration, resets the measurement start date, and verifies a valid level reading

### Settings Only

Use this to change display settings without performing calibration or writing to the device:

- **Weight Unit**: kg or lb
- **Low Level Threshold**: Percentage (5–50%) that triggers the low gas warning
- **History Poll Interval**: How often to fetch consumption history (0 to disable, up to 1440 minutes)

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
| device_id | The Senso4s device to calibrate |

### `senso4s.refresh_history`

Fetch consumption history from the device via BLE connection.

| Field | Description |
|-------|-------------|
| device_id | The Senso4s device to refresh history from |

### `senso4s.write_config`

Write cylinder configuration to the device.

| Field | Description |
|-------|-------------|
| device_id | The Senso4s device to write configuration to |
| empty_weight_kg | Empty cylinder weight (optional) |
| gas_capacity_kg | Gas capacity (optional) |
| usage_mode | Usage mode 1-5 (optional) |

### `senso4s.set_setup_date`

Set the measurement start date on the device, resetting history.

| Field | Description |
|-------|-------------|
| device_id | The Senso4s device to set the date on |
| datetime | Date/time to set (optional, defaults to now) |

## Troubleshooting

### Device Not Found

- Ensure Bluetooth is enabled on your Home Assistant host
- Check that the Senso4s device is powered on and in range
- The device broadcasts every few seconds; wait a moment and try again

### Needs Calibration

When the "Needs Calibration" sensor turns on, Home Assistant will automatically create a **repair issue** that walks you through the calibration process:

1. Go to **Settings** → **System** → **Repairs** and open the "Calibration Required" issue
2. **Remove the gas cylinder from the scale** — the scale must be completely empty
3. Follow the guided steps: the integration connects, calibrates, then asks you to replace the cylinder
4. The integration verifies a valid reading before closing the repair

You can also calibrate manually at any time:
- **Options menu**: Go to the device's **Configure** page and choose **Recalibrate Scale** (see [Device Options](#device-options) above)
- **Service call**: Call `senso4s.calibrate` (useful for automations — ensure the cylinder is removed first)

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
