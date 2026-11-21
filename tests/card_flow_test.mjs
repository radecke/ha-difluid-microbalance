// Node-only test of the brew card's flow-from-weight math and ready view
// (no browser). Run: node tests/card_flow_test.mjs   (also run by test_card.py)
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ------------------------------------------------------------ minimal DOM
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

const here = path.dirname(fileURLToPath(import.meta.url));
await import(
  path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js")
);
const Card = customElements.get("difluid-brew-card");
const { monotone, smoothedFlow, displayProfile, parseFlowSmoothing } = Card.math;

let scenarios = 0;
const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length;
const std = (a) => {
  const m = mean(a);
  return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / a.length);
};

// Real brew excerpt (t s, weight g): vibration makes the weight fall back
// after upward jumps (7.0 -> 3.8, 21.0 -> 20.4).
const REAL = `12.29 2.0, 12.53 4.2, 12.85 3.8, 15.22 7.0, 15.54 3.8, 15.84 6.2,
16.12 5.9, 16.44 5.6, 16.74 7.3, 17.02 8.1, 17.35 8.4, 17.65 8.8, 17.93 10.0,
18.24 10.3, 18.52 12.6, 18.83 12.0, 19.11 12.6, 19.44 13.0, 19.74 13.6,
20.04 14.9, 20.32 15.7, 20.63 15.3, 20.91 17.0, 21.22 17.6, 21.53 19.7,
21.84 21.0, 22.14 20.4, 22.43 21.0, 22.71 23.0, 23.04 23.3, 23.29 23.6,
23.54 24.4, 23.85 23.9, 24.15 25.0, 24.41 26.9, 24.74 27.9, 25.03 29.6,
25.31 31.0, 25.64 30.7, 25.95 31.4, 26.24 31.5, 26.54 33.0, 26.83 33.7,
27.14 34.8, 27.44 34.4, 27.75 36.8, 28.04 37.4, 28.34 38.2, 28.64 38.4,
28.91 38.5, 29.33 38.6`
  .split(",")
  .map((s) => s.trim().split(/\s+/).map(Number));
// Before 12.29 s the weight was 0.0-0.5 g (pre-infusion, nothing in the cup).
const PRE = [];
for (let t = 0; t < 12.2; t += 0.3) PRE.push([Math.round(t * 100) / 100, t < 9 ? 0.0 : 0.2, 0]);
const BREW = [...PRE, ...REAL.map(([t, w]) => [t, w, 0])];

// 1. Monotone weight: drops are ignored, the maximum is held.
{
  assert.deepEqual(monotone([]), []);
  assert.deepEqual(monotone([1, 2, 3]), [1, 2, 3]);
  assert.deepEqual(monotone([7.0, 3.8, 6.2, 7.3]), [7.0, 7.0, 7.0, 7.3]);
  assert.deepEqual(monotone([0, 4, 1, 1, 5]), [0, 4, 4, 4, 5]);
  scenarios++;
}

// 2. Real brew: monotone weight, flow >= 0, steady and plausible mid-brew.
{
  const pts = displayProfile(BREW, 3);
  assert.equal(pts.length, BREW.length);
  for (let i = 1; i < pts.length; i++) {
    assert.ok(pts[i][1] >= pts[i - 1][1] - 1e-9, `weight step at ${pts[i][0]} s`);
  }
  for (const p of pts) assert.ok(p[2] >= 0, `negative flow at ${p[0]} s`);
  const mid = pts.filter((p) => p[0] >= 18.5 && p[0] <= 28).map((p) => p[2]);
  assert.ok(mid.length > 20);
  const m = mean(mid);
  const sd = std(mid);
  assert.ok(m >= 2.4 && m <= 3.0, `mean flow ${m}`);
  assert.ok(sd < 0.45, `flow std dev ${sd}`);
  // No flow before the first drops.
  for (const p of pts) if (p[0] < 12) assert.equal(p[2], 0, `flow before start at ${p[0]} s`);
  scenarios++;
}

// 3. smoothedFlow details: < 3 points -> 0, constant slope recovered,
//    trailing window only, clamped at 0.
{
  assert.deepEqual(smoothedFlow([], [], 3), []);
  assert.deepEqual(smoothedFlow([0, 1], [0, 2], 3), [0, 0]);
  const ts = [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  const line = ts.map((t) => 1 + 2 * t);
  const f = smoothedFlow(ts, line, 1);
  // The first point is the initial weight itself: flow has not started yet.
  assert.equal(f[0], 0);
  // Flow starts once the weight is more than 1 g above the start (t > 0.5 s);
  // the trailing 1 s window then holds >= 3 points.
  assert.deepEqual(f.slice(0, 3), [0, 0, 0]);
  for (const v of f.slice(3)) assert.ok(Math.abs(v - 2) < 1e-9, `slope ${v}`);
  // window 0: only the point itself -> < 3 points -> 0
  assert.ok(smoothedFlow(ts, line, 0).every((v) => v === 0));
  const down = smoothedFlow([0, 1, 2, 3], [5, 4, 3, 2], 3);
  assert.ok(down.every((v) => v === 0), "never negative");
  // Causal: a point's flow does not change when later points arrive.
  const full = displayProfile(BREW, 3);
  const part = displayProfile(BREW.slice(0, 60), 3);
  part.forEach((p, i) => assert.deepEqual(p, full[i], `revised at ${p[0]} s`));
  scenarios++;
}

// 4. flow_smoothing option: default 3, 0 = raw points, range checked.
{
  assert.equal(parseFlowSmoothing(undefined), 3);
  assert.equal(parseFlowSmoothing(""), 3);
  assert.equal(parseFlowSmoothing(0), 0);
  assert.equal(parseFlowSmoothing(2.5), 2.5);
  assert.equal(parseFlowSmoothing("4"), 4);
  for (const bad of [-1, 11, "abc", true, NaN]) {
    assert.throws(() => parseFlowSmoothing(bad), /flow_smoothing must be a number of seconds from 0 to 10/);
  }
  assert.equal(displayProfile(BREW, 0), BREW, "0: the scale's raw values");
  const card = new Card();
  card._render = () => {};
  assert.throws(() => card.setConfig({ flow_smoothing: 20 }), /flow_smoothing/);
  scenarios++;
}

// ------------------------------------------------------------ card logic
function fakeEls() {
  const cls = () => ({ toggle() {}, add() {}, remove() {} });
  return {
    badgeText: {},
    badge: { classList: cls() },
    when: {},
    stats: { classList: cls(), innerHTML: "" },
  };
}

function makeCard(config = {}) {
  const card = new Card();
  card._render = () => {};
  card._renderChart = () => {};
  card._load = () => {};
  card.setConfig(config);
  card._els = fakeEls();
  card._chartBox = { clientWidth: 300 };
  card._width = 300;
  return card;
}

// 5. Ready view: empty live chart with the live start axes and zero stats;
//    a brew continues in the live view; ready off returns to the history.
{
  const card = makeCard();
  card._onLive({ type: "live_idle" });
  assert.equal(card._readyView(), false);
  assert.equal(card._chartBrew(), null, "history, nothing loaded");
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._readyView(), true);
  const brew = card._chartBrew();
  assert.deepEqual(brew, { profile: [], duration: null, live: true });
  const g = card._geometry(brew);
  assert.deepEqual([g.tMax, g.fMax, g.wMax], [30, 3, 40], "live start axes");
  card._renderLiveStats();
  assert.equal(card._els.when.textContent, "Ready – brew starts soon");
  const values = [...card._els.stats.innerHTML.matchAll(/class="value">([^<]*)/g)].map((m) => m[1]);
  assert.deepEqual(values, ["0.0", "0.0", "0.0", "–"], "time, flow, weight 0; no dose yet");

  // German label
  card._hass = { language: "de" };
  card._renderLiveStats();
  assert.equal(card._els.when.textContent, "Bereit – Bezug startet gleich");
  card._hass = null;

  // Brew starts: live view (ready stays on until the brew ends).
  card._onLive({ type: "live_start", start_ts: 0, dose: 18, points: [], weight: 0, flow: 0, elapsed: 0, phase: "brewing" });
  assert.ok(card._live && !card._readyView());
  const liveBrew = card._chartBrew();
  assert.ok(liveBrew.live);
  assert.deepEqual(
    [card._liveAxes.t.target, card._liveAxes.f.target, card._liveAxes.w.target],
    [30, 3, 40],
    "same axes as the ready view: seamless"
  );
  // Server order at the end: ready off, then live_end -> history.
  card._onLive({ type: "live_ready", ready: false });
  assert.ok(card._live, "a live brew is not interrupted");
  card._showNote = () => {};
  card._onLive({ type: "live_end", brew_id: null, reason: "too_short" });
  assert.equal(card._live, null);
  assert.equal(card._readyView(), false);

  // Ready off without a brew: back to the history view.
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._readyView(), true);
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(card._readyView(), false);
  scenarios++;
}

// 6. Live: stats and axis growth use the displayed values; a raw flow
//    spike from the scale does not reach the stat or the flow axis.
{
  const card = makeCard();
  const pts = BREW.filter((p) => p[0] <= 24).map((p) => [p[0], p[1], p[0] > 20 ? 9.5 : 0]);
  card._onLive({ type: "live_start", start_ts: 0, dose: 18, points: pts, weight: 24.4, flow: 9.5, elapsed: 24, phase: "brewing" });
  const disp = displayProfile(pts, 3);
  const last = disp[disp.length - 1];
  assert.deepEqual(card._liveNow(), { weight: last[1], flow: last[2] });
  assert.ok(last[2] < 4, `smoothed tip ${last[2]}`);
  assert.equal(card._liveAxes.f.target, 4, "flow axis from the displayed flow, not 9.5");
  card._renderLiveStats();
  const values = [...card._els.stats.innerHTML.matchAll(/class="value">([^<]*)/g)].map((m) => m[1]);
  assert.equal(values[1], last[2].toFixed(1));
  // New points: cache recomputed (array grows in place).
  const before = card._display(card._live.points);
  card._onLive({ type: "live_points", points: [[24.41, 26.9, 9.5]], weight: 26.9, flow: 9.5, elapsed: 24.4, phase: "brewing" });
  const after = card._display(card._live.points);
  assert.notEqual(after, before);
  assert.equal(after.length, before.length + 1);
  assert.equal(card._display(card._live.points), after, "cached while unchanged");

  // flow_smoothing 0: the scale's values as before.
  const raw = makeCard({ flow_smoothing: 0 });
  raw._onLive({ type: "live_start", start_ts: 0, dose: 18, points: pts, weight: 24.4, flow: 9.5, elapsed: 24, phase: "brewing" });
  assert.deepEqual(raw._liveNow(), { weight: 24.4, flow: 9.5 });
  assert.ok(raw._liveAxes.f.target >= 10);
  scenarios++;
}

// 7. Stored brew: chart and hover use the displayed points; replay keeps
//    the raw profile as its source.
{
  const card = makeCard();
  card._brews = [{ id: "b1" }];
  card._cache.set("b1", { id: "b1", profile: BREW, duration: 28 });
  const chart = card._chartBrew();
  assert.deepEqual(chart.profile, displayProfile(BREW, 3));
  assert.equal(card._currentBrew().profile, BREW, "raw profile for the replay");
  scenarios++;
}

console.log(`card flow: ${scenarios} scenarios OK`);
