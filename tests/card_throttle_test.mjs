// Node-only test of the brew card's pump throttle view (0.6.0): power band
// as a step line, target shot time marker, grind-finer hint, shot time and
// first drop in shot time. Run: node tests/card_throttle_test.mjs
// (also run by test_card.py)
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

class FakeHTMLElement {
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

// Minimal SVG DOM: elements keep their attributes and children.
const node = (tag) => ({
  tag,
  attrs: {},
  children: [],
  textContent: "",
  setAttribute(k, v) {
    this.attrs[k] = v;
  },
  appendChild(c) {
    this.children.push(c);
    return c;
  },
});
globalThis.document = { createElementNS: (_ns, tag) => node(tag) };

const here = path.dirname(fileURLToPath(import.meta.url));
const CARD = path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js");
await import(CARD);
const Card = customElements.get("difluid-brew-card");
const { throttleView, powerSteps } = Card.math;
let scenarios = 0;

const PROFILE = [];
for (let i = 0; i <= 100; i++) {
  const t = i * 0.25;
  PROFILE.push([t, t < 4 ? 0 : Math.round((t - 4) * 1.7 * 10) / 10, t < 4 ? 0 : 1.7]);
}
const THROTTLE = {
  mode: "throttle",
  applied: true,
  triggered: true,
  grind_finer: true,
  unreachable: false,
  target_time: 30,
  power: [[0.1, 100], [7.5, 62], [10.6, 54], [24.9, 100]],
};
const BREW = {
  id: "b1",
  start: "2026-11-23T08:00:00+00:00",
  dose: 18,
  yield: 40.2,
  ratio: 2.23,
  duration: 24.9,
  relay_offset: 0.6,
  first_drop_shot: 5.1,
  shot_time: 25.5,
  since_last_relay_off: 320.0,
  metrics: { first_drop: 4.5, peak_flow: 2.0, mean_flow: 1.7, flow_cv: 0.1 },
  throttle: THROTTLE,
};

function walk(el, out = []) {
  out.push(el);
  for (const c of el.children || []) walk(c, out);
  return out;
}

function renderCard(setup) {
  const card = new Card();
  card._load = () => {};
  card._render = () => {};
  card.setConfig({});
  delete card._render; // the real render from here on
  card._built = true;
  const cls = () => ({ toggle() {}, add() {}, remove() {}, contains() { return false; } });
  const el = () => ({ classList: cls(), setAttribute() {}, style: {}, textContent: "", innerHTML: "", title: "" });
  card._els = Object.fromEntries(
    ["title", "badge", "badgeText", "note", "when", "detail", "nav", "prev", "replay",
     "replayIcon", "next", "pos", "message", "stats", "legend", "del", "confirm",
     "confirmText", "confirmError", "confirmCancel", "confirmOk"].map((k) => [k, el()])
  );
  const box = node("div");
  box.clientWidth = 400;
  box.classList = cls();
  card._chartBox = box;
  card._width = 400;
  card._chartH = 220;
  setup(card);
  card._render();
  const svg = box.children[0];
  return { card, svg, all: svg ? walk(svg) : [] };
}

// 1. Helpers: step line, view of a record, nothing for off/missing.
{
  assert.deepEqual(powerSteps([[0, 100], [5, 60], [8, 50]], 12), [
    [0, 100], [5, 100], [5, 60], [8, 60], [8, 50], [12, 50],
  ]);
  assert.deepEqual(powerSteps([], 10), []);
  const v = throttleView(THROTTLE, 0.6);
  assert.equal(v.targetT, 29.4);
  assert.equal(v.observe, false);
  assert.equal(v.grindFiner, true);
  assert.equal(throttleView({ ...THROTTLE, mode: "observe", applied: false }, 0.6).observe, true);
  assert.equal(throttleView(null, 0.6), null);
  assert.equal(throttleView({ mode: "off" }, 0.6), null);
  // Without a relay offset there is no target marker, but the trace stays.
  assert.equal(throttleView(THROTTLE, null).targetT, null);
  scenarios++;
}

// 2. Stored brew: power band with a step path below the plot, target
//    marker at 30 - 0.6 s, time stat = shot time with "grind finer", first
//    drop in shot time, legend key; only theme colours.
{
  const { card, all } = renderCard((c) => {
    c._brews = [BREW];
    c._index = 0;
    c._cache.set("b1", { ...BREW, profile: PROFILE });
  });
  const power = all.find((e) => e.attrs["data-role"] === "pump-power");
  const target = all.find((e) => e.attrs["data-role"] === "target-time");
  assert.ok(power, "power step line drawn");
  assert.ok(target, "target time marker drawn");
  assert.equal(power.attrs["stroke-dasharray"], "none");
  const g = card._geometry(card._chartBrew());
  assert.equal(g.band, 26, "band reserved under the plot");
  assert.ok(Math.abs(Number(target.attrs.x1) - g.x(29.4)) < 1e-6);
  // Every point of the path lies inside the band (below the plot).
  const ys = [...power.attrs.d.matchAll(/[ML][\d.]+,([\d.]+)/g)].map((m) => Number(m[1]));
  const plotBottom = 10 + g.plotH;
  assert.ok(ys.every((y) => y > plotBottom && y <= 220 - 24), ys.join(","));
  // 100 % at the band top, the lowest power lower in the band.
  assert.ok(Math.min(...ys) < Math.max(...ys));
  const stats = card._els.stats.innerHTML;
  assert.match(stats, /class="value">25\.5<span class="unit">s/, "shot time");
  assert.match(stats, /class="stat-note">grind finer</);
  assert.match(card._els.detail.innerHTML, /first drop 5\.1 s/);
  assert.match(card._els.legend.innerHTML, /Pump \(%\)/);
  for (const e of all) {
    for (const k of ["stroke", "fill"]) {
      const val = e.attrs[k];
      if (val === undefined || val === "none") continue;
      assert.match(val, /^var\(--/, `${k}=${val} must be a theme variable`);
    }
  }
  scenarios++;
}

// 3. Observe mode: dashed (would-be) line; old brew without throttle: no
//    band, duration as before, first drop from the scale timer.
{
  const obs = { ...BREW, throttle: { ...THROTTLE, mode: "observe", applied: false, pump: true } };
  const { all } = renderCard((c) => {
    c._brews = [obs];
    c._index = 0;
    c._cache.set("b1", { ...obs, profile: PROFILE });
  });
  assert.equal(all.find((e) => e.attrs["data-role"] === "pump-power").attrs["stroke-dasharray"], "3 3");
  const old = { id: "b0", start: BREW.start, dose: 18, yield: 36, ratio: 2, duration: 27, metrics: { first_drop: 6.2 } };
  const r = renderCard((c) => {
    c._brews = [old];
    c._index = 0;
    c._cache.set("b0", { ...old, profile: PROFILE });
  });
  assert.equal(r.all.find((e) => e.attrs["data-role"] === "pump-power"), undefined);
  assert.equal(r.card._geometry(r.card._chartBrew()).band, 0);
  assert.match(r.card._els.stats.innerHTML, /class="value">27\.0<span/);
  assert.match(r.card._els.detail.innerHTML, /first drop 6\.2 s/);
  assert.ok(!/Pump/.test(r.card._els.legend.innerHTML));
  scenarios++;
}

// 4. Live: the throttle values of live messages draw the band and the
//    marker; the time runs in shot time (elapsed + relay offset).
{
  const { card, all } = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 50),
      weight: 14, flow: 1.7, elapsed: 12.4, phase: "brewing", target: 40,
      throttle: { relay_offset: 0.6, mode: "throttle", applied: true, target_time: 30,
        triggered: true, grind_finer: true, power: [[0.1, 100], [7.5, 62]] },
    });
    c._live.recvAt = performance.now();
  });
  assert.ok(all.find((e) => e.attrs["data-role"] === "pump-power"));
  assert.ok(all.find((e) => e.attrs["data-role"] === "target-time"));
  card._renderLiveStats();
  const first = card._els.stats.innerHTML.match(/class="value">([\d.]+)/)[1];
  assert.ok(Number(first) >= 13.0 && Number(first) <= 14.1, first);
  assert.match(card._els.stats.innerHTML, /grind finer/);
  // A message without throttle keeps the last one (points only).
  card._onLive({ type: "live_points", points: [], weight: 15, flow: 1.7, elapsed: 13, phase: "brewing" });
  assert.ok(card._live.throttle);
  scenarios++;
}

// 5. German strings, no fixed colours anywhere in the card source.
{
  const src = fs.readFileSync(CARD, "utf8");
  assert.ok(!/(stroke|fill|color)\s*[:=]\s*["']?#[0-9a-fA-F]{3,8}/.test(src), "no hex colours");
  assert.ok(!/rgba?\(/.test(src), "no rgb() colours");
  const { card } = renderCard((c) => {
    c._hass = { language: "de" };
    c._brews = [BREW];
    c._index = 0;
    c._cache.set("b1", { ...BREW, profile: PROFILE });
  });
  assert.match(card._els.stats.innerHTML, /feiner mahlen/);
  assert.match(card._els.legend.innerHTML, /Pumpe \(%\)/);
  scenarios++;
}

// 6. A shot that ended well before its target time: the axis reaches the
//    target marker (stored and live).
{
  const short = { ...BREW, duration: 18.0, shot_time: 18.6 };
  const prof = PROFILE.filter((p) => p[0] <= 18.0);
  const { card, all } = renderCard((c) => {
    c._brews = [short];
    c._index = 0;
    c._cache.set("b1", { ...short, profile: prof });
  });
  const g = card._geometry(card._chartBrew());
  assert.ok(g.tMax >= 30.4, `axis to the target: ${g.tMax}`);
  assert.ok(all.find((e) => e.attrs["data-role"] === "target-time"));
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 20),
      weight: 3, flow: 1, elapsed: 5, phase: "brewing",
      throttle: { relay_offset: 0.6, mode: "observe", target_time: 30, power: [[0.1, 100]] },
    });
  });
  assert.ok(live.card._liveAxes.t.target >= 30, live.card._liveAxes.t.target);
  scenarios++;
}

// 7. Unreachable: the reason follows "grind finer" (en, de).
{
  const un = { ...BREW, throttle: { ...THROTTLE, unreachable: true, unreachable_reason: "floor" } };
  const { card } = renderCard((c) => {
    c._brews = [un];
    c._index = 0;
    c._cache.set("b1", { ...un, profile: PROFILE });
  });
  assert.match(card._els.stats.innerHTML, /grind finer · floor reached/);
  const de = renderCard((c) => {
    c._hass = { language: "de" };
    const r = { ...BREW, throttle: { ...THROTTLE, unreachable: true, unreachable_reason: "rate_limit" } };
    c._brews = [r];
    c._index = 0;
    c._cache.set("b1", { ...r, profile: PROFILE });
  });
  assert.match(de.card._els.stats.innerHTML, /feiner mahlen · Korrekturen begrenzt/);
  const capped = renderCard((c) => {
    const r = { ...BREW, throttle: { ...THROTTLE, unreachable: true, unreachable_reason: "trigger_cap" } };
    c._brews = [r];
    c._index = 0;
    c._cache.set("b1", { ...r, profile: PROFILE });
  });
  assert.match(capped.card._els.stats.innerHTML, /pump not learned – enable learning/);
  scenarios++;
}

// 8. Pump profile (0.7.0): a profile shot's power (bloom to 0 %, ramp,
//    flow phase) in the same band, the profile's name in the legend
//    (escaped), no target time marker; observed = dashed; Zielzeit keeps
//    its marker and names its profile too.
{
  const rec = {
    mode: "throttle", applied: true, active: false, reason: "profile",
    target_time: null, profile: "Blooming <b>",
    power: [[-0.6, 100], [6.4, 0], [36.4, 40], [37.4, 60], [38.4, 80], [41.4, 100], [45, 88]],
    phases: [[-0.6, 0, "Füllen", null], [6.4, 1, "Blooming", "volumetric"], [36.4, 2, "Fluss", "duration"]],
  };
  const brew = { ...BREW, duration: 55, throttle: rec };
  const { card, all } = renderCard((c) => {
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  const power = all.find((e) => e.attrs["data-role"] === "pump-power");
  assert.ok(power, "profile power drawn");
  assert.equal(power.attrs["stroke-dasharray"], "none");
  assert.equal(all.find((e) => e.attrs["data-role"] === "target-time"), undefined);
  assert.match(card._els.legend.innerHTML, /Pump \(%\) · <span class="profile-name">Blooming &lt;b&gt;<\/span>/);
  // 0 % lies on the band's bottom line.
  const g = card._geometry(card._chartBrew());
  const ys = [...power.attrs.d.matchAll(/[ML][\d.]+,([\d.]+)/g)].map((m) => Number(m[1]));
  const bottom = 10 + g.plotH + 6 + (g.band - 6 - 2); // PAD.top, BAND_GAP
  assert.ok(ys.some((y) => Math.abs(y - bottom) < 0.6), `${ys} / ${bottom}`);
  const view = throttleView({ ...rec, mode: "observe", applied: false, pump: true }, 0.6);
  assert.equal(view.observe, true);
  assert.equal(view.targetT, null);
  assert.equal(view.profile, "Blooming <b>");
  const zt = throttleView({ ...THROTTLE, profile: "Zielzeit" }, 0.6);
  assert.equal(zt.targetT, 29.4);
  assert.equal(zt.profile, "Zielzeit");
  // Live: the profile's name follows the live throttle info.
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 20),
      weight: 3, flow: 1, elapsed: 5, phase: "brewing",
      throttle: { relay_offset: 0.6, mode: "observe", applied: false, target_time: null, pump: true,
        profile: "Sanfte Vorbrühung", phase: "Vorbrühung", power: [[-0.6, 40]] },
    });
    c._live.recvAt = performance.now();
  });
  // A record from before 0.7.0b5 (old German label): the built-in's name
  // in the frontend's language.
  assert.match(live.card._els.legend.innerHTML, /Soft preinfusion/);
  assert.equal(live.all.find((e) => e.attrs["data-role"] === "pump-power").attrs["stroke-dasharray"], "3 3");
  scenarios++;
}

// 8b. Built-in profile names (0.7.0b5): by id or old label, en/de; imported
//     labels as they are; the same names as the integration's select
//     state translations.
{
  const { profileName, BUILTIN_PROFILES } = Card.math;
  assert.equal(profileName("soft_preinfusion", "en"), "Soft preinfusion");
  assert.equal(profileName("soft_preinfusion", "de"), "Sanfte Vorbrühung");
  assert.equal(profileName("Sanfte Vorbrühung", "en"), "Soft preinfusion");
  assert.equal(profileName("Zielzeit", "en"), "Target time");
  assert.equal(profileName("Hebel-Annäherung", "de"), "Hebel-Annäherung");
  assert.equal(profileName("LEVER", "en"), "Lever approximation");
  assert.equal(profileName("Mein Profil", "de"), "Mein Profil");
  for (const [language, file] of [["en", "en.json"], ["de", "de.json"]]) {
    const tr = JSON.parse(fs.readFileSync(path.join(here, "../custom_components/difluid_scale/translations", file), "utf8"));
    const states = tr.entity.select.pump_profile.state;
    assert.deepEqual(Object.keys(states).sort(), Object.keys(BUILTIN_PROFILES).sort());
    for (const [id, names] of Object.entries(BUILTIN_PROFILES)) assert.equal(states[id], names[language], `${language} ${id}`);
  }
  // Stored brew (id) in German: the legend and the skipped note.
  const de = renderCard((c) => {
    c._hass = { language: "de" };
    const brew = { ...BREW, throttle: { mode: "throttle", applied: false, active: false, reason: "profile",
      target_time: null, profile: "soft_preinfusion", profile_reason: "not_prepared" } };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  assert.match(de.card._els.stats.innerHTML, /Sanfte Vorbrühung übersprungen/);
  scenarios++;
}

// 9. A pump profile skipped because the brew was not prepared ("Prepare
//    brew" off at the relay start): named under the time (escaped, en and
//    de), stored and live; no band without a trace.
{
  const rec = { mode: "throttle", applied: false, active: false, reason: "profile",
    target_time: null, profile: "Blooming <i>", profile_reason: "not_prepared" };
  const brew = { ...BREW, throttle: rec };
  const { card, all } = renderCard((c) => {
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  assert.match(card._els.stats.innerHTML, /Blooming &lt;i&gt; skipped \(not prepared\): full power/);
  assert.equal(all.find((e) => e.attrs["data-role"] === "pump-power"), undefined);
  const de = renderCard((c) => {
    c._hass = { language: "de" };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  assert.match(de.card._els.stats.innerHTML, /übersprungen \(nicht vorbereitet\): volle Leistung/);
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 20),
      weight: 3, flow: 1, elapsed: 5, phase: "brewing",
      throttle: { relay_offset: 0.6, ...rec },
    });
    c._live.recvAt = performance.now();
  });
  live.card._renderLiveStats();
  assert.match(live.card._els.stats.innerHTML, /skipped \(not prepared\)/);
  scenarios++;
}

// 10. Brews stored before 0.7.0b2 carry the old gate "no_tare": still named
//     as skipped, with what really happened (no tare); an unknown reason or
//     a reason without a profile name shows nothing.
{
  const rec = { mode: "throttle", applied: false, active: false, reason: "profile",
    target_time: null, profile: "Blooming", profile_reason: "no_tare" };
  const show = (throttle, lang = "en") => renderCard((c) => {
    if (lang !== "en") c._hass = { language: lang };
    const brew = { ...BREW, throttle };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  }).card._els.stats.innerHTML;
  assert.match(show(rec), /Blooming skipped \(no tare\): full power/);
  assert.match(show(rec, "de"), /Blooming übersprungen \(nicht tariert\): volle Leistung/);
  assert.doesNotMatch(show({ ...rec, profile_reason: "other" }), /skipped|übersprungen/);
  assert.doesNotMatch(show({ ...rec, profile: null, profile_reason: "not_prepared" }), /skipped/);
  scenarios++;
}

// 11. Legend on a narrow card (audit C4: overflowed by ~12 px at 390 px):
//     one row whose keys shrink with an ellipsis; the key text sits in its
//     own element so the ellipsis can apply. No layout engine here, so the
//     rules themselves are checked.
{
  const src = fs.readFileSync(
    path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"), "utf8");
  const rule = (sel) => {
    const m = src.match(new RegExp(`\\n\\s*${sel.replace(/[.]/g, "\\.")} \\{([^}]*)\\}`));
    assert.ok(m, `rule ${sel}`);
    return m[1];
  };
  assert.match(rule(".legend"), /min-width: 0;.*overflow: hidden;.*white-space: nowrap;/);
  assert.doesNotMatch(rule(".legend"), /flex-wrap/, "never wraps: the chart height counts the row");
  assert.match(rule(".legend .key"), /min-width: 0;/);
  assert.match(rule(".legend .key"), /flex: 0 1 auto;/);
  for (const sel of [".legend .key-text", ".legend .readout"]) {
    const r = rule(sel);
    for (const d of ["min-width: 0", "overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"]) {
      assert.ok(r.includes(d), `${sel}: ${d}`);
    }
  }
  assert.match(rule(".legend .swatch"), /flex: none;/);
  const { card } = renderCard((c) => {
    const brew = { ...BREW, throttle: THROTTLE };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  const keys = card._els.legend.innerHTML.match(/<span class="key [a-z]+">/g) || [];
  const texts = card._els.legend.innerHTML.match(/<span class="swatch"><\/span><span class="key-text">/g) || [];
  assert.ok(keys.length >= 2, "keys rendered");
  assert.equal(texts.length, keys.length, "every key's text is its own element");
  scenarios++;
}

// 12. Legend swatch follows the line style (0.7.0b3): observed = dashed key,
//     applied = solid key, stored and live alike; the line, its hover marker
//     and the key use the pump colour token, the target marker keeps the
//     accent colour.
{
  const show = (throttle) => renderCard((c) => {
    const brew = { ...BREW, throttle };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  const applied = show(THROTTLE);
  assert.match(applied.card._els.legend.innerHTML, /<span class="key pump">/);
  const observed = show({ ...THROTTLE, mode: "observe", applied: false, pump: true });
  assert.match(observed.card._els.legend.innerHTML, /<span class="key pump observe">/);
  const pumpColour = "var(--difluid-pump-color, var(--yellow-color, #fdd835))";
  assert.equal(applied.all.find((e) => e.attrs["data-role"] === "pump-power").attrs.stroke, pumpColour);
  assert.equal(
    applied.all.find((e) => e.attrs["data-role"] === "target-time").attrs.stroke,
    "var(--accent-color, var(--primary-text-color))"
  );
  const src = fs.readFileSync(CARD, "utf8");
  assert.match(src, /\.legend \.pump \.swatch \{ border-color: \$\{PUMP_COLOR\}; \}/);
  assert.match(src, /\.legend \.pump\.observe \.swatch \{ border-top-style: dashed; \}/);
  assert.equal((src.match(/#[0-9a-fA-F]{6}\b/g) || []).join(), "#fdd835", "the only hex: the token fallback");
  // Browsing from an observed brew to an applied one re-renders the key.
  const obs = { ...BREW, id: "b2", throttle: { ...THROTTLE, mode: "observe", applied: false, pump: true } };
  const r = renderCard((c) => {
    c._brews = [obs, BREW];
    c._index = 0;
    c._cache.set("b2", { ...obs, profile: PROFILE });
    c._cache.set("b1", { ...BREW, profile: PROFILE });
  });
  assert.match(r.card._els.legend.innerHTML, /key pump observe/);
  r.card._index = 1;
  r.card._render();
  assert.match(r.card._els.legend.innerHTML, /<span class="key pump">/);
  // Live: the key appears/changes with the live throttle info, without a
  // full render (next animation frame).
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 20),
      weight: 3, flow: 1, elapsed: 5, phase: "brewing",
    });
    c._live.recvAt = performance.now();
  });
  assert.doesNotMatch(live.card._els.legend.innerHTML, /key pump/);
  live.card._render = () => assert.fail("no full render for a live tick");
  live.card._onLive({
    type: "live_points", points: [], weight: 4, flow: 1, elapsed: 6, phase: "brewing",
    throttle: { relay_offset: 0.6, mode: "observe", applied: false, pump: true, target_time: 30,
      power: [[0.1, 100], [3, 80]] },
  });
  live.card._frame();
  assert.match(live.card._els.legend.innerHTML, /key pump observe/);
  live.card._onLive({
    type: "live_points", points: [], weight: 5, flow: 1, elapsed: 7, phase: "brewing",
    throttle: { relay_offset: 0.6, mode: "throttle", applied: true, pump: true, target_time: 30,
      power: [[0.1, 100], [3, 80]] },
  });
  live.card._frame();
  assert.match(live.card._els.legend.innerHTML, /<span class="key pump">/);
  scenarios++;
}

// 13. Hover: the pump power at the hovered time from the drawn step line
//     (at a change the new value), in profile time like the flow points
//     (the relay offset shifts nothing); a marker on the line in the band;
//     none beyond the drawn end (capture tail).
{
  const hoverAt = (brew, t, lang = "en") => {
    const r = renderCard((c) => {
      if (lang !== "en") c._hass = { language: lang };
      c._brews = [brew];
      c._index = 0;
      c._cache.set(brew.id, { ...brew, profile: PROFILE });
    });
    const p = r.card._chartBrew().profile.find((q) => Math.abs(q[0] - t) < 1e-9);
    assert.ok(p, `profile point at ${t}`);
    r.card._hover = p;
    r.card._renderChart();
    r.card._renderLegend();
    const svg = r.card._chartBox.children[r.card._chartBox.children.length - 1];
    return { ...r, all: walk(svg), g: r.card._geometry(r.card._chartBrew()) };
  };
  const pathPoints = (all) => {
    const d = all.find((e) => e.attrs["data-role"] === "pump-power").attrs.d;
    return [...d.matchAll(/[ML]([\d.]+),([\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])]);
  };
  // Boundary 7.5 s: 100 -> 62; just before it still 100.
  const at = hoverAt(BREW, 7.5);
  assert.match(at.card._els.legend.innerHTML, /7\.5 s · .* g · Pump 62 %/);
  const marker = at.all.find((e) => e.attrs["data-role"] === "pump-hover");
  assert.ok(marker, "marker on the pump line");
  assert.equal(marker.attrs.fill, "var(--difluid-pump-color, var(--yellow-color, #fdd835))");
  assert.ok(Math.abs(marker.attrs.cx - at.g.x(7.5)) < 1e-6);
  const onLine = pathPoints(at.all).filter((q) => Math.abs(q[0] - Number(at.g.x(7.5).toFixed(1))) < 0.06);
  assert.ok(onLine.some((q) => Math.abs(q[1] - marker.attrs.cy) < 0.06), `${onLine} / ${marker.attrs.cy}`);
  assert.ok(marker.attrs.cy > 10 + at.g.plotH, "in the band, below the plot");
  assert.match(hoverAt(BREW, 7.25).card._els.legend.innerHTML, / · Pump 100 %/);
  // Relay offset 3 s instead of 0.6 s: same value at the same chart time.
  const shifted = { ...BREW, id: "b3", relay_offset: 3.0, shot_time: 27.9 };
  assert.match(hoverAt(shifted, 7.5).card._els.legend.innerHTML, / · Pump 62 %/);
  assert.match(hoverAt(shifted, 10.5).card._els.legend.innerHTML, / · Pump 62 %/);
  assert.match(hoverAt(shifted, 10.75).card._els.legend.innerHTML, / · Pump 54 %/);
  // Beyond the timer stop (24.9 s) the line is not drawn: no value.
  const tail = hoverAt(BREW, 25);
  assert.doesNotMatch(tail.card._els.legend.innerHTML, /Pump/);
  assert.equal(tail.all.find((e) => e.attrs["data-role"] === "pump-hover"), undefined);
  // German label.
  assert.match(hoverAt(BREW, 7.5, "de").card._els.legend.innerHTML, / · Pumpe 62 %/);
  // Only observed (not set): no value and no marker; the dashed line stays.
  for (const th of [
    { ...THROTTLE, mode: "observe", applied: false, controlled: false, pump: true },
    { ...THROTTLE, mode: "observe", applied: false, pump: true }, // older record
  ]) {
    const obs = hoverAt({ ...BREW, throttle: th }, 7.5);
    assert.doesNotMatch(obs.card._els.legend.innerHTML, /Pump/);
    assert.equal(obs.all.find((e) => e.attrs["data-role"] === "pump-hover"), undefined);
    assert.equal(obs.all.find((e) => e.attrs["data-role"] === "pump-power").attrs["stroke-dasharray"], "3 3");
  }
  // Helper: the step value as drawn.
  const { stepAt } = Card.math;
  const pts = powerSteps([[0, 100], [5, 60]], 8);
  assert.equal(stepAt(pts, 4.99), 100);
  assert.equal(stepAt(pts, 5), 60);
  assert.equal(stepAt(pts, 8), 60);
  assert.equal(stepAt(pts, 8.5), null);
  assert.equal(stepAt([], 1), null);
  scenarios++;
}

// 14. No pump band without a pump (0.7.0b3): a record without a configured
//     pump power entity (pump: false) or an older one without the field
//     that only observed: no band, no key, no hover value; the target
//     marker stays in the plot. An older applied record proves a pump.
{
  const { hasPump } = Card.math;
  assert.equal(hasPump({ mode: "observe", pump: false, applied: false }), false);
  assert.equal(hasPump({ mode: "throttle", pump: true, applied: false }), true);
  assert.equal(hasPump({ mode: "observe", applied: false }), false);
  assert.equal(hasPump({ mode: "throttle", applied: true }), true);
  assert.equal(hasPump({ mode: "throttle", applied: true, pump: false }), false);
  const noPump = { ...THROTTLE, mode: "observe", applied: false, pump: false, power: [[0.22, 100.0]] };
  const legacy = { ...THROTTLE, mode: "observe", applied: false, power: [[0.22, 100.0]] };
  for (const throttle of [noPump, legacy]) {
    const brew = { ...BREW, throttle };
    const r = renderCard((c) => {
      c._brews = [brew];
      c._index = 0;
      c._cache.set("b1", { ...brew, profile: PROFILE });
    });
    assert.equal(r.all.find((e) => e.attrs["data-role"] === "pump-power"), undefined);
    assert.equal(r.card._geometry(r.card._chartBrew()).band, 0, "no band, full plot height");
    assert.doesNotMatch(r.card._els.legend.innerHTML, /Pump/);
    const target = r.all.find((e) => e.attrs["data-role"] === "target-time");
    assert.ok(target, "target marker stays");
    assert.equal(Number(target.attrs.y2), 10 + r.card._geometry(r.card._chartBrew()).plotH, "ends at the plot");
    r.card._hover = r.card._chartBrew().profile[30];
    r.card._renderChart();
    r.card._renderLegend();
    assert.doesNotMatch(r.card._els.legend.innerHTML, /Pump/);
    const svg = r.card._chartBox.children[r.card._chartBox.children.length - 1];
    assert.equal(walk(svg).find((e) => e.attrs["data-role"] === "pump-hover"), undefined);
  }
  // Without a target either: nothing at all.
  assert.equal(throttleView({ ...noPump, target_time: null }, 0.6), null);
  // Live without a pump: no band.
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 20),
      weight: 3, flow: 1, elapsed: 5, phase: "brewing",
      throttle: { relay_offset: 0.6, mode: "observe", applied: false, pump: false,
        target_time: null, power: [[0.22, 100]] },
    });
  });
  assert.equal(live.all.find((e) => e.attrs["data-role"] === "pump-power"), undefined);
  assert.doesNotMatch(live.card._els.legend.innerHTML, /Pump/);
  // With a pump (observed) the band is there.
  const withPump = renderCard((c) => {
    const brew = { ...BREW, throttle: { ...noPump, pump: true } };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  assert.ok(withPump.all.find((e) => e.attrs["data-role"] === "pump-power"));
  assert.equal(withPump.card._geometry(withPump.card._chartBrew()).band, 26);
  scenarios++;
}

// 15. Line style = control (0.7.0b3, "controlled"): a throttle-mode shot
//     that controlled the pump but never had to write it (applied false) is
//     solid, live and stored alike; not controlled is dashed. Older records
//     without the field keep the old rule (observe or not applied).
{
  const style = (throttle) => renderCard((c) => {
    const brew = { ...BREW, throttle };
    c._brews = [brew];
    c._index = 0;
    c._cache.set("b1", { ...brew, profile: PROFILE });
  });
  const dash = (r) => r.all.find((e) => e.attrs["data-role"] === "pump-power").attrs["stroke-dasharray"];
  const untouched = { ...THROTTLE, applied: false, controlled: true, pump: true };
  const stored = style(untouched);
  assert.equal(dash(stored), "none");
  assert.match(stored.card._els.legend.innerHTML, /<span class="key pump">/);
  const live = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 50),
      weight: 14, flow: 1.7, elapsed: 12.4, phase: "brewing",
      throttle: { relay_offset: 0.6, ...untouched },
    });
    c._live.recvAt = performance.now();
  });
  assert.equal(dash(live), "none", "same style live and stored");
  assert.equal(dash(style({ ...THROTTLE, mode: "throttle", applied: false, controlled: false, pump: true })), "3 3");
  // Older records: the old rule.
  assert.equal(dash(style({ ...THROTTLE, applied: false, pump: true })), "3 3");
  assert.equal(dash(style({ ...THROTTLE, applied: true })), "none");
  const { observedOnly } = Card.math;
  assert.equal(observedOnly({ mode: "observe", controlled: true }), false, "the field wins");
  assert.equal(observedOnly({ mode: "throttle", applied: true }), false);
  assert.equal(observedOnly({ mode: "observe" }), true);
  scenarios++;
}

// 16. Pump profile in the info line (0.7.0b6): always shown from
//     throttle.profile - stored and live, with or without a pump band -
//     localized, escaped, in a span of its own (ellipsis for long names);
//     nothing for records without a profile (no made-up "Standard").
{
  const visible = (card) => {
    // The header keeps its height: the line is only made (in)visible.
    const calls = [];
    card._els.detail.classList = {
      toggle(c, on) { calls.push([c, on]); },
      add(c) { calls.push([c, true]); },
      remove(c) { calls.push([c, false]); },
      contains() { return false; },
    };
    return calls;
  };
  const stored = (throttle, extra = {}) => {
    let calls;
    const r = renderCard((c) => {
      calls = visible(c);
      const brew = { ...BREW, ...extra, throttle };
      c._brews = [brew];
      c._index = 0;
      c._cache.set("b1", { ...brew, profile: PROFILE });
    });
    return { html: r.card._els.detail.innerHTML, calls, legend: r.card._els.legend.innerHTML };
  };
  // Built-in by id, with the pump band (throttle) and without (pump: false).
  let r = stored({ ...THROTTLE, profile: "allonge" }, { bean: "Pacandé" });
  assert.match(r.html, /^Pacandé · <span class="brew-profile" title="Allongé">Allongé<\/span> · first drop/);
  r = stored({ mode: "observe", applied: false, pump: false, profile: "standard", power: [[-1.69, 100]] });
  assert.match(r.html, /^<span class="brew-profile" title="Standard">Standard<\/span> · first drop/);
  // An imported profile with markup in its name: escaped, never HTML.
  r = stored({ ...THROTTLE, profile: '<b>Mein "Profil"</b>' });
  assert.match(r.html, /&lt;b&gt;Mein &quot;Profil&quot;&lt;\/b&gt;/);
  assert.ok(!r.html.includes("<b>"));
  // Older record without throttle.profile (or no throttle at all): nothing.
  for (const throttle of [{ ...THROTTLE }, null, { ...THROTTLE, profile: "" }]) {
    r = stored(throttle, { bean: "Pacandé" });
    assert.ok(!r.html.includes("brew-profile"), r.html);
    assert.ok(!/Standard/.test(r.html), r.html);
    assert.match(r.html, /^Pacandé · first drop/);
  }
  // Nothing at all in the line: invisible, space kept.
  r = stored(null, { bean: null, metrics: {}, first_drop_shot: null });
  assert.equal(r.html, "");
  assert.deepEqual(r.calls.at(-1), ["invisible", true]);
  // German name.
  const de = renderCard((c) => {
    c._hass = { language: "de", locale: { language: "de" } };
    c._brews = [{ ...BREW, throttle: { ...THROTTLE, profile: "soft_preinfusion" } }];
    c._index = 0;
    c._cache.set("b1", { ...BREW, profile: PROFILE });
  });
  assert.match(de.card._els.detail.innerHTML, /Sanfte Vorbrühung/);
  // Live: the running brew's profile, also without a pump band.
  let calls;
  const live = renderCard((c) => {
    calls = visible(c);
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: PROFILE.slice(0, 50),
      weight: 14, flow: 1.7, elapsed: 12.4, phase: "brewing",
      throttle: { relay_offset: 0.6, mode: "observe", pump: false, profile: "lever", power: [] },
    });
  });
  assert.match(live.card._els.detail.innerHTML, /<span class="brew-profile" title="Lever approximation">Lever approximation<\/span>/);
  assert.deepEqual(calls.at(-1), ["invisible", false]);
  // Live without a profile (older integration): the line stays empty.
  const bare = renderCard((c) => {
    calls = visible(c);
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: [], weight: 0, flow: 0,
      elapsed: 1, phase: "brewing", throttle: null,
    });
  });
  assert.equal(bare.card._els.detail.innerHTML, "");
  assert.deepEqual(calls.at(-1), ["invisible", true]);
  // Long names: cut by an ellipsis inside their own box, the line keeps
  // one fixed height (no jump), in every layout (same header everywhere).
  const css = fs.readFileSync(CARD, "utf8");
  assert.match(css, /\.detail \.brew-profile \{[^}]*max-width: 40%;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/);
  assert.match(css, /\.detail \{[^}]*height: 1\.3em;[^}]*white-space: nowrap;[^}]*text-overflow: ellipsis;/);
  // In the legend the name stays as well.
  const band = stored({ ...THROTTLE, profile: "allonge" });
  assert.match(band.legend, /profile-name">Allongé</);
  scenarios++;
}

// 17. Backlog (0.7.0b6): a live_points message with replace_from drops the
//     points from there and appends the new ones; without it, appends.
{
  const r = renderCard((c) => {
    c._onLive({
      type: "live_start", start_ts: 0, dose: 18, points: [[0, 0, 0], [0.25, 0, 0], [6.5, 0, 0], [15.6, 13.9, 2.1]],
      weight: 13.9, flow: 2.1, elapsed: 15.6, phase: "brewing",
    });
    c._onLive({ type: "live_points", points: [[15.85, 14.5, 2.1]], replace_from: null, weight: 14.5, flow: 2.1, elapsed: 15.9, phase: "brewing" });
    c._onLive({
      type: "live_points", replace_from: 7.25,
      points: [[7.29, 0, 0], [8.91, 3.3, 0], [15.48, 13.9, 2.1], [15.85, 14.5, 2.1]],
      weight: 14.5, flow: 2.1, elapsed: 16.0, phase: "brewing",
    });
    // An older integration (no field): appended as before.
    c._onLive({ type: "live_points", points: [[16.1, 15.0, 2.2]], weight: 15, flow: 2.2, elapsed: 16.1, phase: "brewing" });
  });
  assert.deepEqual(r.card._live.points.map((p) => p[0]), [0, 0.25, 6.5, 7.29, 8.91, 15.48, 15.85, 16.1]);
  scenarios++;
}

console.log(`card throttle: ${scenarios} scenarios OK`);
