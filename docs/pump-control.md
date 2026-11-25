# Pump control: pump profiles and the pump throttle (experimental)

Reference for [DiFluid Microbalance for Home Assistant](../README.md#pump-profiles-and-pump-throttle-experimental). The README has the overview; this page has the requirements, the safety rules, the profile format, the throttle's maths and every stored field.

**Status: experimental, untuned.** Pump profiles have not been run on a real machine yet; all built-in numbers are starting points. The throttle's prediction and controller were developed in simulation. Try everything in `observe` first.

- [What you need](#what-you-need)
- [Modes](#modes)
- [Safety and pump restore](#safety-and-pump-restore)
- [Prepare brew gate](#prepare-brew-gate)
- [Pump profiles](#pump-profiles)
  - [Built-in profiles](#built-in-profiles)
  - [First drop deadline](#first-drop-deadline)
  - [Profile format](#profile-format)
  - [Power limits and blooms](#power-limits-and-blooms)
  - [Flow phases](#flow-phases)
  - [Scale data and clock](#scale-data-and-clock)
- [Pump throttle (profile Target time)](#pump-throttle-profile-target-time)
  - [Prediction and trigger](#prediction-and-trigger)
  - [Control](#control)
  - [Pump throttle learning](#pump-throttle-learning)
  - [Simulation results](#simulation-results)
- [Entities](#entities)
- [Stored with every brew](#stored-with-every-brew)
- [Limits](#limits)

## What you need

- The **stop switch** of the [auto-stop](auto-stop.md) must be the relay that starts and stops the pump. Shot time 0 is its `off → on`, measured in Home Assistant (the scale's own timer can start late over Bluetooth). A relay that was `unavailable`/`unknown` before is no pump start. A scale brew is matched to a relay start at most 20 s before and 5 s after the brew start. Brews started with the machine's own button have no relay start; they are recorded but never throttled or profiled.
- An active auto-stop target (target ratio or target yield). The throttle needs it for its prediction; a profile defines only phases, so without a target its shot ends only when the relay goes off.
- For `throttle` mode: a **number entity that sets the pump power**, chosen in the options as *Pump power* (Configure → Brew detection and stop switch; e.g. an ESPHome dimmer with phase-angle or pulse-skip control in the pump circuit). Powers are in % of its maximum (`value / max`; e.g. 0–100 in steps of 2 or 0–255 in steps of 1); its `min` is a lower bound of its own, values stay on its grid (`min + n × step`) and the floor is rounded up to the next grid value. Without it the throttle and the profiles can only observe.
- For the throttle (Target time) also a tared cup: its prediction starts at the first drop.

## Modes

The select **Pump throttle** sets the mode for every pump profile, the Target time throttle included (the entity and option keys keep the throttle's name):

| Mode | Shown as | Effect |
|---|---|---|
| `off` | Off | No profile runs, nothing is computed, the pump is untouched. The relay timing is still stored. |
| `observe` (default) | Observe (dry run) | Computed and stored with the brew, the pump is never written. A profile's power trace is what it *would* have commanded on this shot (the card draws it dashed); the shot runs at full power meanwhile, so weight exits and the flow controller see the unprofiled shot. The throttle reports its prediction, trigger and the first power it *would* have set (`would_set`, and `would_need_floor` when that needed the floor); nothing after that is simulated, since the pump does not react. |
| `throttle` | Control pump | The pump power is set. |

## Safety and pump restore

The pump must fall back to full power **on its own** when Home Assistant or the connection is gone (e.g. firmware that returns to 100 % without a command, at power-up or when the pump is off). The integration tries, but cannot guarantee it.

- **Every shot starts at full power.** In `throttle` mode the integration sets the pump to its maximum when the relay goes on, when it is set up (and when the options change), at the end of every shot (auto-stop cut, relay off, brew stored or discarded, scale disconnected mid-shot) and when it is unloaded — each time only if it changed the pump in this shot or the pump stands below its maximum.
- **After an auto-stop cut** the restore waits until the stop switch reports `off` (at the latest 5 s after a successful call, when a failed cut is given up, or when the switch shows `on` again with no retry pending): the pump keeps its power while it still runs, and a failed cut never leaves the machine running at full power.
- **One writer.** Pump commands are sent one at a time, the newest value wins, errors are only logged (the auto-stop never waits for them). After a command, the command counts as the power in effect until the entity shows it; an entity that does not show it within 2 s is warned about once.
- **Pump left low.** A pump this integration set below its maximum is remembered (`restore_pending` in the store, across restarts). The flag clears only when the entity reports its maximum after the maximum was asked for — never on the old state a pump still shows right after a lower command (ESPHome, MQTT and Zigbee numbers report after the call). "At its maximum" allows the smaller of half a grid step and 0.5 % of the range (99.9 of 100 counts; 90 on a step-10 grid never does).
- If the restore at the end of a shot fails (pump entity unavailable, e.g. the ESP's Wi-Fi, or a restart mid-shot), it is retried when the entity comes back from unavailable/unknown with a lower number and at every start — in every mode, also `observe` and `off` — at most once every 30 s per pump. A retry asked for within those 30 s is not dropped but runs once at their end, and a retry whose call failed does not count towards the 30 s. A pump that never reports never causes a loop of commands.
- A relay that goes unavailable or unknown mid-shot ends the shot (profile and Target time) and sets the pump back.

## Prepare brew gate

A pump profile runs only if the integration's **Prepare brew** switch (`switch.<scale>_prepare_brew`) is **on when the pump relay goes on**. Otherwise the shot runs at full power (the Standard behaviour) and one debug line is logged.

- Flushes, rinsing and a backflush cleaning program run at full power as long as *Prepare brew* is off during them. The switch stays on for up to 10 minutes without a brew, so a flush in that time is profiled like a brew.
- The first skip in `throttle` mode is logged once at info level (later ones at debug), so a missing *Prepare brew* automation shows up in the log.
- Turn the switch on before the shot, by hand or from an automation (e.g. the one that tares the cup, the relay following a second later).
- The switch is read only at the relay start: it turns itself off when the brew ends (or after 10 minutes without a brew starting), and that never ends a running profile.
- A skipped profile is visible: the card shows "*profile* skipped (not prepared): full power" under the time, the brew stores `profile_reason` `not_prepared`, and the select's `last_outcome` is `not_prepared`.
- The Target time profile (the throttle) keeps its own rules (a relay start matched to a scale brew with a tared cup); it does not need *Prepare brew*.

## Pump profiles

A pump profile shapes a shot through the *Pump power* entity, with the same floor, modes and safety rules as the throttle. It runs from the pump relay's `off → on` (shot time 0) until the shot ends: relay off (or unavailable/unknown), auto-stop cut, unload, another stop switch or pump power entity in the options, or another mode. Saving bean profiles, a target ratio (written to the active bean), detection settings or importing/deleting profiles during a shot does not end it.

The shot is still ended by the **auto-stop**; a profile only defines its phases and their exits. Without a pressure sensor the only control variable is the pump power (%), the only feedback the scale: the cup weight and the scale flow, which lags the pump by about 1–1.5 s plus its 2 s flow window. The scale is blind until the first drops fall.

The select **Pump profile** chooses the active profile; it takes effect at the next relay start and is kept across restarts. A bean profile can name a pump profile (`profile`, in the options flow — *none* for no default — or with `add_bean`): selecting the bean selects it, also when it becomes active because the active bean was deleted. A bean **without** a profile leaves the active profile as it is. Since 0.7.0 a bean remembers the pump profile chosen while it is active and keeps a target ratio per profile; a choice during a brew applies from the next brew ([bean profiles](../README.md#bean-profiles)). Deleting a pump profile removes it from the beans that named it; they go back to their *Standard* ratio.

**Migration (0.7.0).** Each bean's ratio becomes its ratio for its pump profile (*Standard* without one); nothing is lost. After a downgrade to 0.7.0 the beans keep working with that ratio, and a ratio changed there is taken over on the next upgrade.

Import, export and delete your own profiles with the [profile services](services.md#pump-profile-services).

### Built-in profiles

Read-only. Untuned starting values derived from published Decent and Scott Rao profiles — **not measurements**. The pump's stall threshold on your machine is unknown, so nothing goes below the floor except an explicit 0 % bloom. Last phases run 120 s (the auto-stop ends the shot).

**Ids and names (0.7.0).** The built-in profiles have ids: `standard`, `soft_preinfusion`, `blooming`, `allonge`, `lever`, `target_time` — the select's options, the stored active profile, a bean's default profile, the `profile` of new brew records and `export_profile` use them. Home Assistant shows them and the built-ins' phase names in its language (English or German). The ids, the built-ins' names and their old labels from the test versions are reserved: an import with such a label (any case) is refused. **Breaking for the test versions:** `select.select_option` and state comparisons on the *Pump profile* select now need the id (see [Upgrading](upgrading.md#upgrading-to-070b5-test-version)). Services (`export_profile`, `delete_profile`, a bean's `profile`) accept the id or the old label from the test versions. Imported profiles keep their label and phase names. Brew records stored before keep their old label; the card shows the built-in's name for it.

**Migration (0.7.0).** On the first start the learning store goes from 1.3 to 1.4: an active profile stored by its German label is stored by its id; an imported profile whose label equals a new id (e.g. "lever") is renamed to "<label> (imported)" (or "<label> (imported 2)", … when that name exists; the suffix is English in every language, it is a stored name). The config entry goes from 1.2 to 1.3: a bean's default pump profile naming a built-in by its German label names it by its id. **Downgrade to 0.7.0:** the learning store and the entry keep the ids; 0.7.0 does not know them, so its select starts at Standard and a bean's default profile id is ignored (the active profile stays); choose the profile again. Records written by 0.7.0 show the id as the profile name there.



| Profile | Phases (untuned starting values) |
|---|---|
| `standard` — Standard | The *Standard pump power* for the whole shot (default 100 %: the behaviour without profiles). |
| `soft_preinfusion` — Soft preinfusion | Preinfusion at 40 % (at least the floor) until 2 g in the cup or 15 s; then a 4 s linear ramp to 100 %. |
| `blooming` — Blooming | After Rao/Decent: fill at 100 % until the first drops (1.5 g) or 12 s; bloom at 0 % for 30 s; 5 s linear ramp into a flow target of 2.2 g/s. (Decent's original fills at 4 ml/s until 4 bar, holds flow 0 for 30 s, ramps to 2.2 ml/s and aims at 1:2 in 60–70 s with ~8 g dripped before the ramp; without a pressure sensor the fill ends at the first drops instead.) |
| `allonge` — Allongé | After Rao: 100 % until the first drops (1.5 g) or 12 s, then a flow target of 4.5 g/s; meant for a coarse grind and ~1:5 (set the ratio on the auto-stop or the bean). A vibration pump may not reach 4.5 g/s at all: then the pump stays at 100 % and the shot is a Standard shot. |
| `lever` — Lever approximation | 100 % until 4 g in the cup (at most 20 s), hold 100 % for 8 s, then a linear decline to 60 % (at least the floor) over 20 s, and hold. Unlike lever profiles such as Decent's LRv3 it has no low-pressure preinfusion before the peak (that needs pressure control). |
| `target_time` — Target time | 100 % plus the [pump throttle](#pump-throttle-profile-target-time): a shot predicted too fast is slowed to the target shot time. The only profile that runs the throttle. |

Every weight exit counts only when held for 1 s (see [Profile format](#profile-format)), so "until the first drops (1.5 g)" means 1.5 g held for 1 s.

### First drop deadline

For "no coffee → abort" automations, the *Pump profile* select's attribute `first_drop_deadline` is the shot time (s after relay on) after which "no coffee" is plausible for the active profile. It is the maximum end of the profile's first phase with a weight exit (`gte` on `volumetric` or `ratio`), plus the next phase's transition, plus any 0 % phases right after it (drops may legitimately come during a bloom pause) and the ramp out of them, plus a **3 s buffer** (scale delay, the 1 s hold, frame gaps). It is `null` where the profile does not delay the first drops compared with a shot without profile — no weight exit, or 100 % up to it with no pause after — then keep your automation's own default. A profile can set it with `x_first_drop_deadline` (s).

| Built-in | `first_drop_deadline` |
|---|---|
| `standard`, `target_time` | null (no weight exit) |
| `soft_preinfusion` | 22 s (15 s preinfusion + 4 s ramp + 3 s) |
| `blooming` | 50 s (12 s fill + 30 s bloom + 5 s ramp + 3 s) |
| `allonge`, `lever` | null (100 % until the drops, no pause: as without profile) |

Use the same first-drop rule as the profiles in the automation: the cup weight **at least 1.5 g for 1 s** (not a single frame above 1 g; pump vibration moves an empty cup by up to 1 g). Example: abort when, `running_first_drop_deadline` (or your default when it is null) seconds after the relay went on, the weight has not been ≥ 1.5 g for 1 s.

### Profile format

A subset of the [GaggiMate profile JSON](https://docs.gaggimate.eu/docs/profiles/) (fields as parsed by GaggiMate's `profile.h`):

- **Profile:** `label` (required; unique, case-insensitive, at most 60 characters; not empty, `none`, `unknown` or `unavailable`), `type` (`standard` | `pro`, default `pro`), `description`, `temperature` (accepted, ignored: no temperature control), `phases` (1–20).
- **Phase:** `name`, `phase` (`preinfusion` | `brew`), `valve` (1 or absent, the normal brewing position; 0 is rejected: the valve is not controllable), `duration` (required, the phase's maximum in s, at most 600), `temperature` (ignored), `pump`, `transition`, `targets`.
- **`pump`:** a number = power in % of the pump entity's maximum; `{"target": "flow", "flow": 2.2}` = a flow target in g/s (at most 10) on the **scale flow** (not GaggiMate's pumped ml/s).
- **`transition`:** `{"type": "instant" | "linear" | "ease-in" | "ease-out" | "ease-in-out", "duration": s}` over time (`target` `time` or absent; `adaptive` is ignored). It runs from the power commanded at the phase start to the phase's power.
- **`targets`:** up to 8 of `{"type", "operator": "gte" | "lte" (default gte), "value"}` with
  - `volumetric`: cup weight in g (at most 500), as the scale shows it: net since tare;
  - `flow`: scale flow in g/s; counts only after the first drops (before them the scale flow is about 0 and an `lte` exit would fire at once);
  - `ratio`: × the brew's dose (at most 20), i.e. a weight; GaggiMate's parser knows it too (`TARGET_TYPE_RATIO`). Without a known dose the target is ignored and logged.
- **Phase end:** when its duration has elapsed **or** the first of its targets is met **for 1 s** on end (pump vibration moves an empty cup by 0.1–1.0 g; one frame never ends a phase). First drops = the brew metrics' rule: at least 1.5 g held for 1 s. Without scale data (no brew detected, cup not tared, scale silent) weight and flow targets are skipped and only durations end phases (logged once per shot). After the last phase the pump goes back to 100 %.
- **Rejected** with a clear error because they need a pressure sensor: `pump` `{"target": "pressure"}`, target types `pressure` and `pumped`, transitions over `volumetric`/`pumped`.
- **Unknown fields** are kept (export returns them) but ignored. Own extensions start with `x_`: `x_first_drop_deadline` (above); `x_throttle` is reserved for Target time.

Minimal example (the `import_profile` example):

```json
{"label": "My profile", "type": "pro", "phases": [{"name": "Brew", "phase": "brew", "duration": 60, "pump": 100}]}
```

### Power limits and blooms

Every power is clamped to [floor, 100 %] (the *Pump throttle floor*, rounded up to the entity's grid); only an explicit `0` (a bloom: pump off) goes below it. Ramp values between 0 and the floor are raised to the floor.

That the pressure stays in the puck during a 0 % phase assumes the machine keeps its three-way valve closed while the pump is off and the stop relay stays on (as on a vibration-pump machine whose relay powers pump and valve together); a machine that opens the valve releases the pressure instead.

- **A pump entity with `min` > 0 cannot be off:** a 0 % phase then runs at that minimum (slow preinfusion, not a bloom). It is warned once per shot, recorded as `bloom_not_possible`, and a **repair issue** appears while such a profile is active (mode not `off`).
- **The scale's own auto stop timing** (switch *Auto timer stop*) **must be off for profiles with a bloom:** the scale may stop its timer when the flow stops, which ends the brew — and with it the auto-stop — mid-shot. A profile with a 0 % phase logs a warning when it is on, records `scale_auto_stop_timing` and raises a **repair issue** while it is active.

Both issues disappear by themselves when the cause is gone (other profile, mode `off`, setting changed).

### Flow phases

The start power is the learned pump model's prediction for the target flow (`flow ∝ power^γ`, only once it has learned at least one step and the scale shows a flow), else the power before (after a 0 % bloom: 100 %); a transition runs to it. Before the first drops (1.5 g held 1 s) it holds.

Then a slow controller corrects at most once every 3.5 s — only when the scale's 2 s flow window holds nothing from before the last change plus 1.5 s of pump/puck lag (the same principle as the throttle) — by 70 % of the relative error through the pump model, at most 15 % points per step, within [floor, 100 %], ignoring errors up to 0.1 g/s (scale noise). Below 0.2 g/s no relative correction is possible; the power is raised by a step.

These gains are **untuned**, chosen in simulation only (`tests/throttle_sim.py`): with 1–1.5 s total lag and pump exponents 1–2.2 the flow settles at the target (mean within 0.15 g/s) with the power dithering by up to 8 % points; on a short, coarse 20 s shot it reacts late (the flow only exceeds the target late in the shot). After a bloom without a learned pump model the flow phase starts at 100 % and comes down in steps of at most 15 points every 3.5 s: expect the flow to overshoot the target for the first seconds.

### Scale data and clock

The profile reads every accepted weight frame from the relay start on — whether the scale's timer runs or not (it may start ~10 s late or only after the first drops): the cup's tare is the reference, the scale shows the weight net since it. A 0 % bloom (no flow for 30 s) does not end the brew or trigger the auto-stop: the brew ends with the scale timer, the auto-stop cuts only at its target weight.

The run advances on every scale frame and on a 0.25 s tick without one (the resolution of a ramp, not a delay); commands go through the throttle's single pump queue (newest value wins). An error ends the run and sets the pump back to full power.

## Pump throttle (profile Target time)

The throttle runs only while the pump profile **Target time** is active; with any other profile it stays inactive (`reason` `profile`), so exactly one controller sets the pump per shot.

A shot that would end at least **5 s before the target shot time** is slowed down while it runs, by lowering the pump power, so that it ends at the target shot time. Shots predicted between target − 5 s and the target are left alone; every shot starts at 100 %.

The target shot time is the entity *Target shot time* (10–90 s, default 30), or the bean's own (`shot_time` in the bean profile).

### Prediction and trigger

From the first drop + 3 s on, every weight sample gives a prediction of when the auto-stop will cut:

- **Power law.** The weight follows a power law from 2 s before the first drop (`w ∝ s^p`), with `p` fitted to the running shot (log w against log s since the first drop) and blended with the installation's learned exponent. The shot's own fit weighs n/(n+2) for n weight samples (0.75 at the first fit, over 0.9 after 20), so it dominates and the learned value mainly steadies the first samples (single shots spread by about ±0.2 around the learned value).
- **Trend.** From first drop + 6 s a second estimate with the measured flow and its rise (at most 0.25 g/s²) joins, and the later of both counts.

**Learned exponent.** The installation's exponent starts at 1.55 and is then learned from your own saved brews that were started with the relay, cut at an auto-stop target and not throttled (fitted per brew on the main phase, a forgetting mean over ~14 brews, limited to 1.0–2.5; shown as the attribute `prediction_exponent` of *Predicted shot time*). Throttled brews (their profile up to the trigger only) are kept in a separate pool that counts only when none of the last 10 learnable brews was unthrottled (otherwise a machine whose shots are always throttled would never learn). Brews run with another pump profile that lowered the pump are not used.

**Trigger.** It needs

- the predictions of the last second (at least 3 frames, collected from the first drop on) all at or below target − 5 s (a noise filter),
- until first drop + 6 s, the same for a cautious prediction with the running shot's exponent two standard errors lower (an early fit over a short stretch is uncertain, and a too high exponent predicts too early),
- and at least 4 s left.

The cautious check is a trade-off: a flat shot (low exponent) that is only just fast enough may not be throttled at all (in simulation it cut false triggers on flat pucks from 8 to 1 of 43 without missing a fast shot on the stored profiles).

### Control

- **First step.** It compares the wanted flow with the average flow the shot would still have at full power (the flow keeps rising). While the pump model has fewer than 3 samples, the first step goes at most to half the current power — with learning off it never gets samples, so then always (with an unknown pump a step straight to the floor could not be caught up on a slow pump; the price: fast shots end about 0.6 s early on average in simulation).
- **Then** a target weight curve runs linearly from the current weight to the cut weight at the target shot time; a PI controller on the weight error asks for a flow, and a one-parameter pump model `flow ∝ power^γ` turns the ratio *wanted / current flow* into a new power, always relative to the current power.
- A change is followed by at least 3 s without another (2 s flow window + 1 s for pump and puck to settle), plus the dead time measured at the first step (counted up to 2.5 s). One correction changes the power by at most ×0.7 to ×1.2. Powers never go below the floor.
- In the last second before the target no correction is made, except that a shot behind its curve goes to full power at once.
- **Unreachable.** A shot that ends more than 1 s early is marked `unreachable`, with `unreachable_reason`:
  - `floor`: the floor held it — lower the floor if this happens often, as far as your pump still pumps;
  - `trigger_cap`: the first step was held at half the power because the pump model is not learned — turn on *Pump throttle learning*;
  - `rate_limit`: the per-correction limit held it;
  - `late_trigger`: neither; too little shot left after the trigger for the pump to act.
- **Grind finer.** Every shot the throttle found too fast (it triggered, also in `observe`) is marked `grind_finer`, and the card shows **grind finer**: a throttled coarse shot runs in normal time but is not the same as a finer grind.

### Pump throttle learning

Switch *Pump throttle learning*, default off. From the first power step of at least 15 % in a throttled shot: once the flow has visibly responded (half the expected change, 3 frames in a row; no response within 6 s: no sample), its window past that point is compared with the trend of the weight before the step (the power law with the running shot's own exponent, extrapolated). `γ` (start 1, i.e. flow proportional to power; limited to 0.3–3) is learned by recursive least squares with forgetting factor 0.93.

With learning on, corrections wait for this measurement at most 8 s after the step while the model has fewer than 4 samples, then at most 5.5 s (with learning off they never wait). Samples are kept in a learning journal like the auto-stop's: deleting a brew takes its sample out. *Reset pump throttle learning* returns `γ` and the prediction exponent to their start values.

### Simulation results

All from `tests/throttle_sim.py`, not from a machine.

- **End time** (two sets of 20 shots per case, scale timer 0.3–2.6 s after the relay): with a responsive pump the shot ends 1.5 s early to 1.0 s late even when the pump model is far off (pump exponents 1–2.5, dead zones) — wider than ±1 s on the early side.
- **Slow pump** (1.5 s dead time, 1 s lag, 1 s scale delay): corrections come only every ~5 s and the end lies between 1.6 s early and 0.8 s late with learning on (floor 30 % or pump exponent 2.0). With learning **off**, such a pump misses ±1 s in most shots (pump exponent 2.0: 26 of 40 simulated shots, about −2.1 to +4.2 s; exponent 1.5: 21 of 40, −1.9 to +2.5 s) — turn learning on for a slow pump.
- **Late, slow shot** (first drop at 12 s): not reachable with the default floor of 40 %; with a 20 % floor it ends 3.8 s early to 0.1 s late. ±1 s is not guaranteed then.
- **Learning** (20 shots each): it reads pump exponents 0.7 / 1.5 / 2.5 as about 0.73 / 1.48 / 2.29 with a pump that responds within ~1–2 s, and 0.69 / 1.48 / 2.20 with a slow one (response ~4 s; 4–7 samples from 20 shots); a steep, late puck with outlier frames 0.63 / 1.39 / 2.22.

## Entities

| Entity | Description |
|---|---|
| Pump profile | Select: the built-in profiles and your imported ones (by label). Kept across restarts (in the learning store, like the imported profiles). Takes effect at the next relay start. Attributes (the profile ones show translated): `first_drop_deadline`; `pump_controllable` (0.7.0: the pump power is really controlled — mode `throttle` and a configured pump power entity that shows a number); `auto_stop_target` (0.7.0: a stop switch and a target ratio or yield are set); `profile_revision` (0.7.0: short hash of the active profile's definition, changes when a profile is imported over it); while a profile shot runs `running`, `running_phase`, `running_first_drop_deadline` (s after relay on) and `running_first_drop_deadline_at` (ISO time); `bloom_not_possible`; `last_outcome` (`applied` / `observed` / `not_prepared`) and `last_outcome_profile` of the **last relay start** (a flush or a cleaning pulse counts too); `last_brew_outcome` (`applied` / `observed` / `not_prepared` / `throttle` / `off` / `not_run`) and `last_brew_profile` of the last relay start that was **matched to a scale brew** (flushes and cleaning never overwrite it). |
| Pump throttle | Select: `off` / `observe` / `throttle` (shown as *Off* / *Observe (dry run)* / *Control pump*); the mode of every pump profile. |
| Target shot time | Number, 10–90 s (default 30). A bean profile can set its own (*Target shot time* in the bean form); attribute `effective` = the one for the active bean. |
| Pump throttle floor | Number, 10–100 % of the pump entity's maximum (`value / max`, default 40), rounded up to the entity's next step; the entity's own `min` applies too. If brews are often marked `unreachable` with reason `floor`, lower it (as far as your pump still pumps). |
| Standard pump power | Number (0.7.0), from the *Pump throttle floor* to 100 %, step 1, default 100: the power the profile **Standard** runs at in `throttle` mode (100 % = full power, as before; in `observe` only recorded; without a pump power entity no effect). Global, not per bean; read at the relay start, so a change takes effect with the next shot. Kept across restarts. A setting, not part of the profile definition: `export_profile` of Standard exports the definition at 100 %. |
| Pump throttle learning | Switch (default off). |
| Predicted shot time | Seconds, the running brew's prediction (written at most once a second). Attributes: `mode`, `pump_configured`, `stop_switch_configured`, `target_shot_time`, `last_reason` (why the last brew was not throttled: `no_target`, `no_relay_start`, `no_first_drop` (cup not tared), `timer_after_first_drop` (the scale timer started after the first drops), `no_pump`, `pump_unavailable`, `profile` (another pump profile than Target time was active)), `prediction_exponent`, `prediction_exponent_samples`, `prediction_exponent_throttled_samples`. |
| Last pump power | % (also what it would have set in `observe`). Attributes: the last brew's `mode`, `applied`, `triggered`, `unreachable`, `unreachable_reason`, `would_set`, `would_need_floor`, `grind_finer`, `target_time`, `predicted_at_trigger`, `end_shot_time`. |
| Pump power exponent | Learned `γ` (diagnostic). |
| Pump throttle learning samples | Diagnostic; attributes `journal_samples`, `journal_base_samples`. |
| Reset pump throttle learning | Button. |
| Pump throttle (event) | Event entity: `triggered`, `corrected`, `ended` (in `observe` only `triggered` and `ended`). Attributes: `mode`, `shot_time`, `target_time`, `predicted_shot_time`, `predicted_at_trigger`, `power`, `applied`, `target_flow`, `target_weight`, `unreachable`, `unreachable_reason`, `would_set`, `would_need_floor`, `grind_finer`, `bean`, on `ended` also `end_reason`. |

The *Stop deviation* sensor of the auto-stop gets `throttled` and the mean deviation of the throttled brews (`throttled_deviation_mean`, `throttled_deviation_count`); the auto-stop model stays shared, its journal entries of throttled brews are marked `throttled`. A profile shot that lowered the pump counts as `throttled` there too.

## Stored with every brew

### Relay timing (stored with every brew)

Every brew with a matched relay start, in any mode (also `off`), stores:

| Field | Meaning |
|---|---|
| `relay_offset` | s from relay on to the scale timer start |
| `first_drop_shot` | relay on → first drop |
| `shot_time` | relay on → end of the brew time |
| `since_last_relay_off` | s since the stop switch last went off before this relay start, remembered across restarts; null until one was seen. Flushes with the machine's own button and steaming are not visible to it. |

### Throttle and profile record

With the mode not `off`, a brew also stores `throttle`:

- Throttle fields: `mode`, `active`, `reason`, `target_time`, `applied`, `controlled` (0.7.0: the throttle controlled the pump — mode `throttle` and a usable pump entity — whether or not it wrote a value; the card's line style), `pump` (0.7.0: a *Pump power* entity was set up at the brew start; without one the `power` trace is an assumption, not set or read, and the card draws no band), `triggered`, `too_late`, `trigger_shot_time`, `predicted_at_trigger`, `prediction_exponent`, `unreachable`, `unreachable_reason`, `would_set`, `would_need_floor`, `grind_finer`, `end_shot_time`, `end_reason`, `power` (`[t, %]`, t in profile time), `learned`, `sample`, `sample_reason`.
- Profile fields: `profile` (the active profile's label). For a profile run other than Target time also `applied` (a value was written), `controlled` (0.7.0: the run controlled the pump), `power` (`[t, %]` on every change), `phases` (`[t, index, name, exit reason]`; reasons `volumetric`, `flow`, `ratio`, `duration`, `end`), `profile_end_reason` (`relay_off`, `relay_lost`, `cut`, `unloaded`, `options` (stop switch or pump entity changed), `mode`, `no_weight`, `error`), `profile_ignored_targets`, `first_drop_deadline`, `bloom_not_possible`, `scale_auto_stop_timing`, `reason` `profile` (or `no_pump` / `pump_unavailable` when a profile in `throttle` mode was only observed for lack of a usable pump entity) and `target_time` null. Without *Prepare brew*: `profile_reason` `not_prepared` and no trace.

The websocket API and the [brew-completed event](brews.md#event-difluid_scale_brew_completed) carry the same fields; `subscribe_live` has them as the current value `throttle` (with `profile`, the current `phase` and the power trace). The card draws the power in the pump band (dashed when observed), names the profile in the legend, marks the target shot time and uses the shot time for the time and the first drop (see [card](card.md#pump-power-band)).

## Limits

- The throttle only acts after the first drop + 3 s, and the remaining main phase is short: very fast shots stay too fast (marked `unreachable`). The effect on taste of a throttled shot is not measured.
- Flow targets work on the scale flow, which lags; the controller is deliberately slow and cannot follow fast changes.
- Profiles control the pump power only (no pressure, no temperature, no valve) and are not tested on a machine.
- With the weight unit "gering" there is no auto-stop, no throttle and no pump profile: a running throttle or profile ends with `no_weight`, the pump goes back to full power, and a warning is logged.
