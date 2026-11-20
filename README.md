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

## Brew detection and brew card (0.3.0)

The integration records each brew automatically. Brew detection follows the **scale's own timer**:

- A brew starts when the timer starts and ends when it stops, plus a short capture tail (default 3 s) so drips count towards the yield.
- Runs shorter than 5 s or with less than 5 g yield are discarded. Resetting the timer without a run is not a brew.
- **Dose** = the last stable weight between 12 and 25 g (stable within ±0.3 g for at least 3 s), seen within 30 minutes before the brew. Each weighed dose is used for one saved brew only; a discarded run (e.g. a purge) does not use it up. Without such a plateau, dose and ratio stay unknown.
- **Yield** = weight at the end of the capture tail. If the cup is lifted after the timer stop (weight drops by more than 5 g or below zero), the yield is the last weight before the drop, even if the lift comes before the stop was noticed. **Ratio** = yield / dose.
- **Bluetooth drops** during a brew do not split it: the brew waits for the scale to reconnect and continues if the scale's timer is still running from the same start. If the timer was reset meanwhile, or the scale is not back within 30 s, the brew is saved with the data up to the drop; if the same timer turns up later (still running, or stopped at a higher count), that brew is updated. If the timer stopped while the link was down, the first weight after the reconnect counts as the yield (unless the cup was already lifted). A link that goes silent before the drop is reported is treated the same way, not as a timer stop. When Home Assistant connects while the timer is already running, the brew starts at the timer's start. A reload of the integration or a Home Assistant restart mid-brew (or during a pause) hands the brew to the new instance.
- **Pause and resume** (e.g. pour-over): if the timer is stopped and later counts on from where it stopped (not reset to 0) within 5 minutes, it is the same brew. The saved brew is updated in place (same id, brew count and coffee total unchanged) and `difluid_scale_brew_completed` fires again with `updated: true`. Resetting the timer ends that possibility, and so does lifting the cup or weighing a new dose during the pause: a timer that then counts on from its stop value is a new brew.
- All thresholds can be changed under **Settings → Devices & services → DiFluid Microbalance → Configure**.

| Entity | Description |
|---|---|
| Last dose / Last yield | Grams, from the latest brew |
| Brew ratio | Yield / dose of the latest brew |
| Brew duration | Seconds from timer start to timer stop (a paused and resumed brew includes the pause) |
| Brew count | Total number of brews (total increasing) |
| Coffee ground total | Sum of all known doses in grams (total increasing) |
| Dose correction | Number: sets the dose of the latest brew manually; the ratio is recomputed. Correcting a dose down and up again does not add to the coffee total twice. |

These entities keep their values while the scale is off. The last 20 brews, including their flow profiles, are stored in Home Assistant.
Each brew fires the event `difluid_scale_brew_completed` with `dose`, `yield`, `ratio`, `duration`, `brew_id`, `start` and `updated` (true when a resumed brew replaced its earlier version).

### Brew card

The integration ships a Lovelace card and registers it as a dashboard resource automatically (storage mode). It plots the **flow rate** of a brew over time, with the weight as a second curve, and lets you browse the stored brews.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brew-card-dark.png">
  <img alt="Brew card showing a brew with 16 g dose, 38 g yield, ratio 1:2.4 and 30 s brew time" src="docs/brew-card-light.png" width="720">
</picture>

<sub>Simulated brew: 16 g dose, 38 g yield, 30 s.</sub>

```yaml
type: custom:difluid-brew-card
device_id: <your scale's device id>   # optional with a single scale
title: Espresso                       # optional
chart_height: fill                    # optional: pixels (100–2000) or fill
```

**Chart height:** without `chart_height` the chart is 200 px high. A number sets the height in pixels. `fill` makes the card fill the rest of the screen: the chart grows until the card ends at the bottom of the visible window (never below 200 px), follows window resizes and rotation, and the live figures (time, flow, weight) get larger. Meant for a dashboard section that is shown only during a brew, e.g. on a tablet next to the machine. With a custom height the card lets its row in a sections view grow with it (`rows: auto`). Other values are rejected with an error in the card editor.

**Live mode:** as soon as a brew starts, the card switches to it (also when you are looking at an older brew), shows elapsed time, flow rate and weight in large figures and draws the flow curve and the dashed weight curve as they come in. The axes start at an espresso range (30 s, 3 g/s, 40 g) and only grow. If the scale disconnects mid-brew, the card says so and continues when the scale is back. If the connection to Home Assistant fails, the card retries on its own. When the brew ends, the card shows the saved brew with dose, yield and ratio; a run that is not saved (too short, too little yield) is noted briefly and the card returns to the latest brew.

**Replay:** the play button next to the brew navigation plays a stored brew back in real time, exactly like a live brew (curves building up, growing axes, time, flow and weight). The stop button ends it early; a brew that starts in the meantime takes over the card.

If your dashboard resources are managed in YAML, add the resource yourself:

```yaml
lovelace:
  resources:
    - url: /difluid_scale/difluid-brew-card.js
      type: module
```

### Websocket API

For your own frontends (all commands accept an optional `entry_id` or `device_id`; with a single scale both can be omitted):

| Command | Result |
|---|---|
| `difluid_scale/brews` | Brew summaries (no profiles), newest first |
| `difluid_scale/brew` | One brew including its profile (`brew_id`, default newest) |
| `difluid_scale/subscribe_brews` | Event with the newest brew summary whenever a brew is saved or its dose corrected |
| `difluid_scale/subscribe_live` | Live data of the running brew (below) |

`subscribe_live` events (`type` field):

- `live_start`: `start`, `start_ts`, `dose` (the dose this brew will get, or null), `points` (everything recorded so far, so a client subscribing mid-brew can draw the whole curve), plus the current values.
- `live_points`: only the new `points` since the last event, sent at most every 0.25 s, plus the current values.
- `live_end`: `brew_id` and `brew` (summary) if saved, otherwise `reason` (`too_short`, `low_yield`, `no_samples`, `unloaded`) and `detail`.
- `live_idle`: sent once on subscribe when no brew is running.

Points are `[seconds since start, weight g, flow g/s]`. Current values: `weight`, `flow`, `timer`, `elapsed` (seconds; stops at the timer stop), `phase` (`brewing`, `tail`, `parked` = waiting for a reconnect) and `stop` (seconds at the timer stop, or null).

### Tests

`python3 tests/test_brew.py` runs the brew detection tests without Home Assistant. The full suite needs `pytest-homeassistant-custom-component`: `pytest` from the repository root. `node tests/card_retry_test.mjs` tests the card's reconnect handling (also run by `pytest` when node is installed).

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
