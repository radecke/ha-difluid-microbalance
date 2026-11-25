# Auto-stop at a target ratio or yield

Reference for [DiFluid Microbalance for Home Assistant](../README.md#auto-stop). The README has setup and the overview; this page has the model, the learning rules, every entity attribute and every event.

- [Entities](#entities)
- [Event entity "Auto-stop"](#event-entity-auto-stop)
- [The cut](#the-cut)
- [Learning](#learning)
- [Learning journal](#learning-journal)
- [Stop switch latency](#stop-switch-latency)
- [Failed cuts and retries](#failed-cuts-and-retries)
- [Late cuts after a gap in the scale data](#late-cuts-after-a-gap-in-the-scale-data)
- [Repair issues](#repair-issues)
- [Notes](#notes)

## Entities

| Entity | Description |
|---|---|
| Target ratio | Number, 0–5 (0 = off). With a dose detected for the running brew the target is ratio × dose; without one the target yield applies. With a [bean](../README.md#bean-profiles) active, a new ratio is written to its profile. |
| Target yield | Number, 0–100 g (0 = off). The target when no ratio is set, and the fallback for brews without a detected dose. |
| Learned stop latency | Learned `tau` in seconds (diagnostic); includes the nominal stop switch latency. Its entity ID is `…_stop_latency` (the entity was called "Stop latency" before 0.5.1). |
| Last switch latency | Last auto-stop: seconds from the `turn_off` call until the stop switch reported `off` (unknown: no report within 5 s, switch `off` or unavailable at the cut, or the call failed). Attributes: `nominal_switch_latency` (median of the last 15 measurements, 0.07 s until 3 are known), `measurements` (diagnostic) |
| Stop drip | Learned `c` in grams (diagnostic) |
| Stop learning samples | Number of shots the model has learned from (diagnostic). Attributes: `journal_samples` (of those, the ones in the learning journal, which deleting their brew takes out again) and `journal_base_samples` (older ones; both add up to the state) |
| Stop deviation | Settled yield − target of the last auto-stopped shot. Attributes: `target`, `target_source` (`ratio` or `yield`), `dose`, `bean`, `cut_weight`, `cut_flow`, `predicted_overshoot`, `lead`, `final_yield`, `overshoot`, `accepted`, `reason` (`outlier`, `not_settled`, `cup_lifted`, `brew_discarded`, `brew_truncated`, `switch_latency_unknown`, `delayed_data`), `delayed_data` (true: the stop fell on held-back data), `switch_latency`, `nominal_switch_latency`, `adjusted_overshoot` (the learned value: overshoot − flow × (switch latency − nominal)), and for the pump throttle `throttled`, `throttled_deviation_mean`, `throttled_deviation_count` (mean deviation of the throttled brews) |
| Auto-stop | Event entity, see below |
| Reset stop learning | Button: back to the starting values (also empties the learning journal) |

Both targets are kept by the integration (with the learned model), so the auto-stop keeps its target when a target entity is disabled.

## Event entity "Auto-stop"

One event per step of an auto-stop. Event types:

| Event type | Meaning |
|---|---|
| `cut` | The switch was turned off. `attempt` 1 = the cut itself, > 1 = a retry that succeeded. Fires when the call returns, usually before the switch reports, so it carries no `switch_latency`. |
| `switch_failed` | The `turn_off` call failed, or the switch is missing or unavailable. The cut is then retried (see [Failed cuts and retries](#failed-cuts-and-retries)); failed retries only log. |
| `cut_failed` | Given up after 2 minutes; `attempt` = attempts made. The machine may still run. |
| `learned` | Conclusion: the shot was learned. |
| `outlier`, `not_settled`, `cup_lifted`, `brew_discarded`, `brew_truncated`, `switch_latency_unknown`, `delayed_data` | Conclusion: the shot was not learned, and why (see [Learning](#learning)). `switch_latency_unknown`: the switch did not report `off` within 5 s, was not `on`, or the call failed. |

Attributes: `target`, `target_source`, `dose`, `cut_weight`, `cut_flow`, `predicted_overshoot`, `final_yield` and `deviation` (once known), `bean`; on the conclusion events also `switch_latency`; on `cut`, `switch_failed` and `cut_failed` also `attempt`.

Event entities restore their last state after a restart. An automation with a state trigger on this entity must therefore ignore the change from `unavailable`/`unknown` (`not_from`), or it fires again after every restart — see the [example in the README](../README.md#notify-on-a-failed-cut).

## The cut

After the switch goes off the weight still rises: the command needs time (scale → Bluetooth → Home Assistant → plug → pump), and the puck drips. The overshoot is modelled as `c + tau × flow at the cut`.

**Flow.** The flow is derived from the weight, not from the scale's own flow value, which lags by 1–1.5 s: it is the median of the slopes between all pairs of weight samples of the last 2 s (Theil–Sen), so a sample that falls back by up to ~3 g after a jump (pump vibration) barely moves it. A flow above 8 g/s (espresso runs at 1–3 g/s, the fastest recorded shot peaked at 4.7 g/s) is treated as broken: the cut then only looks at `weight + c`.

**Sample filter.** A single weight sample that jumps further than a shot can pour (more than 5 g down, or more than 5 g + 8 g/s × the time since the last sample up, that time counted up to 1 s) is held back and decided by the next one: back on the old level, it is dropped and never drives a cut; on the new level (cup lifted or placed) both count. A sample more than 3 g above the robust line through the flow window does not trigger the cut on its own; the next sample decides.

**Rule.** On every weight sample of a running brew (the scale's timer is counting) the integration checks

`weight + c + tau × flow + flow × dt/2 ≥ target`

and then turns the switch off (once per brew; the sample handler does not wait for it). `dt/2` anticipates the sampling: the scale sends a weight only every ~0.3 s, so cutting at the first sample past the threshold would land up to one sample interval of flow late (0–1 g). `dt` is the median of the last 15 sample intervals of the running brew (0.3 s until 3 are known, limited to 0.1–1 s); this term is fixed, not learned.

A weight more than 5 g above the target, or one that is already past the threshold when the brew starts, never triggers a cut (a cup that was not tared). Exception: [late cuts after a gap in the scale data](#late-cuts-after-a-gap-in-the-scale-data).

**After the cut** the scale's timer keeps running: while its timer is paused the scale sends no weight, so the drips would be missed. The timer is stopped once the weight is calm (changes by at most 0.2 g within 2 s) and then `tail_seconds` more have passed (default 3 s, for late drops), at the latest 20 s after the cut (a backstop), or at once if the cup is lifted. The weight at that moment is the saved yield, so it includes the drips. In the live view and in the saved brew the brew time ends at the cut (pump off) while the weighing goes on; the scale's own display keeps counting until its timer is stopped. The brew is saved right at that timer stop (no extra capture tail: the drips are already in).

The pump power (pump throttle, pump profiles) is set back to full power only once the stop switch reports `off` after the cut (at the latest 5 s after a successful call, when a failed cut is given up, or when the switch shows `on` again with no retry pending), not while the pump still runs.

## Learning

If the brew was saved normally, the shot is one learning sample: `x` = flow at the cut, `y` = final weight (at the timer stop above; before the lift if the cup was lifted after the weight was calm) − weight at the cut, corrected for the [switch latency](#stop-switch-latency). The overshoot therefore includes late drops.

`tau` and `c` are fitted by recursive least squares with a forgetting factor of 0.93 (roughly the last 14 shots count), starting from `tau` = 1.0 s and `c` = 0.3 g. The first three samples only adjust `tau` (`c` stays at 0.3 g, since the first shots rarely differ enough in flow to separate the two); both are fitted from the fourth on. `tau` stays within 0–3 s, `c` within −0.5–3 g.

Not learned (the conclusion event names the reason):

- `outlier`: an overshoot outside 0–5 g (cup bumped or lifted), a flow at the cut above 8 g/s, or a cut that is not *clean*: a confirmed weight jump (cup touched or lifted, also a gap in the scale data) within the 2 s before the cut, or fewer than 4 frames in that window. A single dropped frame is left out of the flow and does not make the cut unclean.
- `not_settled`: the weight did not calm down within the 20 s after the cut, or the timer was stopped by hand before it did (the saved yield still includes everything up to the timer stop).
- `cup_lifted`: the cup was lifted before the drips settled.
- `brew_discarded`: the run was not saved (too short, too little yield).
- `brew_truncated`: the brew was saved with its data cut short by a Bluetooth drop (see [Bluetooth drops](brews.md#brew-detection)).
- `switch_latency_unknown`: see [Stop switch latency](#stop-switch-latency).
- `delayed_data` (0.7.0): the stop fell on [held-back scale data](brews.md#brew-detection) (Home Assistant or Bluetooth stalled just before it), so it acted on old readings. A stall earlier in the shot does not matter.
- Shots stopped any other way, and [late cuts](#late-cuts-after-a-gap-in-the-scale-data), are never learned.

The model is one model for all beans; the bean is stored with each brew for your own analysis only. Throttled shots (pump throttle or a pump profile that lowered the pump) share the model; their journal entries are marked `throttled`.

## Learning journal

Every accepted sample (flow at the cut, the overshoot actually learned, the brew it came from) is kept in a journal of the last 50 samples, stored with the model. Deleting a brew (card or [`delete_brew`](services.md#difluid_scaledelete_brew)) drops its sample and recomputes the model: starting from the model before the journal's first sample, all remaining samples are fed through the same update in their order. Without a deletion this gives exactly the model learned shot by shot. Samples older than the last 50 are folded into that starting model and can no longer be taken out; with the forgetting factor their weight is below 0.93^50 ≈ 2.6 %. A model learned before 0.5.1 had no journal; its samples are part of that starting model, so deleting one of those older brews leaves the model unchanged.

[`learn_stop_from_brew`](services.md#difluid_scalelearn_stop_from_brew) teaches the model from a past brew, e.g. one stopped by an earlier automation.

## Stop switch latency

On every cut the integration measures the seconds from its `turn_off` call until the stop switch reports `off`. `tau` means the total effective latency including a *nominal* switch latency: the median of the last 15 measured cuts (0.07 s until 3 are known). A learning sample is corrected by the part of its overshoot that the switch's deviation from that nominal caused: `adjusted_overshoot` = overshoot − flow × (switch latency − nominal).

A cut whose latency is unknown — no `off` report within 5 s, switch already `off` or unavailable at the cut, or the call failed — is not learned (`switch_latency_unknown`). After 3 auto-stops in a row without an `off` report in time, a repair issue appears; it clears at the next measured latency.

Each brew stores its `switch_latency` (or `switch_latency_unknown: true`), see [Stored brew](brews.md#stored-brew).

## Failed cuts and retries

If the `turn_off` call fails, or the switch is missing or unavailable, `switch_failed` fires and the cut is retried for **2 minutes**: every second for the first 10 attempts, then every 5 s, and at once whenever the switch shows `on` again (e.g. a Zigbee or ESPHome relay reconnecting).

- A retry that succeeds fires `cut` with `attempt` > 1; failed retries only log.
- A switch reporting `off` ends the retries.
- While the cut has not succeeded, the pump power is not set back to full power.
- After 2 minutes the auto-stop gives up and fires `cut_failed`. With a *Pump power* entity in `throttle` mode the pump then goes back to **full power**, as at the end of every shot.
- A repair issue appears already with the **first** failed attempt ("still trying"), changes its text when the auto-stop gives up, and goes when the switch reports `off` or an auto-stop succeeds.

## Late cuts after a gap in the scale data

A brew whose prediction was below the target and that was not cut yet is cut at once when its weight comes back past the target after a gap in the scale data (Bluetooth drop, the brew resumed).

The same applies to a new brew only if all of these hold:

- the last brew was parked by such a gap (not ended regularly: timer stopped, saved or discarded),
- the stop switch never reported `off` and never was unavailable/unknown in between (the same relay start),
- the new brew starts within 120 s,
- its first weight continues the old one: at most 5 g lower, and at most 5 g plus 1.5 × the last flow (at least 1 g/s) × the time between higher (the shot keeps its flow; a full untared mug is far above that).

A shot stopped at the machine below the target therefore never carries over to the next cup. Such a late cut is never learned.

## Repair issues

| Issue | When | Goes away |
|---|---|---|
| Stop switch could not be used | The configured stop switch does not exist (checked when Home Assistant has started, the integration is reloaded or the options change), or it is missing or unavailable when the auto-stop tries to switch the machine off. A switch that merely becomes unavailable while no brew runs is not reported (e.g. a relay powered from the machine's own socket, which drops off whenever the machine is off). | The switch is available again, another switch is chosen, or the option is emptied |
| Stop switch does not report off in time | 3 auto-stops in a row without an `off` report within 5 s | At the next measured latency |
| Auto-stop cannot / could not switch the machine off | First failed cut attempt ("still trying"); text changes when the auto-stop gives up (and says so when the pump was set back to full power) | The switch reports `off`, or an auto-stop succeeds |

## Notes

- The learned model is stored per scale and removed with the integration.
- Because the timer keeps running until the weight is calm plus `tail_seconds`, the saved brew's yield includes the drips; the brew time ends at the cut.
- "Prepare brew" is not needed for the auto-stop.
- A reload, restart or Bluetooth drop between a cut and the timer stop: the timer keeps running until it is stopped by hand, and the saved brew time is the full timer time (the cut time is only kept in memory).
