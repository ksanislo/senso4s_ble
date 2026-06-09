# Senso4s BLE — Development Context

## Project

Home Assistant custom integration for Senso4s LP/Propane gas-cylinder level
sensors over BLE. HACS-distributed from this repo.

## Architecture (rc8 onwards)

Built on HA's `ActiveBluetoothProcessorCoordinator` framework. The integration
does **not** maintain its own bluetooth callback / dispatcher / availability
state — the framework owns that lifecycle.

```
custom_components/senso4s/
├── __init__.py        Entry setup, services, is_plus_model backfill
├── coordinator.py     Senso4sCoordinator (ActiveBluetoothProcessorCoordinator
│                      subclass): _update_method on every dispatched advert,
│                      _needs_poll + _async_poll_history for active GATT reads,
│                      proof-of-life timer reading the BLE cache every 30s
├── config_flow.py     Setup + options flows; writes CONF_IS_PLUS at entry
│                      creation so anomaly entity creation is timing-independent
├── parser.py          parse_manufacturer_data, parse_history_data, etc
├── ble_client.py      Active GATT operations (connect / read_* / write_* /
│                      calibrate / disconnect)
├── sensor.py          PassiveBluetoothProcessorEntity-based sensors
├── binary_sensor.py   PassiveBluetoothProcessorEntity-based binary sensors
├── repairs.py         Calibration repair flow
├── diagnostics.py     Diagnostic dump
├── const.py           Constants, enums, thresholds
└── models.py          Senso4sDeviceData, HistoryRecord dataclasses
```

## BLE — what to know about the device

### Per the official protocol (v1.0, 2025-09-08)

- Adverts broadcast every 500 ms, **identical bytes between data changes**.
- Real measurement happens once per 15-minute cycle. Bytes only change on a
  successful measurement that differs from the previous one.
- During an active connection no other peer (including ours) can read adverts
  from the device.
- The doc claims "big-endian" everywhere but every worked example is
  little-endian. Trust the examples — our parser uses `<H`/`<h`.

### Wire format (Adv: 12 bytes after company-ID 0x09CC; legacy 0x0059 still accepted)

| Byte | Meaning |
|---|---|
| 0 (D1) | Upper nibble = model/warning flags. `0b1000` → BASIC; `<0b1000` → PLUS, with bits `0b0100`=MOTION, `0b0010`=INCLINE, `0b0001`=TEMPERATURE. Lower nibble = usage mode (1–5). |
| 1 (D2) | Mass %. `0x00–0x64` valid; `0xFF` not-ready/zeroing; `0xFE` batteries empty; `0xFC` setup error; `0xF1–0xF7` cycle-startup anomaly (PLUS). |
| 2–3 (D3) | Not relevant (debug). |
| 4 (D4) | Battery %. Raw integer — do not round. |
| 5 (D5) | Not relevant. |
| 6–11 (D6) | MAC. |

### Characteristics (service `00007081-…`)

| UUID | Use |
|---|---|
| `00007082-…` | Mass byte (same as D2). NOTIFY+READ. |
| `00007083-…` | Cylinder config: 5 bytes `<H` empty_dag, `<H` capacity_dag, byte usage_mode. |
| `00007085-…` | History stream: trigger by WRITE `0x00 0x00`; receives 4-byte records `<H mass_dag, <H duration_in_15min_cycles`. |
| `00007086-…` | Zeroing ("calibration"): WRITE `0x01` to start. Result-byte upper nibble carries warning flags (`0x40`/`0x20`/`0x10`); `0x00` = success. |
| `00007087-…` | Setup date: 7 bytes `<H year`, `month`, `day`, `hour`, `minute`, `0` (constant). |

## How the integration actually behaves

### Lifecycle

- `async_setup_entry` seeds `coordinator.data` from `bluetooth.async_last_service_info`,
  backfills `CONF_IS_PLUS` if missing (one-shot, for entries created before rc8),
  starts the framework coordinator (`coordinator.async_start()`), and starts
  the 30s proof-of-life logger.
- Platforms (`sensor.py`, `binary_sensor.py`) register a
  `PassiveBluetoothDataProcessor` whose `update_method` converts our
  `Senso4sDeviceData` to a `PassiveBluetoothDataUpdate`.
- `binary_sensor.py` reads `entry.data[CONF_IS_PLUS]` to decide whether to
  register the four anomaly binary sensors. **Never** make this depend on
  coordinator data alone — at platform-setup time the cache may be cold.

### Availability — explicitly NOT delegated to the framework

The framework's tracker would mark the device unavailable ~60s after the last
advert (auto-tuned). Far too aggressive for a 15-min-cycle device.

- `AVAILABILITY_TIMEOUT_MINUTES = 60` in const.py.
- Both sensor and binary_sensor entities override `available` to read
  `bluetooth.async_last_service_info(...).time` directly from the BLE cache
  (which updates on every advert, *before* habluetooth's dedupe).
- Each entity registers a 1-minute `async_track_time_interval` to force
  `async_write_ha_state()` so the `available` getter is consulted on a known
  cadence even when no advert dispatches.
- RSSI sensor uses a **5-minute** tick instead of 1 minute — it's the only
  entity whose value actually changes on every refresh.

### Logging — three explicit channels, no hidden state

- `BLE RX [SCANNER]` — every 30s from the coordinator's proof-of-life timer.
  Reads the BLE cache directly so it appears regardless of dedupe.
- `BLE RX [DISPATCH]` — every advert that habluetooth dispatches past dedupe.
- `BLE RX [PARSED]` — parsed values after successful manufacturer-data parse.

### habluetooth dedupe — the core gotcha

`habluetooth/manager.py` short-circuits dispatch when the new advertisement's
`manufacturer_data` / `service_data` / `service_uuids` / `name` are byte-
identical to the previous one (RSSI is ignored in this comparison). Any
integration code that depends on "the callback fired" will silently break
on a device whose data sits steady. **Never** trust the framework's `available`
or callback frequency as a proxy for "is the radio alive" — use the cache
timestamp.

### Entity identity continuity (pre-rc8 → rc8+)

- Override `_attr_unique_id` to `{address}_{key}` after `super().__init__()` —
  the framework's default is `{address}-{key}` and would orphan existing
  entity_registry rows.
- Union `(DOMAIN, address)` into `_attr_device_info["identifiers"]` so the
  pre-rc8 device-registry row is still matched (the framework alone would
  register only `("bluetooth", address)`).

### Estimated Empty

Least-squares linear regression over the last 10 history records, anchored at
the most recent record's timestamp. See [[estimated-empty-duration-weighting]]
in memory for the planned duration-weighting variant we may try if the value
keeps drifting from the vendor app.

## Conventions

### Calibration / Zeroing terminology

- Protocol calls it "Scale zeroing." User-facing strings keep "Calibration"
  per Ken's preference — do not rename without explicit go-ahead.
- **Scale must be EMPTY (cylinder removed) before zeroing.** Emphasized in
  service description, repair flow, and README.

### Weight units

- Internal: always kg.
- Display: lb if user picked it in options.
- Config flow has a two-step options flow that converts visible values when
  the unit switches.

### Translations

- `strings.json` is the source. `translations/*.json` are locale overrides.
- Supported locales: en, de, es, fr, it, nl, pl, pt, ru, zh-Hans.
- When adding entity descriptions, add translation keys to ALL of them.

### Comments / docstrings

- Default to none. Only add a comment when the **why** is non-obvious — a
  workaround for a specific upstream behavior, a non-obvious invariant, or
  a "this can't be reordered because…" note.
- Do not narrate WHAT the code does — names should already tell that story.
- One-liners over paragraphs. No banner-style section headers.

## Git workflow

**Only commit locally without explicit approval.** Do NOT:

- `git push`
- `git tag` (annotated or otherwise)
- `gh release create`

When the user says "commit," that means `git add` + `git commit`. Wait for
"push", "tag", "release", "ship" before going further. Push commands and
release commands are always explicitly requested.

When releasing, ALWAYS draft the release notes first and ask for
verification before running `gh release create`. The user explicitly
asked for this after the v1.4.0-beta.1 round.

### Acknowledgements convention

Every 1.4.0-line release notes ends with thanks to **Miha Finžgar and
Senso4s** for providing test hardware. Keep this in future release notes
unless the user says to drop it.

### Source attribution

Some details of the integration's protocol handling and algorithms come
from materials we do not have rights to redistribute. Do not describe
how any specific behavior was derived in code, comments, commit
messages, or release notes. Treat externally-sourced reference material
as ignored working files only.

## HACS release process (when explicitly requested)

Pre-releases use the `-rcN` (or `-beta.N`) suffix. The GitHub release must be
created with `--prerelease`. Users opt in via the per-integration "Show beta
versions" toggle in HACS.

1. Bump `manifest.json` version.
2. `git commit` for the bump.
3. `git push origin main`.
4. `git tag -a vX.Y.Z[-rcN] -m "vX.Y.Z[-rcN]"`.
5. `git push origin vX.Y.Z[-rcN]`.
6. Draft notes, get user approval, then
   `gh release create vX.Y.Z[-rcN] [--prerelease] --title "…" --notes "…"`.
7. If superseding an earlier RC, `gh release delete vX.Y.Z-rcN-1 --yes --cleanup-tag`
   then `git fetch origin --prune --prune-tags`.

## Repository

- GitHub: https://github.com/ksanislo/senso4s_ble
- HACS layout: `custom_components/senso4s/`
- Stable channel currently tracks `v1.3.0`. The 1.4.0 line is in pre-release.
