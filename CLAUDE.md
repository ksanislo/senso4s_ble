# Senso4s BLE Integration - Development Context

## Project Overview

Home Assistant custom integration for Senso4s LP/Propane gas cylinder level sensors. Communicates via Bluetooth Low Energy (BLE).

## Device Communication

### Passive Monitoring
- Device broadcasts BLE advertisements every few seconds
- Advertisement contains: gas level %, battery %, usage mode, anomaly flags
- No connection required for basic monitoring

### Active Connection (for these operations)
- Reading/writing cylinder configuration
- Calibration
- Reading consumption history
- Setting setup date

## Key Architecture

```
custom_components/senso4s/
├── __init__.py          # Entry point, BLE callback setup, services, history refresh
├── coordinator.py       # Data coordinator, stores state, calculates estimated empty
├── config_flow.py       # Setup and options flows (two-step for unit conversion)
├── parser.py            # BLE data parsing (advertisements, characteristics)
├── ble_client.py        # Active BLE connection operations
├── sensor.py            # Gas level, remaining, battery, usage mode, estimated empty
├── binary_sensor.py     # Calibration needed, errors, anomalies, low gas
├── repairs.py           # Calibration repair flow
└── const.py             # Constants, enums, unit conversion
```

## Important Conventions

### Calibration Warning
**CRITICAL**: Scale must be EMPTY (gas cylinder removed) before calibrating. This is emphasized in:
- Service descriptions
- Repair flow UI
- README troubleshooting

### Weight Units
- Internal storage: Always in kg
- Display: Converts to lb if user preference is set
- Config flow has two-step options to show converted values when switching units

### History Refresh
- Not polled on timer - triggered when BLE advertisement arrives AND history is stale
- Staleness threshold configurable (default 30 min, 0 to disable)
- Required for "Estimated Empty" calculation

## BLE Protocol Summary

### Manufacturer IDs
- 0x0059 (89) - Standard model
- 0x09CC (2508) - Plus model

### Advertisement Data (12 bytes)
- Byte 0: Flags (model type in upper nibble, usage mode in lower)
- Byte 1: Level (0-100 normal, 241-247 anomalies, 251-254 errors, 255 needs calibration)
- Byte 4: Battery %
- Bytes 6-11: MAC address

### Characteristics
- Level: `00007082-...` (notify)
- Config: `00007083-...` (read/write) - 5 bytes: empty weight, capacity, mode
- History: `00007085-...` (notify/write)
- Calibration: `00007086-...` (notify/write)
- Setup Date: `00007087-...` (read/write) - 7 bytes

## Testing

Test on real device by:
1. Installing to HA's custom_components/senso4s/
2. Restarting Home Assistant
3. Device should auto-discover via Bluetooth

Enable debug logging:
```yaml
logger:
  logs:
    custom_components.senso4s: debug
```

## Translations

- `strings.json` is the source of truth
- `translations/*.json` are language-specific copies
- Supported: en, de, es, fr, it, nl, pl, pt, ru, zh-Hans

## Repository

- GitHub: https://github.com/ksanislo/senso4s_ble
- Uses standard HACS structure: `custom_components/senso4s/`

## Git Workflow

**IMPORTANT**: Only commit changes locally without explicit user approval. Do NOT:
- Push to remote (`git push`)
- Create tags (`git tag`)
- Create GitHub releases (`gh release`)

These actions require user approval to allow for proper testing before release. When asked to "commit", only run `git add` and `git commit`. Wait for explicit instructions like "push", "release", or "bump version and release" before proceeding with those steps.

## Creating HACS Releases

HACS downloads directly from tagged releases (no zip file needed).

### Steps:
1. Update version in `manifest.json`
2. Commit changes
3. Create annotated tag: `git tag -a v1.x.x -m "Release notes..."`
4. Push: `git push origin main && git push origin v1.x.x`
5. Create GitHub release:
   ```bash
   gh release create v1.x.x --title "Short Description" --notes "Release notes..."
   ```
