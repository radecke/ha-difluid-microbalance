// Node-only test of the profile preview tile feature (0.7.0b3): profile
// definition -> drawn points (every built-in profile, an imported one,
// phases of uncertain length, the adaptive throttle profile), the feature
// element (graph, target shot time buttons, switching on a profile change,
// nothing without its entity). Run: node tests/card_profile_preview_test.mjs
// (also run by test_card.py)
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

class FakeHTMLElement {
  constructor() {
    this.style = {};
  }
  attachShadow() {
    this.shadowRoot = {};
    return this.shadowRoot;
  }
}
const registry = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  get: (n) => registry.get(n),
  define: (n, c) => registry.set(n, c),
};
globalThis.window = globalThis;
globalThis.navigator ??= { language: "en" };
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
console.info = () => {};

const here = path.dirname(fileURLToPath(import.meta.url));
const CARD = path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js");
await import(CARD);
const Card = customElements.get("difluid-brew-card");
const Feature = customElements.get("difluid-profile-preview");
const { profilePreview, isProfileSelect } = Card.math;
// Built-in definitions as the websocket sends them; test_profile_preview.py
// checks the file against profiles.BUILTINS.
const DEFS = JSON.parse(fs.readFileSync(path.join(here, "fixtures/profile_definitions.json"), "utf8"));
const FLOOR = DEFS.floor;
// By their (German) display name, as the tests were written; the label is
// the built-in's id (0.7.0b5).
const byLabel = Object.fromEntries(DEFS.builtins.map((p) => [p.name, p]));
let scenarios = 0;

const close = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;
const flat = (prev) => prev.segments.flatMap((s) => s.pts);
const solid = (prev) => prev.segments.filter((s) => !s.dashed && !s.fade);
const dashed = (prev) => prev.segments.filter((s) => s.dashed && !s.fade);
const tail = (prev) => prev.segments[prev.segments.length - 1];

// 1. Every built-in profile has a definition; the adaptive throttle profile
//    (recognised by x_throttle, not by its name) shows no graph.
{
  assert.equal(DEFS.builtins.length, 6);
  const target = DEFS.builtins.filter((p) => p.throttle === "target_time");
  assert.equal(target.length, 1);
  assert.deepEqual(profilePreview(target[0], FLOOR), { kind: "target" });
  // Renamed (another language): still recognised by its id.
  assert.deepEqual(profilePreview({ ...target[0], label: "Target time" }, FLOOR), { kind: "target" });
  assert.equal(profilePreview(null, FLOOR), null);
  assert.equal(profilePreview({ label: "x", phases: [] }, FLOOR), null);
  scenarios++;
}

// 2. Standard: constant 100 %, open end only (no defined length).
{
  const p = profilePreview(byLabel["Standard"], FLOOR);
  assert.equal(p.kind, "graph");
  assert.equal(p.defined, 0);
  assert.equal(p.segments.length, 1);
  assert.equal(tail(p).type, "line");
  assert.equal(tail(p).fade, true);
  assert.equal(tail(p).dashed, false);
  assert.ok(flat(p).every((q) => q[1] === 100));
  assert.ok(p.tMax >= 4);
  scenarios++;
}

// 3. Sanfte Vorbrühung: 40 % (at least the floor) up to 15 s, dashed (2 g
//    may end it earlier), then a 4 s linear ramp to 100 %, open end at 100;
//    its length is not fixed (the 2 g exit).
{
  const p = profilePreview(byLabel["Sanfte Vorbrühung"], FLOOR);
  const pi = dashed(p);
  assert.equal(pi.length, 1);
  assert.deepEqual(pi[0].pts, [[0, 100], [0, Math.max(FLOOR, 40)], [15, Math.max(FLOOR, 40)]]);
  const ramp = solid(p)[0].pts;
  assert.deepEqual(ramp[0], [15, Math.max(FLOOR, 40)]);
  assert.deepEqual(ramp[ramp.length - 1], [19, 100]);
  // Linear: the middle sample halfway.
  const mid = ramp[6];
  assert.ok(close(mid[0], 17) && close(mid[1], (Math.max(FLOOR, 40) + 100) / 2), String(mid));
  assert.equal(p.defined, 19);
  assert.equal(tail(p).pts[1][1], 100);
  assert.equal(tail(p).fade, true);
  scenarios++;
}

// 4. Blooming: fill at 100 % up to 12 s (dashed: 1.5 g ends it), pump off
//    for 30 s (solid, an explicit 0 %), then the flow phase: its power is
//    the controller's, an open range over the full height to the open end.
{
  const p = profilePreview(byLabel["Blooming"], FLOOR);
  assert.equal(p.segments.length, 3);
  const [fill, bloom, flow] = p.segments;
  assert.equal(fill.type, "line");
  assert.equal(fill.dashed, true);
  assert.deepEqual(fill.pts, [[0, 100], [0, 100], [12, 100]]);
  assert.equal(bloom.dashed, false);
  assert.deepEqual(bloom.pts, [[12, 100], [12, 0], [42, 0]]);
  assert.equal(flow.type, "open");
  assert.equal(flow.fade, true);
  assert.equal(flow.t0, 42);
  assert.equal(p.defined, 42);
  scenarios++;
}

// 5. Allongé: 100 % up to 12 s (dashed), then the flow phase as an open
//    range — not a 100 % line like Standard.
{
  const p = profilePreview(byLabel["Allongé"], FLOOR);
  assert.deepEqual(p.segments[0].pts, [[0, 100], [0, 100], [12, 100]]);
  assert.equal(p.segments[0].dashed, true);
  assert.equal(p.defined, 12);
  assert.equal(tail(p).type, "open");
  assert.equal(tail(p).t0, 12);
  const std = profilePreview(byLabel["Standard"], FLOOR);
  assert.notDeepEqual(p.segments.map((x) => x.type), std.segments.map((x) => x.type));
  scenarios++;
}

// 6. Hebel-Annäherung: 100 % up to 20 s (dashed), 8 s at 100 % (solid),
//    a 20 s linear decline to 60 %, held as the open end.
{
  const p = profilePreview(byLabel["Hebel-Annäherung"], FLOOR);
  const [fill, peak, decline] = p.segments;
  assert.equal(fill.dashed, true);
  assert.deepEqual(fill.pts[fill.pts.length - 1], [20, 100]);
  assert.equal(peak.dashed, false);
  assert.deepEqual(peak.pts[peak.pts.length - 1], [28, 100]);
  assert.equal(decline.dashed, false);
  assert.deepEqual(decline.pts[decline.pts.length - 1], [48, Math.max(FLOOR, 60)]);
  assert.equal(p.defined, 48);
  assert.equal(tail(p).pts[1][1], Math.max(FLOOR, 60));
  scenarios++;
}

// 7. Imported profile: eased transitions, a ratio exit (dashed), a short
//    last phase whose transition is longer than the phase (cut at its end,
//    like the engine), then full power as the open end; a floor of 0.
{
  const imp = DEFS.imported;
  const p = profilePreview(imp, 0);
  const [pre, press, out] = p.segments;
  // ease-in-out 100 -> 20 over 3 s: halfway at 1.5 s.
  const half = pre.pts.find((q) => close(q[0], 1.5));
  assert.ok(half && close(half[1], 60), String(half));
  assert.deepEqual(pre.pts[pre.pts.length - 1], [8, 20]);
  assert.equal(pre.dashed, false);
  assert.equal(press.dashed, true, "ratio target: may end before 10 s");
  // ease-out 20 -> 100 over 2 s: past halfway at 1 s (0.75).
  const q1 = press.pts.find((q) => close(q[0], 9));
  assert.ok(q1 && close(q1[1], 20 + 80 * 0.75), String(q1));
  // Last phase 15 s, linear over 30 s from 100 to 70: 85 % at its end.
  assert.ok(close(out.pts[out.pts.length - 1][0], 33));
  assert.ok(close(out.pts[out.pts.length - 1][1], 85), String(out.pts[out.pts.length - 1]));
  assert.equal(p.defined, 33);
  assert.deepEqual(tail(p).pts.slice(0, 2), [[33, 85], [33, 100]]);
  assert.equal(tail(p).dashed, false);
  // With the default floor the 20 % phase runs at the floor.
  const pf = profilePreview(imp, FLOOR);
  assert.equal(pf.segments[0].pts[pf.segments[0].pts.length - 1][1], Math.max(FLOOR, 20));
  scenarios++;
}

// 8. Uncertain length: a long non-last phase with a weight exit is drawn to
//    its cap, dashed; only the last phase's long cap is an open end.
{
  const def = {
    label: "Lang", throttle: null,
    phases: [
      { name: "A", duration: 90, power: 50, flow: null, transition: "instant", transition_duration: 0,
        targets: [{ type: "volumetric", operator: "gte", value: 3 }] },
      { name: "B", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    ],
  };
  const p = profilePreview(def, 0);
  assert.deepEqual(p.segments[0].pts, [[0, 100], [0, 50], [90, 50]]);
  assert.equal(p.segments[0].dashed, true);
  assert.equal(p.defined, 90);
  assert.ok(close(p.tMax, 90 + 22.5));
  scenarios++;
}

// 8b. A flow phase in the middle: open range; the next transition starts
//     from an unknown power (open until it ends), then a line; no exits:
//     the length is fixed.
{
  const def = {
    label: "Mitte", throttle: null,
    phases: [
      { name: "F", duration: 10, power: null, flow: 2.0, transition: "instant", transition_duration: 0, targets: [] },
      { name: "P", duration: 20, power: 60, flow: null, transition: "linear", transition_duration: 3, targets: [] },
    ],
  };
  const p = profilePreview(def, 0);
  assert.deepEqual(p.segments.map((x) => x.type), ["open", "open", "line", "line"]);
  assert.deepEqual([p.segments[0].t0, p.segments[0].t1], [0, 10]);
  assert.deepEqual([p.segments[1].t0, p.segments[1].t1], [10, 13]);
  assert.deepEqual(p.segments[2].pts, [[13, 60], [30, 60]]);
  assert.deepEqual(tail(p).pts.slice(0, 2), [[30, 60], [30, 100]]);
  assert.equal(p.defined, 30);
  scenarios++;
}

// ----------------------------------------------------------- the element

const SELECT = "select.scale_pump_profile";
const NUMBER = "number.scale_target_shot_time";
const FLOOR_ENTITY = "number.scale_throttle_floor";
const STANDARD = "number.scale_standard_pump_power";
const RATIO = "number.scale_target_ratio";

function makeHass({
  profile = "Blooming", numberState = "30", withNumber = true, entities,
  withStandard = true, standardState = "80", withRatio = false, ratioState = "2.8",
  pumpControllable = true, autoStopTarget = true,
} = {}) {
  const calls = [];
  const ws = [];
  const hass = {
    language: "en",
    entities: entities || {
      [SELECT]: { entity_id: SELECT, platform: "difluid_scale", translation_key: "pump_profile" },
    },
    states: {
      [SELECT]: {
        entity_id: SELECT, state: profile,
        attributes: {
          options: DEFS.builtins.map((p) => p.label), first_drop_deadline: null,
          pump_controllable: pumpControllable, auto_stop_target: autoStopTarget,
        },
      },
      [FLOOR_ENTITY]: { entity_id: FLOOR_ENTITY, state: String(FLOOR), attributes: {} },
    },
    callWS: async (msg) => {
      ws.push(msg);
      const def = DEFS.builtins.find((p) => p.label === msg.label || p.name === msg.label);
      if (!def) throw { code: "not_found" };
      return {
        entry_id: "e1", profile: def, floor: FLOOR,
        floor_entity: FLOOR_ENTITY,
        target_time_entity: withNumber ? NUMBER : null,
        standard_power_entity: withStandard ? STANDARD : null,
        ratio_entity: withRatio ? RATIO : null,
        pump_controllable: pumpControllable, auto_stop_target: autoStopTarget,
      };
    },
    callService: (domain, service, data) => calls.push([domain, service, data]),
    calls,
    ws,
  };
  if (withNumber) {
    hass.states[NUMBER] = {
      entity_id: NUMBER, state: numberState,
      attributes: { min: 15, max: 60, step: 1, unit_of_measurement: "s" },
    };
  }
  if (withStandard) {
    hass.states[STANDARD] = {
      entity_id: STANDARD, state: standardState,
      attributes: { min: FLOOR, max: 100, step: 1, unit_of_measurement: "%" },
    };
  }
  if (withRatio) {
    hass.states[RATIO] = {
      entity_id: RATIO, state: ratioState,
      attributes: { min: 0, max: 5, step: 0.1 },
    };
  }
  return hass;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

async function mount(hass) {
  const el = new Feature();
  el._width = 320;
  el.setConfig(Feature.getStubConfig());
  el.hass = hass;
  el.context = { entity_id: SELECT };
  await flush();
  return el;
}

// 9. Registration: a custom card feature, offered only for this
//    integration's profile select.
{
  const reg = window.customCardFeatures.find((f) => f.type === "difluid-profile-preview");
  assert.ok(reg, "registered");
  assert.equal(Feature.getStubConfig().type, "custom:difluid-profile-preview");
  const hass = makeHass();
  assert.equal(reg.isSupported(hass, { entity_id: SELECT }), true);
  assert.equal(isProfileSelect(hass, { entity_id: "select.other" }), false);
  const other = { ...hass, entities: { [SELECT]: { platform: "other", translation_key: "pump_profile" } } };
  assert.equal(isProfileSelect(other, { entity_id: SELECT }), false);
  const bean = { ...hass, entities: { [SELECT]: { platform: "difluid_scale", translation_key: "bean" } } };
  assert.equal(isProfileSelect(bean, { entity_id: SELECT }), false);
  // Without a translation key in the registry entry: the select's attribute.
  const noKey = { ...hass, entities: { [SELECT]: { platform: "difluid_scale" } } };
  assert.equal(isProfileSelect(noKey, { entity_id: SELECT }), true);
  assert.equal(isProfileSelect(hass, {}), false);
  scenarios++;
}

// 10. Graph of the selected profile: websocket by entity and label; only
//     theme colours (the pump colour token); the open range of a flow
//     phase; "100 %"; the length only when it is fixed.
{
  const hass = makeHass({ profile: "Blooming" });
  const el = await mount(hass);
  assert.deepEqual(hass.ws[0], { type: "difluid_scale/profile", entity_id: SELECT, label: "Blooming", language: "en" });
  const html = el.shadowRoot.innerHTML;
  assert.match(html, /<svg/);
  assert.match(html, /data-role="uncertain"/);
  assert.match(html, /data-role="fixed"/);
  assert.match(html, /<g data-role="open-end" mask="url\(#difluid-fade-mask\)">/);
  assert.match(html, /fill="url\(#difluid-hatch\)"/);
  assert.match(html, /stroke="var\(--difluid-pump-color, var\(--yellow-color, #fdd835\)\)"/);
  assert.doesNotMatch(html, /100 %/, "no axis labels");
  // The only text in the graph: the flow phase's target flow.
  assert.deepEqual([...html.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map((m) => m[1]), ["2.2 g/s"]);
  assert.doesNotMatch(html, /rgba?\(|"(white|black)"|: (white|black)\b/);
  assert.equal(el.style.display, "");
  // No refetch on an unrelated state change.
  el.hass = { ...hass, states: { ...hass.states, "sensor.x": { state: "1", attributes: {} } } };
  await flush();
  assert.equal(hass.ws.length, 1);
  // No length at the right any more (0.7.0b4), also for a fixed profile.
  const fixedDef = {
    label: "Fest", throttle: null,
    phases: [
      { name: "A", duration: 8, power: 60, flow: null, transition: "instant", transition_duration: 0, targets: [] },
      { name: "B", duration: 120, power: 100, flow: null, transition: "linear", transition_duration: 4, targets: [] },
    ],
  };
  const h2 = makeHass({ profile: "Fest" });
  h2.callWS = async (msg) => ({ profile: fixedDef, floor: 0, floor_entity: FLOOR_ENTITY, target_time_entity: NUMBER });
  const el2 = await mount(h2);
  assert.doesNotMatch(el2.shadowRoot.innerHTML, /data-role="defined"|12 s</);
  scenarios++;
}

// 11. Switching: Blooming -> Zielzeit shows the target shot time buttons
//     (value, unit), − / + call number.set_value (step, clamped); the sent
//     value shows until the entity changes; back to a graph on the next
//     profile change; no old graph while the new definition loads.
{
  const hass = makeHass({ profile: "Blooming", numberState: "30" });
  const el = await mount(hass);
  assert.match(el.shadowRoot.innerHTML, /<svg/);
  const zt = { ...hass, states: { ...hass.states, [SELECT]: { ...hass.states[SELECT], state: "Zielzeit" } } };
  el.hass = zt;
  // While loading: a placeholder of the same height, not the old graph.
  assert.match(el.shadowRoot.innerHTML, /data-role="loading"/);
  assert.doesNotMatch(el.shadowRoot.innerHTML, /<svg/);
  assert.equal(el.style.display, "");
  await flush();
  assert.equal(hass.ws[hass.ws.length - 1].label, "Zielzeit");
  let html = el.shadowRoot.innerHTML;
  assert.doesNotMatch(html, /<path d="M[\d.]+,[\d.]+L/, "no graph");
  assert.match(html, /data-role="value">30 s</);
  assert.match(html, /data-step="-1" data-target="main" aria-label="Decrease target shot time"/);
  assert.match(html, /data-step="1" data-target="main" aria-label="Increase target shot time"/);
  el._step(1);
  assert.deepEqual(hass.calls[0], ["number", "set_value", { entity_id: NUMBER, value: 31 }]);
  assert.match(el.shadowRoot.innerHTML, /data-role="value">31 s</, "shows the sent value");
  // The entity reports it: a new state object.
  const at31 = { ...zt, states: { ...zt.states, [NUMBER]: { ...zt.states[NUMBER], state: "31" } } };
  el.hass = at31;
  assert.equal(el._pending[NUMBER], undefined);
  el._step(-1);
  assert.deepEqual(hass.calls[1], ["number", "set_value", { entity_id: NUMBER, value: 30 }]);
  // At the maximum (set elsewhere): + is disabled and sends nothing.
  const top = { ...at31, states: { ...at31.states, [NUMBER]: { ...at31.states[NUMBER], state: "60" } } };
  el.hass = top;
  html = el.shadowRoot.innerHTML;
  assert.match(html, /data-role="value">60 s</);
  assert.match(html, /data-step="1" data-target="main" aria-label="Increase target shot time" disabled/);
  el._step(1);
  assert.equal(hass.calls.length, 2);
  // Back to a graph.
  el.hass = { ...top, states: { ...top.states, [SELECT]: { ...top.states[SELECT], state: "Hebel-Annäherung" } } };
  await flush();
  assert.match(el.shadowRoot.innerHTML, /data-role="fixed"/);
  assert.doesNotMatch(el.shadowRoot.innerHTML, /data-step/);
  scenarios++;
}

// 12. − / + edge cases (W1): a change from elsewhere while a value is
//     pending drops it (the new state shows, the next step starts there);
//     a failing set_value drops it too.
{
  const hass = makeHass({ profile: "Zielzeit", numberState: "30" });
  const el = await mount(hass);
  el._step(1);
  assert.equal(el._pending[NUMBER].value, 31);
  // Someone else sets 40 before ours arrives.
  const other = { ...hass, states: { ...hass.states, [NUMBER]: { ...hass.states[NUMBER], state: "40" } } };
  el.hass = other;
  assert.equal(el._pending[NUMBER], undefined);
  assert.match(el.shadowRoot.innerHTML, /data-role="value">40 s</);
  el._step(1);
  assert.deepEqual(hass.calls[hass.calls.length - 1], ["number", "set_value", { entity_id: NUMBER, value: 41 }]);
  // A failing call: back to the entity's value.
  const failing = makeHass({ profile: "Zielzeit", numberState: "30" });
  failing.callService = () => Promise.reject(new Error("unavailable"));
  const ef = await mount(failing);
  ef._step(1);
  assert.match(ef.shadowRoot.innerHTML, /data-role="value">31 s</);
  await flush();
  assert.equal(ef._pending[NUMBER], undefined);
  assert.match(ef.shadowRoot.innerHTML, /data-role="value">30 s</);
  scenarios++;
}

// 13. Nothing without its entity: no target shot time number (not found in
//     the registry), the number not in the states, an unknown profile, a
//     context without an entity.
{
  const noNum = await mount(makeHass({ profile: "Zielzeit", withNumber: false }));
  assert.equal(noNum.shadowRoot.innerHTML, "");
  assert.equal(noNum.style.display, "none");
  const hass = makeHass({ profile: "Zielzeit" });
  delete hass.states[NUMBER];
  const gone = await mount(hass);
  assert.equal(gone.shadowRoot.innerHTML, "");
  const unknown = await mount(makeHass({ profile: "Gelöscht" }));
  assert.equal(unknown.shadowRoot.innerHTML, "");
  assert.equal(unknown.style.display, "none");
  const el = new Feature();
  el.setConfig(Feature.getStubConfig());
  el.hass = makeHass();
  assert.equal(el.shadowRoot.innerHTML || "", "");
  scenarios++;
}

// 14. Refetch when the floor changes (the powers are clamped to it), the
//     profile list changes, the profile is imported over (revision) or the
//     entity changes; not on the same key; German labels.
{
  const hass = makeHass({ profile: "Sanfte Vorbrühung" });
  hass.language = "de";
  const el = await mount(hass);
  const n = hass.ws.length;
  let h = { ...hass, states: { ...hass.states, [FLOOR_ENTITY]: { state: "55", attributes: {} } } };
  el.hass = h;
  await flush();
  assert.equal(hass.ws.length, n + 1);
  const st = h.states[SELECT];
  h = { ...h, states: { ...h.states,
    [SELECT]: { ...st, attributes: { ...st.attributes, options: [...st.attributes.options, "Neu"] } } } };
  el.hass = h;
  await flush();
  assert.equal(hass.ws.length, n + 2);
  const st2 = h.states[SELECT];
  h = { ...h, states: { ...h.states,
    [SELECT]: { ...st2, attributes: { ...st2.attributes, profile_revision: "abc123" } } } };
  el.hass = h;
  await flush();
  assert.equal(hass.ws.length, n + 3, "imported over: new revision");
  el.hass = h;
  await flush();
  assert.equal(hass.ws.length, n + 3, "same key: no refetch");
  // Another entity (a second scale's select) with the same state.
  const SEL2 = "select.scale2_pump_profile";
  h = { ...h, states: { ...h.states, [SEL2]: { ...h.states[SELECT], entity_id: SEL2 } } };
  el.hass = h;
  el.context = { entity_id: SEL2 };
  await flush();
  assert.equal(hass.ws[hass.ws.length - 1].entity_id, SEL2);
  const zt = makeHass({ profile: "Zielzeit" });
  zt.language = "de";
  const de = await mount(zt);
  assert.match(de.shadowRoot.innerHTML, /aria-label="Zielzeit verringern"/);
  assert.match(de.shadowRoot.innerHTML, /aria-label="Zielzeit erhöhen"/);
  scenarios++;
}

// 15. A failed fetch is retried on a later update, after a pause that
//     grows (no loop on every update); answers in swapped order: the newer
//     request wins (token).
{
  const hass = makeHass({ profile: "Blooming" });
  let fail = true;
  const real = hass.callWS;
  hass.callWS = async (msg) => {
    if (fail) {
      hass.ws.push(msg);
      throw new Error("not loaded");
    }
    return real(msg);
  };
  const el = await mount(hass);
  assert.equal(hass.ws.length, 1);
  assert.equal(el.shadowRoot.innerHTML || "", "");
  const first = el._retryAt;
  assert.ok(first > Date.now() + 5000, "first pause ~10 s");
  el.hass = { ...hass };
  await flush();
  assert.equal(hass.ws.length, 1, "no retry before the pause");
  el._retryAt = Date.now() - 1; // the pause has passed
  el.hass = { ...hass };
  await flush();
  assert.equal(hass.ws.length, 2, "retried");
  assert.ok(el._retryAt - Date.now() > 15000, "the pause grows");
  fail = false;
  el._retryAt = Date.now() - 1;
  el.hass = { ...hass };
  await flush();
  assert.match(el.shadowRoot.innerHTML, /<svg/);
  assert.equal(el._retryAt, null);

  // Swapped answers: Blooming asked first, answers last.
  const h2 = makeHass({ profile: "Blooming" });
  const waiting = [];
  h2.callWS = (msg) => new Promise((resolve) => waiting.push({ msg, resolve }));
  const el2 = new Feature();
  el2._width = 320;
  el2.setConfig(Feature.getStubConfig());
  el2.hass = h2;
  el2.context = { entity_id: SELECT };
  const lever = { ...h2, states: { ...h2.states, [SELECT]: { ...h2.states[SELECT], state: "Hebel-Annäherung" } } };
  el2.hass = lever;
  assert.equal(waiting.length, 2);
  const answer = (w) => w.resolve({
    profile: DEFS.builtins.find((x) => x.name === w.msg.label), floor: FLOOR,
    floor_entity: FLOOR_ENTITY, target_time_entity: NUMBER,
  });
  answer(waiting[1]); // Hebel first
  await flush();
  answer(waiting[0]); // the stale Blooming answer
  await flush();
  assert.equal(el2._def.profile.label, "lever");
  assert.doesNotMatch(el2.shadowRoot.innerHTML, /open-end/, "not Blooming's open flow range");
  scenarios++;
}

// 16. Phase sections (0.7.0b4): each phase under its section (including
//     its own transition: the engine runs a phase's transition from that
//     phase's start), the open last phase to the end; the return to 100 %
//     after a short last phase has no section; the end criteria (targets,
//     duration; none for the open last phase's cap).
{
  const secs = (label) => profilePreview(byLabel[label], FLOOR).phases
    .map((ph) => [ph.name, ph.t0, Math.round(ph.t1 * 100) / 100, ph.duration]);
  const tMax = (label) => profilePreview(byLabel[label], FLOOR).tMax;
  assert.deepEqual(secs("Standard"), [["Bezug", 0, tMax("Standard"), null]]);
  assert.deepEqual(secs("Sanfte Vorbrühung"),
    [["Vorbrühung", 0, 15, 15], ["Bezug", 15, tMax("Sanfte Vorbrühung"), null]]);
  assert.deepEqual(secs("Blooming"),
    [["Füllen", 0, 12, 12], ["Blooming", 12, 42, 30], ["Fluss", 42, tMax("Blooming"), null]]);
  assert.deepEqual(secs("Allongé"), [["Füllen", 0, 12, 12], ["Fluss", 12, tMax("Allongé"), null]]);
  assert.deepEqual(secs("Hebel-Annäherung"),
    [["Füllen", 0, 20, 20], ["Spitze", 20, 28, 8], ["Abfall", 28, tMax("Hebel-Annäherung"), null]]);
  assert.equal(profilePreview(byLabel["Zielzeit"], FLOOR).phases, undefined);
  const imp = profilePreview(DEFS.imported, 0);
  assert.deepEqual(imp.phases.map((ph) => [ph.name, ph.t0, ph.t1]),
    [["Pre", 0, 8], ["Druck", 8, 18], ["Ausklang", 18, 33]]);
  assert.ok(imp.tMax > 33, "the return to 100 % follows, unnamed");
  const noName = profilePreview({ label: "x", phases: [
    { duration: 5, power: 50, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    { name: " ", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0, targets: [] },
  ] }, 0);
  assert.deepEqual(noName.phases.map((ph) => ph.name), ["Phase 1", "Phase 2"]);
  scenarios++;
}

// 17. End criteria chips over the graph (0.7.0b4): at every phase end what
//     ends it (exits in their order, then the duration), at the right end
//     the shot's end ("Target weight", after an open last phase's own
//     exits); centred on the border, kept inside the width, never on top
//     of each other; every built-in profile, an imported one, exits of
//     every kind and direction; names under the graph; flow target text.
{
  const chipsOf = (html) => [...html.matchAll(
    /<span class="chip" data-role="chip" style="left: ([\d.]+)px; width: ([\d.]+)px">([^<]*)<\/span>/g,
  )].map((m) => ({ left: Number(m[1]), width: Number(m[2]), text: m[3] }));
  const namesOf = (html) => [...html.matchAll(
    /<span class="phase" data-role="phase-label" style="left: ([\d.]+)px; width: ([\d.]+)px">([^<]*)<\/span>/g,
  )].map((m) => ({ left: Number(m[1]), width: Number(m[2]), name: m[3] }));
  const render = async (def, width = 340, language = "en") => {
    const h = makeHass({ profile: def.label });
    h.language = language;
    h.callWS = async () => ({ profile: def, floor: FLOOR, floor_entity: FLOOR_ENTITY, target_time_entity: NUMBER });
    const el = new Feature();
    el._width = width;
    el.setConfig(Feature.getStubConfig());
    el.hass = h;
    el.context = { entity_id: SELECT };
    await flush();
    return el.shadowRoot.innerHTML;
  };
  const texts = async (def, width = 340, language = "en") => chipsOf(await render(def, width, language)).map((c) => c.text);
  const inside = (chips, W) => {
    for (const c of chips) assert.ok(c.left >= 0 && c.left + c.width <= W + 1e-6, JSON.stringify(c));
    for (let i = 1; i < chips.length; i++) {
      assert.ok(chips[i - 1].left + chips[i - 1].width < chips[i].left, "never on top of each other");
    }
  };
  // Every built-in profile with a graph: the full texts, wide.
  assert.deepEqual(await texts(byLabel["Sanfte Vorbrühung"], 1200), ["2 g / 15 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Blooming"], 1200), ["1.5 g / 12 s", "30 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Allongé"], 1200), ["1.5 g / 12 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Hebel-Annäherung"], 1200), ["4 g / 20 s", "8 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Blooming"], 1200, "de"), ["1,5 g / 12 s", "30 s", "Zielgewicht"]);
  // At 360 px (a phone card).
  assert.deepEqual(await texts(byLabel["Sanfte Vorbrühung"], 360), ["2 g / 15 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Blooming"], 360), ["1.5 g / 12 s", "30 s"], "the end chip goes first");
  assert.deepEqual(await texts(byLabel["Allongé"], 360), ["1.5 g / 12 s", "Target weight"]);
  assert.deepEqual(await texts(byLabel["Hebel-Annäherung"], 360), ["4 g / 20 s", "8 s", "Target weight"]);
  // 360/260/200/150 px, every profile: only complete chips (never a part
  // of one), inside, apart, at most half their width from their border;
  // a chip between phases only goes after the end chip went.
  for (const W of [360, 260, 200, 150]) {
    for (const label of ["Sanfte Vorbrühung", "Blooming", "Allongé", "Hebel-Annäherung"]) {
      const full = await texts(byLabel[label], 1200);
      const chips = chipsOf(await render(byLabel[label], W));
      inside(chips, W);
      const p = profilePreview(byLabel[label], FLOOR);
      const borders = [...p.phases.filter((ph) => !ph.open).map((ph) => ph.t1), p.tMax]
        .map((t) => 4 + (t / p.tMax) * (W - 8));
      for (const c of chips) {
        const k = full.indexOf(c.text);
        assert.ok(k >= 0, `${W} ${label}: "${c.text}" is complete`);
        assert.ok(Math.abs(c.left + c.width / 2 - borders[k]) <= c.width / 2 + 0.1, `${W} ${label}: near its border`);
      }
      const texts2 = chips.map((c) => c.text);
      if (texts2.length < full.length - 1) assert.ok(!texts2.includes(full[full.length - 1]), `${W} ${label}: end chip first`);
    }
  }
  // Positions: centred on the border where there is room.
  const bl = chipsOf(await render(byLabel["Blooming"], 1200));
  const pb = profilePreview(byLabel["Blooming"], FLOOR);
  const xs = (p, t) => 4 + (t / p.tMax) * (340 - 8);
  const xw = (p, t) => 4 + (t / p.tMax) * (1200 - 8);
  assert.ok(close(bl[0].left + bl[0].width / 2, xw(pb, 12), 0.1));
  assert.ok(close(bl[1].left + bl[1].width / 2, xw(pb, 42), 0.1));
  assert.ok(close(bl[2].left + bl[2].width, 1200, 0.1), "clamped at the right edge");
  // No auto-stop target: no end chip (the shot ends by hand).
  const noTarget = async (def) => {
    const h = makeHass({ profile: def.label, autoStopTarget: false });
    h.callWS = async () => ({ profile: def, floor: FLOOR, floor_entity: FLOOR_ENTITY, auto_stop_target: false });
    const el = new Feature();
    el._width = 1200;
    el.setConfig(Feature.getStubConfig());
    el.hass = h;
    el.context = { entity_id: SELECT };
    await flush();
    return chipsOf(el.shadowRoot.innerHTML).map((c) => c.text);
  };
  assert.deepEqual(await noTarget(byLabel["Blooming"]), ["1.5 g / 12 s", "30 s"]);
  // Imported: fixed phases with their duration, the ratio exit "1 : x".
  assert.deepEqual(await texts(DEFS.imported, 1200), ["8 s", "1 : 1 / 10 s", "15 s", "Target weight"]);
  // Exits of every kind and direction; an open last phase's own exit;
  // flow values with the decimals they have (2.25, not 2.3).
  const exits = { label: "Exits", throttle: null, phases: [
    { name: "A", duration: 25, power: 70, flow: null, transition: "instant", transition_duration: 0,
      targets: [{ type: "flow", operator: "gte", value: 1.5 }, { type: "volumetric", operator: "gte", value: 36 }] },
    { name: "B", duration: 20, power: null, flow: 2.25, transition: "instant", transition_duration: 0,
      targets: [{ type: "flow", operator: "lte", value: 0.8 }, { type: "ratio", operator: "lte", value: 2.5 }] },
    { name: "C", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0,
      targets: [{ type: "volumetric", operator: "lte", value: 12.25 }, { type: "flow", operator: "gte", value: 2 }] },
  ] };
  assert.deepEqual(await texts(exits, 1400),
    ["≥ 1.5 g/s / 36 g / 25 s", "≤ 0.8 g/s / ≤ 1 : 2.5 / 20 s", "≤ 12.25 g / ≥ 2 g/s / Target weight"]);
  assert.deepEqual((await texts(exits, 1400, "de"))[0], "≥ 1,5 g/s / 36 g / 25 s");
  assert.match(await render(exits, 1400), /data-role="flow-target">2.25 g\/s</);

  // Helper: clamping, moving apart, leaving out whole (never shortened).
  const { layoutChips } = Card.math;
  const one = layoutChips([{ x: 0, parts: ["12 s"] }], 300)[0];
  assert.equal(one.left, 0, "clamped at the left edge");
  const right = layoutChips([{ x: 300, parts: ["12 s"] }], 300)[0];
  assert.ok(close(right.left + right.width, 300));
  const apart = layoutChips([{ x: 100, parts: ["1.5 g", "12 s"] }, { x: 140, parts: ["30 s"] }], 300);
  assert.equal(apart.length, 2);
  assert.ok(close(apart[0].left + apart[0].width + 3, apart[1].left), "moved apart, sharing the move");
  assert.ok(apart[0].left + apart[0].width / 2 < 100 && apart[1].left + apart[1].width / 2 > 140);
  const endFirst = layoutChips([{ x: 100, parts: ["1.5 g", "12 s"] }, { x: 110, parts: ["30 s"] },
    { x: 115, parts: ["Target weight"], end: true }], 300);
  assert.deepEqual(endFirst.map((c) => c.text), ["1.5 g / 12 s"]);
  const whole = layoutChips([{ x: 50, parts: ["aaaaaaaaaa"] }, { x: 60, parts: ["bb", "cc"] }], 150);
  assert.deepEqual(whole.map((c) => c.text), ["aaaaaaaaaa"], "the later chip left out whole");
  assert.deepEqual(layoutChips([{ x: 50, parts: ["a".repeat(40)] }], 100), [], "wider than the feature");

  // Names under the graph: only the name, centred, no overlap.
  const bh = await render(byLabel["Blooming"]);
  const nm = namesOf(bh);
  assert.deepEqual(nm.map((n) => n.name), ["Füllen", "Blooming", "Fluss"]);
  assert.ok(close(nm[1].left, xs(pb, 12) + 2, 0.06) && close(nm[1].left + nm[1].width, xs(pb, 42) - 2, 0.11));
  for (let i = 1; i < nm.length; i++) assert.ok(nm[i - 1].left + nm[i - 1].width < nm[i].left);
  assert.doesNotMatch(bh, /class="crit"/, "no criteria row under the names");
  assert.equal([...bh.matchAll(/data-role="phase-tick"/g)].length, 2);
  // Flow phases: the target flow in the middle of the hatched area, on a
  // halo of the card colour; the name row shows the name only. Left out
  // (not cut) when the area is too narrow.
  const flowAt = (html) => [...html.matchAll(/<text x="([\d.]+)"[^>]*data-role="flow-target">([^<]*)<\/text>/g)]
    .map((m) => [Number(m[1]), m[2]]);
  const bf = flowAt(await render(byLabel["Blooming"], 340, "de"));
  assert.deepEqual(bf.map((f) => f[1]), ["2,2 g/s"]);
  const seg = pb.segments[pb.segments.length - 1];
  assert.ok(close(bf[0][0], (xs(pb, seg.t0) + xs(pb, seg.t1)) / 2, 0.1), "centred in the area");
  assert.deepEqual(flowAt(await render(byLabel["Allongé"], 340, "de")).map((f) => f[1]), ["4,5 g/s"]);
  assert.deepEqual(namesOf(await render(byLabel["Allongé"], 340)).map((n) => n.name), ["Füllen", "Fluss"]);
  const flowImp = { label: "Fluss-Import", throttle: null, phases: [
    { name: "Vor", duration: 8, power: 60, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    { name: "Durchfluss", duration: 20, power: null, flow: 1.8, transition: "linear", transition_duration: 3,
      targets: [{ type: "volumetric", operator: "gte", value: 30 }] },
    { name: "Ende", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0, targets: [] },
  ] };
  const fi = await render(flowImp, 600);
  assert.deepEqual(flowAt(fi).map((f) => f[1]), ["1.8 g/s"]);
  assert.deepEqual(namesOf(fi).map((n) => n.name), ["Vor", "Durchfluss", "Ende"]);
  assert.doesNotMatch(await render(byLabel["Blooming"], 140), /data-role="flow-target"/, "too narrow: left out");
  assert.match(bh, /\.graph svg text \{[^}]*paint-order: stroke;[^}]*stroke: var\(--card-background-color/);
  // Too narrow: a 1 s phase gets no name.
  const narrow = { label: "Eng", throttle: null, phases: [
    { name: "Lang", duration: 30, power: 60, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    { name: "Mini", duration: 1, power: 80, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    { name: "Ende", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0, targets: [] },
  ] };
  assert.deepEqual(namesOf(await render(narrow)).map((n) => n.name), ["Lang", "Ende"]);
  // Escaped (imported names and labels are user data).
  const evil = { label: "Böse", throttle: null, phases: [
    { name: "<img src=x onerror=alert(1)>", duration: 10, power: 60, flow: null, transition: "instant", transition_duration: 0, targets: [] },
    { name: "x", duration: 120, power: 100, flow: null, transition: "instant", transition_duration: 0, targets: [] },
  ] };
  const ev = await render(evil);
  assert.doesNotMatch(ev, /<img/);
  assert.match(ev, /&lt;img src=x onerror=alert\(1\)&gt;/);
  // Chip look: theme tokens only.
  assert.match(bh, /\.chip \{[^}]*height: 14px;[^}]*border-radius: 7px;[^}]*background: color-mix\(in srgb, var\(--primary-text-color\) 9%, transparent\);[^}]*color: var\(--secondary-text-color\);[^}]*font-size: 10px;[^}]*white-space: nowrap;/);
  scenarios++;
}

// 18. Standard (0.7.0b4): − value + for the Standard pump power instead of
//     a graph; without a pump power entity greyed out at a fixed 100 %,
//     buttons disabled, nothing sent; operable again when the select's
//     attribute says a pump is set up (options changed, no refetch).
{
  const hass = makeHass({ profile: "Standard", standardState: "80" });
  const el = await mount(hass);
  let html = el.shadowRoot.innerHTML;
  assert.match(html, /class="mode-standard"/);
  assert.doesNotMatch(html, /<svg width/, "no graph");
  assert.doesNotMatch(html, /data-role="chip"/);
  assert.match(html, /data-role="value">80 %</);
  el._step(-1);
  assert.deepEqual(hass.calls[0], ["number", "set_value", { entity_id: STANDARD, value: 79 }]);
  // Without a pump entity: fixed 100 %, locked.
  const noPump = makeHass({ profile: "Standard", standardState: "80", pumpControllable: false });
  const locked = await mount(noPump);
  html = locked.shadowRoot.innerHTML;
  assert.match(html, /class="number compact unavailable locked"/);
  assert.match(html, /data-role="value">100 %</);
  assert.match(html, /data-step="-1" data-target="main" aria-label="Decrease pump power" disabled aria-disabled="true"/);
  assert.match(html, /data-step="1" data-target="main" aria-label="Increase pump power" disabled aria-disabled="true"/);
  locked._step(-1);
  locked._step(1);
  assert.equal(noPump.calls.length, 0);
  assert.match(html, /\.number\.unavailable \{ color: var\(--disabled-text-color, var\(--disabled-color\)\); \}/);
  // A pump is configured afterwards: the attribute changes, no refetch.
  const n = noPump.ws.length;
  const st = noPump.states[SELECT];
  locked.hass = { ...noPump, states: { ...noPump.states,
    [SELECT]: { ...st, attributes: { ...st.attributes, pump_controllable: true } } } };
  await flush();
  assert.equal(noPump.ws.length, n);
  assert.match(locked.shadowRoot.innerHTML, /data-role="value">80 %</);
  assert.doesNotMatch(locked.shadowRoot.innerHTML, /aria-disabled/);
  locked._step(1);
  assert.deepEqual(noPump.calls[0], ["number", "set_value", { entity_id: STANDARD, value: 81 }]);
  // Not controlled (observe/off, no or unavailable pump): the integration
  // says pump_controllable false; the German labels name the quantity.
  const obs = makeHass({ profile: "Standard", pumpControllable: false });
  obs.language = "de";
  const eobs = await mount(obs);
  assert.match(eobs.shadowRoot.innerHTML, /data-role="value">100 %</);
  assert.match(eobs.shadowRoot.innerHTML, /aria-label="Pumpenleistung verringern" disabled aria-disabled="true"/);
  // No Standard number: nothing.
  const none = await mount(makeHass({ profile: "Standard", withStandard: false }));
  assert.equal(none.shadowRoot.innerHTML, "");
  scenarios++;
}

// 19. Brew ratio row under every view (graph, Zielzeit, Standard): label
//     and a compact − "1 : x" +; set_value with step/min/max; the sent
//     value until the entity changes; a failing call goes back; no row
//     without the entity; stable heights per view.
{
  for (const profile of ["Blooming", "Zielzeit", "Standard"]) {
    const h = makeHass({ profile, withRatio: true });
    const el = await mount(h);
    const html = el.shadowRoot.innerHTML;
    assert.match(html, /<div class="row" data-role="ratio-row">\s*<span class="row-label">Brew Ratio<\/span>/, profile);
    assert.match(html, /<div class="number compact" data-role="ratio">/);
    assert.match(html, /data-role="ratio">[\s\S]*data-role="value">1 : 2.8</);
  }
  const de = makeHass({ profile: "Blooming", withRatio: true });
  de.language = "de";
  assert.match((await mount(de)).shadowRoot.innerHTML, /data-role="value">1 : 2,8</);
  const h = makeHass({ profile: "Zielzeit", withRatio: true });
  const el = await mount(h);
  el._step(1, "ratio");
  assert.deepEqual(h.calls[0], ["number", "set_value", { entity_id: RATIO, value: 2.9 }]);
  assert.match(el.shadowRoot.innerHTML, /1 : 2.9</);
  assert.match(el.shadowRoot.innerHTML, /data-role="value">30 s</, "the main number untouched");
  // Changed elsewhere: the pending value goes.
  el.hass = { ...h, states: { ...h.states, [RATIO]: { ...h.states[RATIO], state: "2.5" } } };
  assert.match(el.shadowRoot.innerHTML, /1 : 2.5</);
  // At the minimum the − button is disabled.
  const atMin = makeHass({ profile: "Blooming", withRatio: true, ratioState: "0" });
  const em = await mount(atMin);
  assert.match(em.shadowRoot.innerHTML, /data-step="-1" data-target="ratio" aria-label="Decrease Brew Ratio" disabled/);
  assert.match(em.shadowRoot.innerHTML, /data-role="ratio">[\s\S]*data-role="value">off</, "0 = off");
  assert.match(em.shadowRoot.innerHTML, /aria-label="Increase Brew Ratio"/);
  const deOff = makeHass({ profile: "Blooming", withRatio: true, ratioState: "0" });
  deOff.language = "de";
  const eo = await mount(deOff);
  assert.match(eo.shadowRoot.innerHTML, /data-role="value">aus</);
  assert.match(eo.shadowRoot.innerHTML, /aria-label="Brew Ratio verringern"/);
  // Unavailable: "–".
  const un = makeHass({ profile: "Blooming", withRatio: true, ratioState: "unavailable" });
  assert.match((await mount(un)).shadowRoot.innerHTML, /data-role="ratio">[\s\S]*data-role="value">–</);
  // Zielzeit or Standard without their number: the ratio row alone.
  for (const [profile, opts] of [["Zielzeit", { withNumber: false }], ["Standard", { withStandard: false }]]) {
    const e = await mount(makeHass({ profile, withRatio: true, ...opts }));
    assert.match(e.shadowRoot.innerHTML, /class="mode-none"/);
    assert.match(e.shadowRoot.innerHTML, /data-role="ratio-row"/);
    assert.doesNotMatch(e.shadowRoot.innerHTML, /data-target="main"/);
    assert.equal(e._heightOf(e._view()), 4 + 32);
    e._step(1, "main"); // nothing to step
  }
  // Disabled buttons show the not-allowed cursor (no pointer-events: none).
  assert.match(em.shadowRoot.innerHTML, /\.button\[disabled\] \{ opacity: 0\.4; cursor: not-allowed; \}/);
  // A failing call: back.
  const f = makeHass({ profile: "Blooming", withRatio: true });
  f.callService = () => Promise.reject(new Error("no"));
  const ef = await mount(f);
  ef._step(-1, "ratio");
  assert.match(ef.shadowRoot.innerHTML, /1 : 2.7</);
  await flush();
  assert.match(ef.shadowRoot.innerHTML, /1 : 2.8</);
  // No ratio number: no row.
  const no = await mount(makeHass({ profile: "Blooming" }));
  assert.doesNotMatch(no.shadowRoot.innerHTML, /data-role="ratio-row"/);
  // Heights stable per view, whatever the profile.
  const hOf = async (profile, withRatio) => {
    const e = await mount(makeHass({ profile, withRatio }));
    return e._heightOf(e._view());
  };
  const graphs = await Promise.all(["Sanfte Vorbrühung", "Blooming", "Allongé", "Hebel-Annäherung"].map((p) => hOf(p, true)));
  assert.ok(graphs.every((v) => v === graphs[0]), String(graphs));
  assert.equal(await hOf("Zielzeit", true), await hOf("Standard", true));
  assert.equal((await hOf("Blooming", true)) - (await hOf("Blooming", false)), 16 + 32);
  scenarios++;
}

// 21. 0.7.0b5: labelled rows for the target shot time and the Standard pump
//     power (like the ratio row), the label greyed out with a locked input;
//     spacing and heights; the frontend's language in the request; phase
//     names as the integration sends them.
{
  const zt = makeHass({ profile: "Zielzeit", withRatio: true });
  const ez = await mount(zt);
  let html = ez.shadowRoot.innerHTML;
  assert.match(html, /<div class="row" data-role="main-row">\s*<span class="row-label">Target time<\/span>\s*<div class="number compact" data-role="main">/);
  assert.match(html, /<div class="row" data-role="ratio-row">/);
  const st = makeHass({ profile: "Standard" });
  st.language = "de";
  html = (await mount(st)).shadowRoot.innerHTML;
  assert.match(html, /<span class="row-label">Pumpenleistung<\/span>/);
  const locked = makeHass({ profile: "Standard", pumpControllable: false });
  html = (await mount(locked)).shadowRoot.innerHTML;
  assert.match(html, /<div class="row locked" data-role="main-row">\s*<span class="row-label">Pump power<\/span>/);
  assert.match(html, /\.row\.locked \.row-label \{ color: var\(--disabled-text-color, var\(--disabled-color\)\); \}/);
  // Spacing: 4 px over the tile's feature gap, 16 px between the blocks.
  assert.match(html, /:host \{ display: block; width: 100%; padding-top: 4px; box-sizing: border-box; \}/);
  assert.match(html, /\.graph \+ \.row, \.row \+ \.row \{ margin-top: 16px; \}/);
  assert.match(html, /\.row \{ display: flex; align-items: center; justify-content: flex-end; gap: 10px; height: 32px; min-width: 0; \}/);
  // Heights (px): graph 4 + 66 (+ 16 + 32), Zielzeit/Standard 4 + 32 (+ 16 + 32).
  const hOf = async (profile, withRatio) => {
    const e = await mount(makeHass({ profile, withRatio }));
    return e._heightOf(e._view());
  };
  assert.equal(await hOf("Blooming", false), 70);
  assert.equal(await hOf("Blooming", true), 118);
  assert.equal(await hOf("Zielzeit", false), 36);
  assert.equal(await hOf("Zielzeit", true), 84);
  assert.equal(await hOf("Standard", true), 84);
  // The request names the frontend's language (locale first).
  const de = makeHass({ profile: "Blooming" });
  de.locale = { language: "de" };
  await mount(de);
  assert.equal(de.ws[0].language, "de");
  // Phase names come from the integration (translated there).
  const enDef = { ...byLabel["Blooming"], phases: byLabel["Blooming"].phases.map((ph, i) => ({ ...ph, name: ["Fill", "Bloom", "Flow"][i] })) };
  const h = makeHass({ profile: "Blooming" });
  h.callWS = async () => ({ profile: enDef, floor: FLOOR, floor_entity: FLOOR_ENTITY, auto_stop_target: true });
  const ee = new Feature();
  ee._width = 600;
  ee.setConfig(Feature.getStubConfig());
  ee.hass = h;
  ee.context = { entity_id: SELECT };
  await flush();
  assert.deepEqual([...ee.shadowRoot.innerHTML.matchAll(/data-role="phase-label"[^>]*>([^<]*)</g)].map((m) => m[1]),
    ["Fill", "Bloom", "Flow"]);
  scenarios++;
}

// 22. A language change refetches (the phase names come translated); a
//     labelled row never overflows a narrow feature: the label is cut with
//     an ellipsis, the input keeps its width.
{
  const hass = makeHass({ profile: "Blooming" });
  const el = await mount(hass);
  const n = hass.ws.length;
  el.hass = { ...hass, language: "de" };
  await flush();
  assert.equal(hass.ws.length, n + 1);
  assert.equal(hass.ws[hass.ws.length - 1].language, "de");
  el.hass = { ...hass, language: "de" };
  await flush();
  assert.equal(hass.ws.length, n + 1, "same language: no refetch");
  el.hass = { ...hass, language: "de", locale: { language: "en-GB" } };
  await flush();
  assert.equal(hass.ws[hass.ws.length - 1].language, "en-GB", "the user's profile language first");
  // 170 px feature: CSS keeps it inside (no layout engine here: the rules).
  const narrow = makeHass({ profile: "Standard", withRatio: true });
  const en = new Feature();
  en._width = 170;
  en.setConfig(Feature.getStubConfig());
  en.hass = narrow;
  en.context = { entity_id: SELECT };
  await flush();
  const html = en.shadowRoot.innerHTML;
  assert.match(html, /\.row \{[^}]*min-width: 0;/);
  assert.match(html, /\.row-label \{ flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;[^}]*white-space: nowrap; \}/);
  assert.match(html, /\.number\.compact \{ width: 156px; height: 32px; flex: none; \}/);
  assert.match(html, /<span class="row-label">Pump power<\/span>/);
  assert.match(html, /<span class="row-label">Brew Ratio<\/span>/);
  scenarios++;
}

console.log(`card profile preview: ${scenarios} scenarios OK`);
