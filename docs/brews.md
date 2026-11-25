# Brew detection, brew data and the websocket API

Reference for [DiFluid Microbalance for Home Assistant](../README.md). The README has the overview; this page has the exact rules.

- [Brew detection](#brew-detection)
- [Brew entities](#brew-entities)
- [Brew metrics](#brew-metrics)
- [Stored brew](#stored-brew)
- [Event `difluid_scale_brew_completed`](#event-difluid_scale_brew_completed)
- [Websocket API](#websocket-api)

## Brew detection

Brew detection follows the **scale's own timer**.

- **Start and end.** A brew starts when the timer starts and ends when it stops, plus a short capture tail (`tail_seconds`, default 3 s).
  - With the [auto-stop](auto-stop.md) the tail is extra weighing *after the weight is calm*, before the integration stops the timer.
  - Without it (timer stopped by hand or by the scale) recording goes on for `tail_seconds` after the stop, but the paused scale sends no weight, so drops after the stop are not counted: stop the timer once the drips are in.
- **Discarded runs.** Runs shorter than `min_duration` (5 s) or with less than `min_yield` (5 g) are not saved. Resetting the timer without a run is not a brew.
- **Dose** = the last stable weight between `dose_min` and `dose_max` (12–25 g), stable within ±`stability_tolerance` (0.3 g) for at least `stability_seconds` (3 s), seen within `dose_lookback_minutes` (30 min) before the brew. Each weighed dose is used for one saved brew only; a discarded run (e.g. a purge) does not use it up. Without such a plateau, dose and ratio stay unknown.
- **Yield** = the last weight recorded (with the auto-stop: at the end of the extra weighing after the weight is calm). If the cup is lifted after the timer stop (weight drops by more than 5 g or below zero), the yield is the last weight before the drop, even if the lift comes before the stop was noticed.
- **Ratio** = yield / dose.
- **Bluetooth drops** during a brew do not split it. The brew waits for the scale to reconnect and continues if the scale's timer is still running from the same start.
  - If the timer was reset meanwhile, or the scale is not back within 30 s, the brew is saved with the data up to the drop; if the same timer turns up later (still running, or stopped at a higher count), that brew is updated.
  - If the timer stopped while the link was down, the first weight after the reconnect counts as the yield (unless the cup was already lifted).
  - A link that goes silent before the drop is reported is treated the same way, not as a timer stop.
  - When Home Assistant connects while the timer is already running, the brew starts at the timer's start.
  - A reload of the integration or a Home Assistant restart mid-brew (or during a pause) hands the brew to the new instance.
- **Held-back data** (0.7.0). If Home Assistant or its Bluetooth stack stalls for a moment, the scale's readings arrive late and all at once. The integration puts them back at the time the scale took them (to within about a second), so the saved profile, its [metrics](#brew-metrics) and the live curve show no gap or jump. The [auto-stop](auto-stop.md#learning) does not learn from a shot whose stop fell on such late data.
- **Pause and resume** (e.g. pour-over): if the timer is stopped and later counts on from where it stopped (not reset to 0) within 5 minutes, it is the same brew. The saved brew is updated in place (same id, brew count and coffee total unchanged) and `difluid_scale_brew_completed` fires again with `updated: true`. Resetting the timer ends that possibility, and so does lifting the cup or weighing a new dose during the pause: a timer that then counts on from its stop value is a new brew.

All thresholds in brackets are the defaults; change them under **Settings → Devices & services → DiFluid Microbalance → Configure → Brew detection and stop switch**. Each option has an explanation in the form.

## Brew entities

| Entity | Description |
|---|---|
| Last dose / Last yield | Grams, from the latest brew |
| Brew ratio | Yield / dose of the latest brew |
| Brew duration | Seconds from timer start to timer stop (a paused and resumed brew includes the pause); with the auto-stop up to the cut (pump off), not to the timer stop after the drips |
| Brew count | Total number of brews (total increasing) |
| Coffee ground total | Sum of all known doses in grams (total increasing) |
| Dose correction | Number, 1–60 g: sets the dose of the latest brew manually; the ratio is recomputed. Correcting a dose down and up again does not add to the coffee total twice. |
| Prepare brew | Switch: a brew is about to start. The [card](card.md#ready-mode) shows an empty live chart, and [pump profiles](pump-control.md#prepare-brew-gate) run only if it is on at the pump relay start. Available while the scale is off. Turns itself off when the brew ends (saved or discarded) or after 10 minutes without a brew starting. Starts off after a restart. |

These entities keep their values while the scale is off. The last 20 brews, including their flow profiles, are stored in Home Assistant.

`brew_count` and `coffee_total` are *total increasing* sensors: Home Assistant's long-term statistics read a decrease as a meter reset. Deleting a brew therefore does not reduce them; they count every brew made.

## Brew metrics

Every saved brew gets `metrics`, computed from its stored profile:

| Key | Meaning |
|---|---|
| `first_drop` | Seconds from the timer start to the first drop: the weight is at least 1.5 g above the baseline and stays there for 1 s (the profile must show that second). Baseline: the median weight of the first second, at most 0; if it is above 1.5 g (cup not tared) there is no `first_drop`. Pump vibration moves an empty cup by 0.1–1.0 g, which does not count. |
| `peak_flow` | Highest flow in g/s |
| `mean_flow` | Mean flow in g/s in the main phase: weight gained ÷ time between 20 % and 90 % of the final weight (the ramp of the first drops and the end of the shot left out) |
| `flow_cv` | Variation of the flow in the main phase: standard deviation ÷ mean (0.15 = ±15 %) |

The flow is the one the brew card draws by default (`flow_smoothing: 3`): the weight made monotone (a drop is ignored), then the slope over the preceding 3 s. A value that cannot be computed (no drop, too little in the cup) is null. Brews saved by versions before 0.5.0 get their metrics computed when the integration loads, if they have a profile.

## Stored brew

Each stored brew has `id`, `start`, `dose`, `yield`, `ratio`, `duration`, the flow `profile` (points `[seconds since start, weight g, flow g/s]`, one per 0.25 s), and:

| Field | Meaning |
|---|---|
| `bean` | Name of the active [bean profile](../README.md#bean-profiles) at the brew, or null |
| `target`, `target_source` | The [auto-stop](auto-stop.md) target in g and `ratio` or `yield` (both null with the auto-stop off): the target it cut at if it fired, otherwise the target shown live (ratio × the brew's dose, or the fixed target yield, at the end of the brew). It records what was aimed at: it stays when the dose is corrected later or the auto-stop is turned off afterwards. Brews saved before 0.5.0 have none. |
| `metrics` | See [Brew metrics](#brew-metrics) |
| `switch_latency` | Seconds from the auto-stop's `turn_off` call until the stop switch reported `off` (null without a cut). A cut whose latency could not be measured is marked `switch_latency_unknown: true`. Brews saved before 0.5.1 have neither key. |
| `relay_offset`, `first_drop_shot`, `shot_time`, `since_last_relay_off` | Relay timing, stored for every brew with a pump relay start, in every throttle mode: see [Pump control → relay timing](pump-control.md#relay-timing-stored-with-every-brew) |
| `throttle` | Pump throttle and pump profile record (throttle mode not `off`): see [Pump control → stored fields](pump-control.md#stored-with-every-brew) |

## Event `difluid_scale_brew_completed`

Fired when a brew is saved, and again with `updated: true` when a resumed brew replaces its earlier version. Data: `entry_id`, `device_id`, `brew_id`, `start`, `dose`, `yield`, `ratio`, `duration`, `bean`, `target`, `target_source`, `metrics`, `relay_offset`, `first_drop_shot`, `shot_time`, `since_last_relay_off`, `throttle` and `updated`. Deleting a brew or correcting its dose fires no brew-completed event.

## Websocket API

For your own frontends. All commands accept an optional `entry_id` or `device_id`; with a single scale both can be omitted.

| Command | Result |
|---|---|
| `difluid_scale/brews` | Brew summaries (no profiles), newest first; each with `bean`, `target`, `target_source`, `metrics` and `learned` (true: its sample is in the auto-stop learning journal) |
| `difluid_scale/brew` | One brew including its profile, `bean`, `metrics` and `learned` (`brew_id`, default newest) |
| `difluid_scale/subscribe_brews` | Event with the newest brew summary whenever a brew is saved or its dose corrected |
| `difluid_scale/subscribe_live` | Live data of the running brew (below) |
| `difluid_scale/profile` | A pump profile's definition (0.7.0, for the [profile preview](card.md#profile-preview-tile-feature)): `profile` (`label` (a built-in's id), `name` (0.7.0: for people, built-ins in `language`), `revision`, `builtin`, `standard` (0.7.0: the built-in Standard), `throttle` = `target_time` for Target time, `phases` with `name` (built-ins translated), `key` (the stored name), `duration`, `power` or `flow`, `transition`, `transition_duration`, `targets`), `floor` (the pump throttle floor, %), `floor_entity`, `target_time_entity`, `standard_power_entity`, `ratio_entity` (the target ratio number), `pump_controllable` (mode `throttle` and a usable pump power entity), `auto_stop_target` (a stop switch and a target ratio or yield). The active profile, or `label` (an id, an old German label or an imported label); optional `language` (e.g. `en-GB`, `de`; default: the server's). Also accepts `entity_id` (any entity of the scale, e.g. the profile select) instead of `entry_id`/`device_id` |

`subscribe_live` events (`type` field):

- `live_start`: `start`, `start_ts`, `dose` (the dose this brew will get, or null), `points` (everything recorded so far, so a client subscribing mid-brew can draw the whole curve), plus the current values.
- `live_points`: only the new `points` since the last event, sent at most every 0.25 s, plus the current values. `replace_from` (0.7.0): normally `null`; a number (seconds since start) after [held-back data](#brew-detection) was put back in place: drop your points with `t >= replace_from`, then append `points`. Clients that ignore the field keep working. Apart from this, points once sent never change.
- `live_end`: `brew_id` and `brew` (summary) if saved, otherwise `reason` (`too_short`, `low_yield`, `no_samples`, `unloaded`) and `detail`, or `reason` `ended` without `detail` when the live brew ended without a result of its own (e.g. a parked brew taken over by another run).
- `live_idle`: sent once on subscribe when no brew is running.
- `live_ready`: `ready` (true/false, the state of the Prepare brew switch), `dose` (the dose the next brew would get, or null) and `target` / `target_source` (its auto-stop target, or null with the auto-stop off); `dose` and `target` are null while `ready` is false. Sent on subscribe right after `live_start` / `live_idle`, and again whenever the switch, the dose or the target changes; when a brew ends, `ready: false` comes just before `live_end`.

Points are `[seconds since start, weight g, flow g/s]`. Current values:

| Key | Meaning |
|---|---|
| `weight`, `flow`, `timer` | Latest values from the scale |
| `elapsed` | Seconds; stops at the auto-stop cut, else at the timer stop |
| `phase` | `brewing`, `tail`, `parked` (waiting for a reconnect) |
| `stop` | Seconds at the timer stop, or null |
| `cut` | Seconds at the auto-stop cut, or null |
| `target`, `target_source` | The brew's auto-stop target, or null with the auto-stop off |
| `throttle` | Live pump throttle / pump profile data (`profile`, the current `phase`, the power trace, `pump`: a pump power entity is set up, `controlled`: the pump is controlled), or null |
