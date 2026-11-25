<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/radecke/ha-difluid-microbalance/main/custom_components/difluid_scale/brand/dark_logo@2x.png">
    <img alt="DiFluid Microbalance" src="https://raw.githubusercontent.com/radecke/ha-difluid-microbalance/main/custom_components/difluid_scale/brand/logo@2x.png" width="412">
  </picture>
</p>

# DiFluid Microbalance for Home Assistant

A Home Assistant custom integration for the **DiFluid Microbalance** coffee scale over Bluetooth Low Energy.

- **Live scale:** weight, flow rate, brew timer and battery; tare and timer control from Home Assistant; an event for the buttons on the scale.
- **Brew recording:** every brew is saved automatically with dose, yield, ratio, brew time, the full flow profile and shot metrics (first drop, peak flow, mean flow and its variation), also when Home Assistant or Bluetooth stalls for a moment; sensors for the latest brew and running totals; an event for your own automations.
- **Brew card:** a bundled dashboard card draws the flow curve live, shows each brew's target, bean, pump profile and metrics, lets you browse (also by swiping), replay and delete the last 20 brews, and can expand to full screen while a brew is prepared or running.
- **Prepare brew:** a switch that puts the card into a ready view before the brew and gates the pump profiles.
- **Auto-stop:** switches the espresso machine (any switch, e.g. its smart plug) off early enough that the cup ends on a target ratio (× the weighed dose) or a target yield. It learns its timing from every shot, measures the stop switch's delay, retries a failed switch-off and reports every attempt on an event entity and in repair issues.
- **Bean profiles:** per coffee a target ratio for each pump profile (and optionally a target shot time); a bean remembers its pump profile. Pick the bean and profile and ratio follow.
- **Pump profiles and pump throttle (experimental):** on machines whose pump power can be set from Home Assistant (a number entity, e.g. a dimmer in the pump circuit), shape the shot — gentle preinfusion, blooming, allongé, a lever-like decline, or your own profiles in a subset of the GaggiMate format — or slow down a shot that would end too early (profile *Zielzeit*). With the stop switch as the pump relay every brew also gets its real shot time.
- **Diagnostics** download, repair issues, automatic migration of older stores.

Tested with model `DFT-S101`, firmware `V320` and `V326`.

> Not affiliated with or endorsed by DiFluid.

**Upgrading from 0.5.1?** See [Upgrading from 0.5.1 to 0.7.0](#upgrading-from-051-to-070).

**Reference documentation:** [Brew detection and websocket API](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/brews.md) · [Brew card](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/card.md) · [Auto-stop](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/auto-stop.md) · [Pump control](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/pump-control.md) · [Services](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/services.md) · [Upgrading from older versions](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/upgrading.md)

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [Entities](#entities)
- [Brew recording](#brew-recording)
- [Brew card](#brew-card)
- [Auto-stop](#auto-stop)
- [Bean profiles](#bean-profiles)
- [Pump profiles and pump throttle (experimental)](#pump-profiles-and-pump-throttle-experimental)
- [Services](#services)
- [Events](#events)
- [Upgrading from 0.5.1 to 0.7.0](#upgrading-from-051-to-070)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)

## Requirements

- **Home Assistant 2026.5.4 or newer.**
- A working [Bluetooth](https://www.home-assistant.io/integrations/bluetooth/) setup with an adapter or proxy that supports **active connections** (e.g. an ESPHome Bluetooth proxy).
- For the auto-stop: a switch that ends the shot when turned off (e.g. the machine's smart plug or pump relay).
- For pump profiles and the pump throttle, additionally: the stop switch is the pump relay. Setting the pump power (mode `throttle`) needs a number entity for it; `observe` works without one.

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

1. Turn the scale on. Home Assistant discovers it automatically; confirm the discovery. You can also add it via **Settings → Devices & services → Add integration → DiFluid Microbalance**.
2. Optional: **Settings → Devices & services → DiFluid Microbalance → Configure**:
   - **Brew detection and stop switch:** the brew detection thresholds (the defaults fit espresso; each has an explanation in the form), the **Stop switch** for the [auto-stop](#auto-stop) and the **Pump power** entity for [pump control](#pump-profiles-and-pump-throttle-experimental).
   - **Add / Edit / Delete bean profile:** see [Bean profiles](#bean-profiles).
3. Add the [brew card](#brew-card) to a dashboard.

A scale can be replaced without losing anything: **Settings → Devices & services → DiFluid Microbalance → Reconfigure** keeps history, learning, settings and entity IDs.

## Entities

Entity IDs in the examples assume a scale named "Microbalance".

| Group | Entities |
|---|---|
| Scale | **Weight** (g; ounce readings are converted to grams), **Flow rate** (g/s), **Timer** (brew timer, counts in seconds as observed; unit not documented), **Battery** (%; attributes `charging`, `device_state`), **Device state** (idle, timing, paused, …), **Connected** (binary sensor for the BLE connection, always available) |
| Scale controls | **Tare**, **Start / Stop / Reset timer** (buttons); **Auto timer start** / **Auto timer stop** (switches for the scale's "auto detect / auto stop timing" settings); **Weight unit** (select: gram / ounce / gering — the third unit as named in the DiFluid protocol; no gram conversion is documented, so the weight shows no value in this mode) |
| Scale buttons | **Key press** (event entity for buttons pressed *on the scale*: `power_click`, `timer_click`, `power_double`, `timer_double`, `power_long`, `timer_long`, `timer_press`) |
| Brews | **Last dose**, **Last yield**, **Brew ratio**, **Brew duration**, **Brew count**, **Coffee ground total**, **Dose correction** (number: sets the dose of the latest brew), **Prepare brew** (switch) — [details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/brews.md#brew-entities) |
| Auto-stop | **Target ratio**, **Target yield**, **Learned stop latency**, **Stop drip**, **Last switch latency**, **Stop learning samples**, **Stop deviation**, **Auto-stop** (event), **Reset stop learning** — [details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/auto-stop.md#entities) |
| Beans | **Bean** (select) |
| Pump control | **Pump profile**, **Pump throttle** (mode select), **Target shot time**, **Pump throttle floor**, **Standard pump power**, **Pump throttle learning**, **Predicted shot time**, **Last pump power**, **Pump power exponent**, **Pump throttle learning samples**, **Reset pump throttle learning**, **Pump throttle** (event) — [details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/pump-control.md#entities) |

The brew entities keep their values while the scale is off.

## Brew recording

The integration records each brew automatically, following the **scale's own timer**: a brew starts when the timer starts and ends when it stops, plus a short capture tail (`tail_seconds`, default 3 s).

- Runs shorter than 5 s or with less than 5 g yield are discarded.
- **Dose** = the last stable weight between 12 and 25 g (stable within ±0.3 g for at least 3 s) within 30 minutes before the brew: tare the dosing cup or portafilter, add the beans, then brew. Each weighed dose is used for one saved brew only.
- **Yield** = the last weight recorded; lifting the cup after the stop does not count. **Ratio** = yield / dose.
- Without the auto-stop, stop the timer once the drips are in: the paused scale sends no weight.
- Bluetooth drops, a Home Assistant restart mid-brew and pause/resume (e.g. pour-over, within 5 minutes) do not split a brew.
- The last 20 brews, including their flow profiles, are stored in Home Assistant. Each gets shot metrics: `first_drop`, `peak_flow`, `mean_flow`, `flow_cv`.

All rules and stored fields: [Brew detection and brew data](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/brews.md).

## Brew card

The integration ships a Lovelace card and registers it as a dashboard resource automatically (storage mode). It plots the **flow rate** of a brew over time, with the weight as a second curve, and lets you browse the stored brews — with the arrow buttons or, on touch devices, by swiping (to the right for older brews, to the left for newer ones).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/radecke/ha-difluid-microbalance/main/docs/brew-card-dark.png">
  <img alt="Brew card showing a real espresso: 16.0 g dose, 43.2 g yield (target 43.2 g), ratio 1:2.70, 24.9 s shot time, with bean, pump profile Standard, shot metrics and the smoothed flow line" src="https://raw.githubusercontent.com/radecke/ha-difluid-microbalance/main/docs/brew-card-light.png" width="100%">
</picture>

<sub>A real brew recorded by the integration with the auto-stop: 16.0 g dose, 43.2 g yield, 24.9 s shot time (pump on to off).</sub>

```yaml
type: custom:difluid-brew-card
device_id: <your scale's device id>   # optional with a single scale
title: Espresso                       # optional
chart_height: fill                    # optional: pixels (100–2000) or fill
expand_when_active: true              # optional: full screen while ready or brewing
flow_smoothing: 3                     # optional: seconds (0–10), 0 = raw scale flow
```

- **Live:** as soon as a brew starts, the card switches to it and shows time, flow and weight in large figures, with the auto-stop target under the weight.
- **Ready:** while **Prepare brew** is on and no brew runs, the card shows empty axes, the weighed dose and the target the next brew would get.
- **Full screen** (`expand_when_active: true`): the card covers the dashboard view while ready or brewing and shrinks back when the brew is saved.
- **Stored brews:** a calm smoothed flow line, bean and metrics, the target, **replay** in real time, and **delete** (with a confirmation in the card).
- **Pump control:** the commanded pump power as a band under the chart (with a *Pump power* entity; dashed when only observed), and its value where you point when the pump was controlled.
- **Profile preview:** a tile feature for the *Pump profile* select draws the selected profile under its dropdown (what ends each phase over the graph, the phase names under it); for *Zielzeit* it sets the target shot time, for *Standard* the Standard pump power instead; the brew ratio under it:

```yaml
type: tile
entity: select.<your scale>_pump_profile
features:
  - type: select-options
  - type: custom:difluid-profile-preview
grid_options:
  rows: auto   # sections view: let the card grow with the preview
```

In a sections view the grid sizes a tile card from the number of its features, one feature row each; the preview is taller than one row and would overflow into the next card, so the card needs `rows: auto`.

Dashboards in YAML mode need the resource by hand, with the integration's version as `?v=` (change it with every update so that browsers load the new card):

```yaml
lovelace:
  resources:
    - url: /difluid_scale/difluid-brew-card.js?v=0.7.0
      type: module
```

All options and views: [Brew card](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/card.md).

## Auto-stop

Set a **target ratio** (e.g. 2.5, yield ÷ dose) and/or a **target yield** (g), and choose a **stop switch** (Configure → Brew detection and stop switch → *Stop switch*; turning it off must end the shot at once). While a brew runs, the integration turns that switch off early enough that the weight in the cup **after the dripping has settled** lands on the target. Without a stop switch, or with both targets at 0, the feature is off. With a dose detected the target is ratio × dose; without one, the target yield applies.

**How it works, briefly.** After the switch goes off the weight still rises (switching delay, pump, drips). The overshoot is modelled as `c + tau × flow at the cut` and learned from every saved shot (recursive least squares; roughly the last 14 shots count). The scale's timer keeps running after the cut until the weight is calm plus `tail_seconds`, so the saved yield includes the drips; the brew time ends at the cut. Broken weight samples, lifted cups and unclear shots are not learned. Deleting a brew takes its sample out of the learning again.

**Stop switch delay.** Each cut measures how long the switch takes to report `off`, and the learning is corrected for it. A switch that does not report `off` within 5 s three times in a row raises a repair issue.

**Failed switch-off.** If the switch cannot be turned off (call failed, switch missing or unavailable), the cut is retried for 2 minutes, and at once when the switch comes back. A repair issue appears with the first failed attempt.

Every step is reported on the event entity **Auto-stop**: `cut`, `switch_failed`, `cut_failed`, then how it ended (`learned` or a reason).

### Notify on a failed cut

The repair issue shows up in the settings, which nobody watches mid-shot. Example:

```yaml
automation:
  - alias: "Coffee: auto-stop could not switch off"
    triggers:
      - trigger: state
        entity_id: event.microbalance_auto_stop
        # Event entities restore their last event after a restart. Without this
        # line the last switch_failed/cut_failed would notify again after every restart.
        not_from: ["unavailable", "unknown"]
    conditions:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.event_type in ['switch_failed', 'cut_failed'] }}
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Coffee machine"
          message: >
            {{ 'Gave up switching it off' if trigger.to_state.attributes.event_type == 'cut_failed'
               else 'Could not switch it off, still trying' }} – check the cup!
```

The model, every attribute, the retry timing, late cuts after a Bluetooth gap and the repair issues: [Auto-stop](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/auto-stop.md).

## Bean profiles

Under **Configure → Add / Edit / Delete bean profile** each coffee gets a name, a target ratio and optionally a note, a target shot time (pump throttle) and a pump profile. The select **Bean** picks the active one:

- **A bean remembers its pump profile** (0.7.0). Choose a pump profile while a bean is active and it becomes that bean's profile; choosing the bean later selects it again. A bean without a profile leaves the active profile as it is.
- **One target ratio per bean and pump profile** (0.7.0), e.g. Pacandé with *Standard* 1 : 2.7 and with *Allongé* 1 : 5. Choosing a bean or a profile loads the matching ratio; changing the target ratio stores it for the active bean and profile. A profile the bean has no ratio for yet starts with the profile's own ratio target, if it has one, else with the bean's current ratio. The ratio in the bean form belongs to the profile chosen in that form. Existing beans are taken over unchanged.
- **During a brew** (0.7.0) the running shot keeps its target: a bean or profile chosen meanwhile applies from the next brew. A target ratio set by hand applies at once.
- The active bean is kept across restarts.
- As soon as there is a bean profile, one is always active: the first one you add becomes active, and if the active one is deleted the newest remaining one (the one created last; editing does not move a profile) takes over — its ratio becomes the target (during a brew only once that brew has ended, see below; the brew keeps the bean it started with). After deleting the last profile no bean is active and the target ratio stays; `none` is offered only while there are no profiles.
- If no active bean is stored (e.g. after an update) the newest one becomes active at startup; otherwise a restart keeps the stored target ratio, also one set by hand.
- Every saved brew stores the bean's name (`bean`, or null). Renaming or deleting a profile does not change brews saved before.

Beans can also be added and deleted from a dashboard with the services [`add_bean` and `delete_bean`](#services), e.g. a text helper for the name and two buttons. When a button is pressed on a dashboard, a service error (empty or taken name) shows up as a notification in the frontend.

```yaml
# Helper: input_text.new_bean, max length 60 (Settings → Devices & services → Helpers)
script:
  add_bean:
    alias: Add bean
    sequence:
      - action: difluid_scale.add_bean   # ratio: the current target ratio; empty/taken names are rejected with a message
        data:
          name: "{{ states('input_text.new_bean') }}"
      - action: input_text.set_value
        target: {entity_id: input_text.new_bean}
        data: {value: ""}

# Dashboard
type: vertical-stack
cards:
  - type: entities
    entities:
      - select.microbalance_bean        # your scale's "Bean" entity
      - input_text.new_bean
  - type: horizontal-stack
    cards:
      - type: button
        name: Add bean
        icon: mdi:seed-plus-outline
        tap_action: {action: perform-action, perform_action: script.add_bean}
      - type: button
        name: Delete bean
        icon: mdi:seed-off-outline
        tap_action:
          action: perform-action
          perform_action: difluid_scale.delete_bean
          data: {active: true}           # the bean selected above
```

## Pump profiles and pump throttle (experimental)

**Status: experimental, untuned.** Needs hardware most machines do not have. No profile has been run on a real machine yet; all built-in values are starting points. Try everything in `observe` first.

**What you need:** the auto-stop's **stop switch** is the relay that starts and stops the pump (shot time 0 = its `off → on`), and a **number entity that sets the pump power**, chosen as *Pump power* in the options (e.g. an ESPHome phase-angle or pulse-skip dimmer in the pump circuit; powers are in % of its maximum). The pump must fall back to full power **on its own** when Home Assistant or the connection is gone. In `throttle` mode the integration sets it to its maximum at every shot start and end and remembers a pump it left low, but it cannot guarantee it.

**Mode** (select *Pump throttle*, applies to every profile): `off` – nothing runs, the pump is untouched; `observe` (default) – a dry run: computed and stored with the brew, the pump is never written; `throttle` – the pump power is set.

**Pump profile** (select): the profile for the next shot.

| Profile | Shape (untuned starting values) |
|---|---|
| `standard` — Standard | The *Standard pump power* for the whole shot (default 100 %: as without profiles) |
| `soft_preinfusion` — Soft preinfusion / Sanfte Vorbrühung | 40 % until 2 g in the cup or 15 s, then a 4 s ramp to 100 % |
| `blooming` — Blooming | 100 % until the first drops or 12 s, pump off for 30 s, 5 s ramp into a flow target of 2.2 g/s |
| `allonge` — Allongé | 100 % until the first drops or 12 s, then a flow target of 4.5 g/s (coarse grind, ~1:5) |
| `lever` — Lever approximation / Hebel-Annäherung | 100 % until 4 g (at most 20 s), hold 8 s, then a 20 s decline to 60 % |
| `target_time` — Target time / Zielzeit | 100 %, plus the **pump throttle** below |

Built-in profiles are stored, selected and exported by their id (0.7.0) and shown in Home Assistant's language (English or German); their old German labels are still accepted by the integration's services. **Coming from 0.7.0:** automations that call `select.select_option` with an old German label or compare the select's state with one must use the id — see [Upgrading](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/upgrading.md#upgrading-to-070b5-test-version). Imported profiles keep their label.

The auto-stop still ends the shot; a profile only shapes the pump power on the way. Your own profiles are imported in a subset of the [GaggiMate profile format](https://docs.gaggimate.eu/docs/profiles/) (no pressure targets: there is no pressure sensor) with the [profile services](#services).

**Prepare brew gate.** A profile runs only if the **Prepare brew** switch is **on when the pump relay goes on**; otherwise the shot runs at full power. This keeps flushes, rinsing and cleaning programs at full power. Turn the switch on before the shot, by hand or from an automation (e.g. the one that tares the cup). A skipped profile is shown on the card and stored with the brew (`not_prepared`). Zielzeit does not need the switch.

**Pump throttle (profile *Zielzeit*).** From the first drop + 3 s the throttle predicts when the auto-stop will cut. A shot that would end at least **5 s before the target shot time** (entity *Target shot time*, 10–90 s, default 30, or the bean's own) is slowed down by lowering the pump power so that it ends at the target. It never goes below the *Pump throttle floor* (default 40 %). With *Pump throttle learning* on it learns how your pump responds. A shot it found too fast shows **grind finer** on the card.

For bloom profiles, turn off the scale's **Auto timer stop** (it may end the brew during the pause); a repair issue tells you when it is on, or when the pump entity cannot go to 0 %.

Requirements, safety rules, the profile format, the first-drop deadline for "no coffee" automations, the throttle's maths and simulation results, all entities and stored fields: [Pump control](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/pump-control.md).

## Services

All services return response data. With a single scale the scale can be omitted (else `entry_id`/`device_id` for the brew services, `config_entry_id` for the others).

| Service | Purpose |
|---|---|
| `difluid_scale.delete_brew` | Delete a saved brew and take it out of the auto-stop and throttle learning (`brew_id`). Brew count and coffee total stay. |
| `difluid_scale.learn_stop_from_brew` | Teach the auto-stop from a past brew (`brew_id`, `cut_offset`; optional `target`, `switch_latency`). |
| `difluid_scale.add_bean` | Add a bean profile (`name`; optional `ratio`, `note`, `shot_time`, `profile`, `activate`). |
| `difluid_scale.delete_bean` | Delete a bean profile (exactly one of `name`, `bean_id`, `active: true`). |
| `difluid_scale.import_profile` | Import a pump profile (`profile` as JSON; optional `overwrite`, `activate`). |
| `difluid_scale.delete_profile` | Delete an imported pump profile (`label`). |
| `difluid_scale.export_profile` | Return a pump profile's JSON (`label`, default the active one). |

Fields, rules and responses: [Services](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/services.md).

## Events

| Event | Fires |
|---|---|
| `difluid_scale_brew_completed` (bus event) | When a brew is saved (again with `updated: true` when a resumed brew replaced it). Data: `brew_id`, `start`, `dose`, `yield`, `ratio`, `duration`, `bean`, `target`, `target_source`, `metrics`, relay timing and pump data — [all fields](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/brews.md#event-difluid_scale_brew_completed) |
| **Key press** (event entity) | A button pressed on the scale |
| **Auto-stop** (event entity) | `cut`, `switch_failed`, `cut_failed`, then `learned`, `outlier`, `not_settled`, `cup_lifted`, `brew_discarded`, `brew_truncated`, `switch_latency_unknown` or `delayed_data` — [details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/auto-stop.md#event-entity-auto-stop) |
| **Pump throttle** (event entity) | `triggered`, `corrected`, `ended` — [details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/pump-control.md#entities) |

Event entities restore their last event after a restart: a state trigger on them needs `not_from: ["unavailable", "unknown"]` (see the [example](#notify-on-a-failed-cut)).

Own frontends can use the [websocket API](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/brews.md#websocket-api).

## Upgrading from 0.5.1 to 0.7.0

0.6.0 was never released; 0.7.0 follows 0.5.1. Everything is converted on the first start.

- **Minimum Home Assistant version 2026.5.4.**
- The learning store is converted to 1.4: the auto-stop model, its journal, the targets and bean profiles stay as they are. The pump throttle is added in mode `observe` with its start values, and the pump profile **Standard** is active. Without a *Pump power* entity nothing changes at the machine; with one, choose a profile and the mode yourself.
- New: pump profiles and the pump throttle (entities, the option *Pump power*, the services `import_profile`, `export_profile`, `delete_profile`); built-in profiles are named by ids (`standard`, `soft_preinfusion`, `blooming`, `allonge`, `lever`, `target_time`) and shown in your language; bean profiles keep a target ratio per pump profile, remember their pump profile and can carry a target shot time (each bean's ratio becomes its ratio for *Standard*); every brew with a pump relay start stores its relay timing, and with a stop switch or pump entity the pump profile it ran with.
- **Pump profiles need *Prepare brew*:** a profile runs only if the *Prepare brew* switch is on when the pump relay goes on; otherwise the shot runs at full power. Zielzeit is the exception ([details](#pump-profiles-and-pump-throttle-experimental)).
- **Check automations on the Auto-stop event entity.** `switch_failed` now means "the first attempt failed, still retrying", not a final state; the new event type `cut_failed` marks giving up after 2 minutes. Trigger on both, with `not_from: ["unavailable", "unknown"]` (see [the example](#notify-on-a-failed-cut)).
- New repair issues: the auto-stop cannot / could not switch the machine off (`auto_stop_cut_failed*`: still trying, gave up, gave up with the pump set back to full power), and for bloom profiles the scale's auto stop timing is on or the pump entity cannot go to 0 % (`profile_bloom_*`).
- New auto-stop behaviour: a failed cut is retried for 2 minutes, and a brew that passed its target during a Bluetooth gap is cut at once when the scale is back ([details](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/auto-stop.md#late-cuts-after-a-gap-in-the-scale-data)).
- The config entry moves to version 1.4; a downgrade to 0.5.1 still loads it, with each bean's ratio for its own profile.
- A scale can now be replaced: **Reconfigure** keeps history, learning, settings and entity IDs.
- Weight-jump diagnostics are debug messages now (0.5.x logged a warning with the device address for every cup).
- New auto-stop conclusion `delayed_data`: a shot whose stop fell on scale readings delayed by a Home Assistant or Bluetooth stall is not learned.

Coming from an older version, or from a 0.6.0/0.7.0 test version: [Upgrading from older versions](https://github.com/radecke/ha-difluid-microbalance/blob/main/docs/upgrading.md).

## Limitations

- The scale accepts **one** BLE connection. While Home Assistant is connected, the DiFluid app cannot connect. Disable the integration temporarily to use the app.
- Powering the scale off via Bluetooth does not work. The documented command is acknowledged, but the scale stays on.
- The scale does not accept settings while the timer is running.
- The scale sends no weight while its timer is paused; weight updates resume when the timer runs again or is reset.
- A reload of the integration (or a restart) in the middle of a brew gives that brew the bean that is active when it ends, not the one at its start.
- A reload, restart or Bluetooth drop between an auto-stop cut and the timer stop: the timer keeps running until it is stopped by hand, and the saved brew time is the full timer time (the cut time is only kept in memory).
- With bean profiles there is no "no bean" choice; delete all profiles to brew without one.
- The pump throttle only acts after the first drop + 3 s, and the remaining main phase is short: very fast shots stay too fast (marked `unreachable`). Its prediction and controller were developed in simulation; the effect on taste of a throttled shot is not measured.
- Pump profiles are untuned and untested on a machine; they control the pump power only (no pressure, no temperature, no valve). They run only while *Prepare brew* is on at the relay start.
- With the scale set to **ounces** the flow is taken as g/s like in grams (not verified with a recording); auto-stop and throttle are tested in grams only. The unit "gering" has no gram conversion: then there is no auto-stop, no throttle and no pump profile (a running throttle or profile ends with `no_weight`, the pump goes back to full power, and a warning is logged).
- After a Home Assistant or Bluetooth stall, the delayed readings are put back to within about a second of when the scale took them.
- Only the cleartext channel is used. The encrypted channel requires DiFluid's server-side activation and is not supported.

## Troubleshooting

- **The scale is not found or does not connect.** It needs an adapter or proxy with active connections, and the DiFluid app must not be connected. Right after power-on the scale sometimes refuses the notification subscription (`Insufficient authorization`), so the integration waits 1 s after connecting before it subscribes and leaves 0.3 s between its setup commands.
- **The card still shows the old version after an update.** Reload the dashboard (in the companion app: refresh). With dashboards in YAML mode, update `?v=` in the resource URL.
- **No dose or ratio.** The dose must be a stable weight between 12 and 25 g within 30 minutes before the brew; adjust the range under Configure if your doses differ.
- **The auto-stop does nothing.** Check that a stop switch is configured, a target is above 0 and the cup was tared (a weight more than 5 g above the target never triggers a cut). Watch the **Auto-stop** event entity and **Settings → Repairs**.
- **A pump profile never runs.** *Prepare brew* must be on when the pump relay goes on; otherwise the *Pump profile* select's attribute `last_outcome` is `not_prepared`, and the first skip in `throttle` mode is logged at info level.
- **Repair issues** (they disappear by themselves once the cause is gone): stop switch missing or unavailable; stop switch does not report `off` in time; auto-stop cannot / could not switch the machine off; bloom profile with the scale's auto stop timing on; bloom profile with a pump entity whose `min` is above 0.
- **Diagnostics:** **Settings → Devices & services → DiFluid Microbalance → ⋮ → Download diagnostics** gives the options, the learned stop model (`tau`, `c`, covariance, sample count) with its learning journal, the measured switch latencies, the last auto-stop result, targets and bean profiles, device info and firmware, the connection state, the last five brews without their profiles, the pump throttle (settings, pump entity and its range, model, journal, prediction exponent), the active pump profile and the number of imported profiles (not their contents), and the shot timing of the last ten brews with their flow profile and throttle record (to re-check the prediction later). The Bluetooth address and the serial number are redacted.
- **Debug log:** a raw weight jump of 50 g or more between two readings is logged at debug level with the frame data and the last raw notifications, to track down broken frames (placing or lifting a cup does this too).

Issues: [github.com/radecke/ha-difluid-microbalance/issues](https://github.com/radecke/ha-difluid-microbalance/issues)

## Development and tests

Without Home Assistant: `python3 tests/test_brew.py` (brew detection), `python3 tests/test_stop_model.py` (auto-stop model), `python3 tests/test_throttle.py` (pump throttle on simulated shots of two different machines, see `tests/throttle_sim.py`) and `python3 tests/test_profiles.py` (pump profile format, executor, every built-in profile on both simulated machines with a fine and a coarse grind).

The full suite needs the Home Assistant test helpers: `pytest-homeassistant-custom-component` up to Home Assistant 2026.10, `pytest-homeassistant` (same version as Home Assistant) from 2026.11 on; then `pytest` from the repository root. It is run against the minimum version from `hacs.json` and the current release. Its fixtures in `tests/fixtures` are copies of real stores (brews with profiles, the learned model, the restored targets).

Card tests (also run by `pytest` when node is installed): `node tests/card_retry_test.mjs` (reconnect handling), `node tests/card_flow_test.mjs` (flow smoothing, the smoothed flow line, the ready view, deleting a brew), `node tests/card_expand_test.mjs` (expansion while active), `node tests/card_throttle_test.mjs` (the pump power band, also of pump profiles with the profile's name, and the shot time), `node tests/card_profile_preview_test.mjs` (the profile preview tile feature), `node tests/card_swipe_test.mjs` (swiping between brews).

## Protocol

Based on DiFluid's public protocol description in [DiFluid/difluid-sdk-demo](https://github.com/DiFluid/difluid-sdk-demo/blob/master/docs/protocolMicrobalance.md) (MIT).
Some behaviour (e.g. timer state `15`, "timer ready") was observed on the device and is not in the documentation.

## License

MIT
