# Upgrading from older versions

Reference for [DiFluid Microbalance for Home Assistant](../README.md#upgrading-from-051-to-070). The README covers 0.5.1 → 0.7.0. Coming from an older version, read the notes below from your version upwards; each upgrade happens on the first start, so you can jump straight to the newest version.

## Testers of the 0.6.0 and 0.7.0 test versions

- 0.6.0 was never released; 0.7.0 follows 0.5.1.
- A learning store from 0.6.0 starts with the pump profile *Target time* (`target_time`) active, so shots behave as before (the throttle).
- The tare rule of the 0.7.0 test versions is replaced by *Prepare brew* (see [Prepare brew gate](pump-control.md#prepare-brew-gate)); the option *Tare window* is removed (config entry 1.2).
- Brews stored by the 0.7.0 test versions may carry the profile reason `no_tare` instead of `not_prepared`.
- From 0.7.0b5 or older: each bean's ratio becomes its ratio for its pump profile (config entry 1.4); a downgrade keeps working with that ratio.

## Upgrading to 0.7.0b5 (test version)

The built-in pump profiles are named by ids (`standard`, `soft_preinfusion`, `blooming`, `allonge`, `lever`, `target_time`) and shown in Home Assistant's language. Converted on the first start: the active profile and every bean's default profile (learning store 1.4, config entry 1.3); brews stored before keep their German label (the card shows the name for it).

**Breaking — automations and dashboards** that name a built-in profile by its German label must use the id:

- `select.select_option` on the *Pump profile* select: `option: soft_preinfusion` instead of `option: Sanfte Vorbrühung`. Home Assistant checks the option against the select's options (now the ids) and refuses the old label; the integration cannot accept it there.
- State triggers and conditions on that select (`state: "Zielzeit"` → `state: target_time`), and templates comparing its state.
- The attributes `running`, `last_outcome_profile`, `last_brew_profile` (a built-in's id) and `running_phase` (a built-in phase's key: `brew`, `preinfusion`, `fill`, `bloom`, `flow`, `peak`, `decline`) — shown translated in the UI, the raw value is the id/key. `first_drop_deadline` is unchanged.
- A tile card's `select-options` feature with an explicit `options:` list: list the ids.

The integration's own services (`export_profile`, `delete_profile`, a bean's profile) still accept the old German labels.

**Known:** an imported profile renamed by the migration gets the English suffix " (imported)" in every language (a stored name); reasons for an invalid profile import are English.

**Downgrade to 0.7.0b4:** the converted ids stay; 0.7.0b4 does not know them, so its select starts at Standard and a bean's default profile is ignored until set again.

## Upgrading to 0.5.1

- Deleting a brew no longer reduces the brew count and the coffee ground total.
- The learned model is kept. Its learning journal starts empty, so deleting a brew learned before 0.5.1 leaves the model unchanged; the switch latency is measured from the first auto-stop on.
- The sensor "Stop latency" is now named "Learned stop latency" (entity ID unchanged).

## Upgrading to 0.5.0

- The **Timer** sensor no longer has a state class (it restarts with every brew). Home Assistant then lists its old long-term statistics under **Developer tools → Statistics**; delete them there.
- On the first start the targets are taken over from the target entities, the stored brews get their metrics, and the stores are converted (1.2). A downgrade to 0.4.x still reads them. Targets changed while on 0.4.x are overwritten by the stored 0.5 settings when upgrading again.
- From 0.5.0 on the overshoot the auto-stop learns also includes late drops, so the first shots after updating from 0.4.x may land about one drop above the target until the model has caught up.
- The cut's flow estimate changed from the slope over the last second between two samples (up to 0.4.0) to a Theil–Sen slope over the last 2 s.
