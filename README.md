# Senso4s Home Assistant Integration

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/ksanislo/senso4s_ble)](https://github.com/ksanislo/senso4s_ble/releases)
[![License](https://img.shields.io/github/license/ksanislo/senso4s_ble)](LICENSE)

A Home Assistant integration for the Senso4s LP/Propane Gas Cylinder Level Sensor.

## How it works

Live data (gas level, battery, usage mode, anomaly flags) is parsed from BLE
advertisements the device broadcasts continuously. When **automatic history
polling** is enabled (the default), the integration also makes brief active
GATT connections to read consumption history for a high-resolution
**Estimated Empty** prediction. Active connections are also used for
cylinder configuration, setting the measurement start date, and zeroing
the scale ("calibration"). The device only allows one connected client at
a time — the OEM app and Home Assistant can both work with the same device,
just not at the exact same moment.

With history polling disabled (passive-only mode), the integration never
initiates active connections on its own. It still provides an **Estimated
Empty** prediction by tracking percentage changes from advertisements — a
coarser estimate, but functional for setups with passive-only Bluetooth
hardware such as ESPHome BLE proxies without active connection support.

Configurable from the integration options:

- **Enable automatic history polling** (default on; disable for passive-only
  Bluetooth hardware)
- **History refresh interval** (minutes; default 240 — gas level changes
  also trigger an immediate refresh)
- **Low-gas warning threshold** (percentage; default 10)
- **Weight unit** (kg or lb)

## Features

- Real-time gas level and remaining-weight tracking
- Battery monitoring
- Anomaly detection on Plus models (temperature, incline, motion)
- Calibration / zeroing from Home Assistant
- Consumption history with predicted empty date (active or passive mode)
- Compatible with the official Senso4s mobile app — either can configure or
  zero the device

## Supported Devices

- Senso4s Basic
- Senso4s Plus (adds Caravanning mode and the three anomaly sensors)

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Search for **Senso4s** and install
3. Restart Home Assistant

### Manual

Copy `custom_components/senso4s/` from this repository into your Home Assistant
`config/custom_components/` directory, then restart Home Assistant.

## Configuration

### Automatic discovery

The device shows up in **Settings → Devices & Services → Discovered**
when Home Assistant sees its advertisements. Click **Configure**, confirm
you want to add it, and follow the prompts.

If the device was already configured (for example via the OEM app), the
integration imports the existing settings. If it has never been configured,
the integration walks you through a guided setup that includes zeroing the
empty scale and verifying a valid reading.

### Manual add

**Settings → Devices & Services → + Add Integration → Senso4s** opens a
picker showing every Senso4s device currently in range, with each one's
RSSI displayed so you can tell adjacent units apart. Picking a device adds
it directly — no extra confirmation step.

### Initial setup for a new device

1. Enter your cylinder configuration:
   - **Weight Unit**: kg or lb
   - **Empty Cylinder Weight**: weight of the cylinder without gas
   - **Gas Capacity**: maximum gas it can hold
   - **Usage Mode**: BBQ, Camping, Caravanning (Plus only), Heating, or Household
2. When prompted, **remove the cylinder from the scale** — it must be empty
   for zeroing to work
3. Wait for zeroing to complete
4. When prompted, **place the cylinder back on the scale**
5. The integration verifies a valid reading before finishing setup

## Device options

**Settings → Devices & Services → Senso4s → Configure** has three modes.

### Refill Tank

Use when you have refilled or replaced the cylinder with the same-size tank.
Resets the measurement start date used by consumption history and Estimated
Empty.

- Place the **full tank on the scale** before submitting
- Optionally adjust display settings (weight unit, low-gas threshold, history
  refresh interval)
- Cylinder configuration (empty weight, gas capacity, usage mode) is
  unchanged

### Recalibrate Scale

Use when switching to a different tank size or when readings are inaccurate.
Performs a full zeroing and lets you update the cylinder configuration.

- **Remove the cylinder from the scale** before submitting
- Enter the new cylinder configuration
- Replace the cylinder when prompted
- The integration writes the new configuration, resets the measurement start
  date, and verifies a valid reading

### Settings Only

Change display settings without touching the device:

- Weight Unit
- Low Level Threshold
- Enable Automatic History Polling
- History Refresh Interval

## Entities

### Sensors

| Entity | Description |
|---|---|
| Gas Level | Current gas level (0–100%) |
| Gas Remaining | Calculated remaining mass in kg or lb |
| Battery | Device battery level |
| Usage Mode | Current usage mode preset |
| Estimated Empty | Predicted date/time the tank will run dry |
| Last Setup | When the current measurement cycle was started |
| Signal Strength | RSSI of the most recent advertisement (diagnostic) |
| Empty Cylinder Weight | Configured empty weight (diagnostic) |
| Gas Capacity | Configured gas capacity (diagnostic) |

### Binary sensors

| Entity | Description |
|---|---|
| Needs Calibration | True when the device reports it needs zeroing |
| Error | True when the device reports an error |
| Low Gas | True when gas level is at or below the threshold |
| Anomaly (Plus only) | True when any anomaly is active |
| Temperature Anomaly (Plus only) | Temperature outside operating range |
| Incline Anomaly (Plus only) | Tilt > 6° |
| Motion Anomaly (Plus only) | Motion detected during measurement |

The four Plus-only anomaly binary sensors are created for Plus devices but
disabled by default — enable them in the entity registry if you want them.

## Services

### `senso4s.calibrate`

Zero the scale. **The cylinder must be removed from the scale before
calling this.**

| Field | Description |
|---|---|
| device_id | The Senso4s device to zero |

### `senso4s.refresh_history`

Fetch consumption history from the device via an active connection.

| Field | Description |
|---|---|
| device_id | The Senso4s device to refresh from |

### `senso4s.write_config`

Write cylinder configuration to the device.

| Field | Description |
|---|---|
| device_id | The device to write to |
| empty_weight_kg | Empty cylinder weight (optional) |
| gas_capacity_kg | Gas capacity (optional) |
| usage_mode | Usage mode 1–5 (optional) |

### `senso4s.set_setup_date`

Set the measurement start date on the device, resetting history.

| Field | Description |
|---|---|
| device_id | The device to set the date on |
| datetime | Date/time to set (optional; defaults to now) |

## Troubleshooting

### Device not found

- Make sure Bluetooth is enabled on the Home Assistant host or a Bluetooth
  proxy is online
- Confirm the device is powered on and within range
- The device advertises continuously every 500 ms; if Home Assistant's
  Bluetooth panel sees it but the integration doesn't, try a full HA restart

### Needs calibration

When the **Needs Calibration** binary sensor turns on, Home Assistant
creates a **Calibration Required** repair issue. To resolve:

1. **Settings → System → Repairs**, open the issue
2. Remove the cylinder from the scale when prompted
3. Follow the guided zeroing flow; the integration replaces the cylinder
   for you and verifies a valid reading

You can also rezero at any time via the **Recalibrate Scale** option, or
by calling the `senso4s.calibrate` service (make sure the cylinder is
removed first).

### Inaccurate readings

- Confirm the empty cylinder weight and gas capacity match the actual tank
- Make sure the device is on a level surface
- Check for active motion or incline anomalies on a Plus model

### Connection failures

- BLE connections can be unreliable; the integration retries on the next
  cycle
- Confirm no other device (mobile app, second proxy) is connected to the
  sensor
- If your setup uses an ESPHome BLE proxy, ensure `bluetooth_proxy: active:
  true` is set so active connections can pass through
- If your Bluetooth hardware only supports passive scanning, disable
  **Enable automatic history polling** in Settings — the integration will
  use passive percentage tracking for Estimated Empty instead

## Common cylinder sizes

| Size | Empty weight (kg) | Gas capacity (kg) |
|---|---|---|
| 5 kg | 5–7 | 5 |
| 11 kg | 10–12 | 11 |
| 15 kg | 14–16 | 15 |
| 33 kg | 28–32 | 33 |

## License

Apache License 2.0 — see [LICENSE](LICENSE).
