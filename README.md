<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/difluid_scale/brand/dark_logo@2x.png">
    <img alt="DiFluid Microbalance" src="custom_components/difluid_scale/brand/logo@2x.png" width="412">
  </picture>
</p>

# DiFluid Microbalance for Home Assistant

A Home Assistant custom integration for the **DiFluid Microbalance** coffee scale over Bluetooth Low Energy.
It shows weight, flow rate, brew timer and battery live, and lets you tare and control the timer remotely.

Tested with model `DFT-S101`, firmware `V320` and `V326`.

> Not affiliated with or endorsed by DiFluid.

## Features

| Entity | Description |
|---|---|
| Weight | Grams. Readings in ounces are converted to grams. |
| Flow rate | g/s |
| Timer | Brew timer (counts in seconds as observed; unit not documented) |
| Battery | Percent. Attributes: `charging`, `device_state` |
| Device state | Scale state (idle, timing, paused, ...) |
| Connected | Binary sensor for the BLE connection; always available |
| Key press | Event entity for buttons pressed **on the scale**: `power_click`, `timer_click`, `power_double`, `timer_double`, `power_long`, `timer_long`, `timer_press` |
| Tare | Button |
| Start / Stop / Reset timer | Buttons |
| Auto timer start / Auto timer stop | Switches for the scale's "auto detect / auto stop timing" settings |
| Weight unit | Select: gram / ounce / gering (third unit as named in the DiFluid protocol; no gram conversion documented, weight shows no value in this mode) |

## Installation

### HACS (recommended)

1. In HACS, search for **DiFluid Microbalance**.
2. Download it and restart Home Assistant.

### HACS custom repository (fallback)

If the integration does not show up in the search yet:

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/radecke/ha-difluid-microbalance` with type **Integration**.
3. Download **DiFluid Microbalance** and restart Home Assistant.

### Manual

Copy `custom_components/difluid_scale` into your `config/custom_components/` folder and restart Home Assistant.

## Setup

Requires a working [Bluetooth](https://www.home-assistant.io/integrations/bluetooth/) setup with an adapter or proxy that supports **active connections**.

Turn the scale on. Home Assistant discovers it automatically; confirm the discovery. You can also add it manually via **Settings → Devices & services → Add integration → DiFluid Microbalance**.

## Limitations

- The scale accepts **one** BLE connection. While Home Assistant is connected, the DiFluid app cannot connect. Disable the integration temporarily to use the app.
- Powering the scale off via Bluetooth does not work. The documented command is acknowledged, but the scale stays on.
- The scale does not accept settings while the timer is running.
- Only the cleartext channel is used. The encrypted channel requires DiFluid's server-side activation and is not supported.

## Protocol

Based on DiFluid's public protocol description in [DiFluid/difluid-sdk-demo](https://github.com/DiFluid/difluid-sdk-demo/blob/master/docs/protocolMicrobalance.md) (MIT).
Some behaviour (e.g. timer state `15`, "timer ready") was observed on the device and is not in the documentation.

## License

MIT
