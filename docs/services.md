# Services

Reference for [DiFluid Microbalance for Home Assistant](../README.md#services). Every service returns response data; call it with `response_variable` in a script, or in **Developer tools → Actions** with "return response". `export_profile` only returns data.

**Choosing the scale.** With a single scale the target can be omitted. Otherwise:

- `delete_brew`, `learn_stop_from_brew`: `entry_id` (config entry) or `device_id`.
- Bean and profile services: `config_entry_id`.

- [Brews and the auto-stop](#brews-and-the-auto-stop)
  - [`difluid_scale.delete_brew`](#difluid_scaledelete_brew)
  - [`difluid_scale.learn_stop_from_brew`](#difluid_scalelearn_stop_from_brew)
- [Bean services](#bean-services)
  - [`difluid_scale.add_bean`](#difluid_scaleadd_bean)
  - [`difluid_scale.delete_bean`](#difluid_scaledelete_bean)
- [Pump profile services](#pump-profile-services)
  - [`difluid_scale.import_profile`](#difluid_scaleimport_profile)
  - [`difluid_scale.delete_profile`](#difluid_scaledelete_profile)
  - [`difluid_scale.export_profile`](#difluid_scaleexport_profile)

## Brews and the auto-stop

### `difluid_scale.delete_brew`

| Field | Required | Meaning |
|---|---|---|
| `brew_id` | yes | Id of a stored brew |
| `entry_id` / `device_id` | no | The scale |

Removes a saved brew and takes its learning sample out of the auto-stop: the model is recomputed from the remaining samples (see [Learning journal](auto-stop.md#learning-journal)). The same for the pump throttle's power model and prediction exponent. Brew count and coffee ground total stay as they are (they count every brew made). The card and the sensors update. No brew-completed event. A brew that is still running (continued after a pause) or whose auto-stop result is still pending cannot be deleted.

Returns `brew_id`, `removed_from_learning` (false: the brew was not in a learning journal, the model is unchanged; also true for a pump power-model sample) and the auto-stop `model` (`tau`, `c`, `count`) afterwards.

The card's bin icon calls this service.

### `difluid_scale.learn_stop_from_brew`

| Field | Required | Meaning |
|---|---|---|
| `brew_id` | yes | Id of a stored brew |
| `cut_offset` | yes | Seconds since the brew start (0–600) at which the cut happened |
| `target` | no | Target in g (0–100); adds `deviation` to the result |
| `switch_latency` | no | Seconds (0–5); overrides the latency stored with the brew. Needed to learn a brew marked `switch_latency_unknown`. |
| `entry_id` / `device_id` | no | The scale |

Teaches the auto-stop from a past brew, e.g. one stopped by an earlier automation. The profile point at or just before `cut_offset` is the cut: its weight, and the flow computed the same way as live (same sample filter). Learning sample: saved yield − that weight, corrected to the nominal switch latency when a latency is known, with the same acceptance checks as a live shot.

- Brews from before 0.5.1 (latency never measured) are learned uncorrected; a brew whose measurement ended without a value is not learned (reason `switch_latency_unknown`) unless `switch_latency` is given.
- A brew whose sample is still in the learning journal (learned live or by this service, within the last 50 samples) has its sample replaced, not counted twice; an older one, or one learned before 0.5.1, is counted again.
- While the brew's own auto-stop result is still pending the call is refused. A `cut_offset` before the first profile point (or a brew without yield) is an error.

Returns `brew_id`, `cut_offset`, `cut_point_t` (time of the profile point used), `weight_at_cut`, `flow_at_cut`, `clean_cut` (false: a confirmed weight jump within 2 s before the cut or fewer than 4 frames in that window — not learned; a single dropped frame does not count), `final_yield`, `overshoot`, `switch_latency`, `nominal_switch_latency`, `adjusted_overshoot` (the overshoot that was learned; null when the latency is unknown), `predicted_overshoot_before` (the model's prediction for this flow before the update), `target`, `deviation` (final yield − target, if `target` is given), `accepted`, `reason` (null, `outlier` or `switch_latency_unknown`) and the `model` (`tau`, `c`, `count`) after the update.

A stored profile keeps one point per 0.25 s, so a bad frame overwritten in its bucket is not seen here: live and service can differ in `clean_cut`.

## Bean services

### `difluid_scale.add_bean`

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique (case-insensitive), at most 60 characters, not `none`, `Keine`, `unknown` or `unavailable` |
| `ratio` | no | 0.5–5, the bean's ratio for its `profile` (*Standard* without one); default: the current target ratio (an error if that is outside 0.5–5) |
| `note` | no | Free text |
| `shot_time` | no | 10–90 s: the pump throttle's target shot time for this bean |
| `profile` | no | The bean's [pump profile](pump-control.md#pump-profiles), a label; an unknown label is an error. Without one the bean takes the profile chosen while it is active ([bean profiles](../README.md#bean-profiles)) |
| `activate` | no | Default true |
| `config_entry_id` | no | The scale |

Adds a bean profile exactly like the options do, without reloading the integration or touching a running brew. With `activate` it becomes the active bean and the target ratio follows; the first bean always becomes active; a bean with a `profile` makes that pump profile active too.

Returns `bean_id`, `name`, `ratio`, `ratios` (0.7.0: its ratio per pump profile, `{label: ratio}`), `active` (and `shot_time`, `profile` when set). Called from a dashboard button, an error (empty or taken name) shows up as a notification in the frontend.

### `difluid_scale.delete_bean`

Exactly one of:

| Field | Meaning |
|---|---|
| `name` | Bean name (case-insensitive) |
| `bean_id` | Bean id |
| `active: true` | The active bean |

plus optional `config_entry_id`. Like a delete in the options: saved brews keep the name; if it was the active one, the newest remaining bean becomes active and its ratio the target; after the last bean none is active and the target ratio stays. Returns `bean_id`, `name`.

## Pump profile services

Profile format: [Pump control → Profile format](pump-control.md#profile-format).

### `difluid_scale.import_profile`

| Field | Required | Meaning |
|---|---|---|
| `profile` | yes | JSON object or JSON text |
| `overwrite` | no | Default false |
| `activate` | no | Default false |
| `config_entry_id` | no | The scale |

Validates and stores a user profile. A label that exists (case-insensitive) is an error unless `overwrite`; built-in labels can never be overwritten. Errors name the reason (e.g. *needs a pressure sensor*). Returns `label`, `replaced`, `active` (the active profile's label), `phases` (number of phases).

### `difluid_scale.delete_profile`

| Field | Required | Meaning |
|---|---|---|
| `label` | yes | A user profile (not a built-in) |
| `config_entry_id` | no | The scale |

Deletes a user profile. If it was active, Standard becomes active. Beans that named it lose their default profile. Returns `label`, `active`.

### `difluid_scale.export_profile`

| Field | Required | Meaning |
|---|---|---|
| `label` | no | Default: the active profile |
| `config_entry_id` | no | The scale |

Returns (response data only) `label`, `builtin`, `active` and `profile`: the JSON, with unknown fields as imported.
