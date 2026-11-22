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

// 8. Detail line of a stored brew: bean and shot metrics, missing parts
//    left out (brews stored before 0.5.0 have neither).
{
  const card = makeCard();
  const brew = {
    bean: "Kenya Kiambu",
    metrics: { first_drop: 8.8, peak_flow: 3.3, mean_flow: 2.8, flow_cv: 0.19 },
  };
  assert.equal(
    card._detailText(brew),
    "Kenya Kiambu · first drop 8.8 s · peak flow 3.3 g/s · Ø 2.8 g/s ±19 %"
  );
  assert.equal(
    card._detailText({ metrics: { first_drop: 12.3, peak_flow: null, mean_flow: 2.6, flow_cv: null } }),
    "first drop 12.3 s · Ø 2.6 g/s"
  );
  assert.equal(card._detailText({ bean: null }), "");
  assert.equal(card._detailText({}), "");
  card._hass = { language: "de" };
  assert.equal(
    card._detailText(brew),
    "Kenya Kiambu · erster Tropfen 8,8 s · Spitzenfluss 3,3 g/s · Ø 2,8 g/s ±19 %"
  );
  // The line keeps its space in the live view (visibility, not display):
  // the header height does not change when the brew ends.
  const src = await import("node:fs").then((fs) =>
    fs.readFileSync(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"), "utf8")
  );
  assert.match(src, /\.detail \{[^}]*min-height: 1\.3em/);
  assert.ok(!/detail\.classList\.(add|toggle)\("hidden"/.test(src), "detail never display:none");
  scenarios++;
}

// 9. Target line: live (from live_start/live_points), ready (live_ready),
//    stored (brew.target), auto-stop off (null) -> empty but reserved.
{
  const notes = (card) =>
    [...card._els.stats.innerHTML.matchAll(/class="stat-note">([^<]*)</g)].map((m) => m[1]);
  const card = makeCard();
  card._onLive({ type: "live_ready", ready: true, dose: 16, target: 40, target_source: "ratio" });
  card._renderLiveStats();
  assert.deepEqual(notes(card), ["", "", "target 40.0 g", ""], "ready: under the weight");
  card._onLive({ type: "live_start", start_ts: 0, dose: 16, points: [], weight: 0, flow: 0, elapsed: 0, phase: "brewing", target: 40, target_source: "ratio" });
  card._renderLiveStats();
  assert.deepEqual(notes(card), ["", "", "target 40.0 g", ""], "live");
  card._onLive({ type: "live_points", points: [], weight: 1, flow: 0, elapsed: 1, phase: "brewing", target: 41.6, target_source: "ratio" });
  card._renderLiveStats();
  assert.equal(notes(card)[2], "target 41.6 g", "follows a changed target");
  card._hass = { language: "de" };
  card._renderLiveStats();
  assert.equal(notes(card)[2], "Ziel 41,6 g");
  card._hass = null;
  // Auto-stop off: no text, the line is still there (no height jump).
  card._onLive({ type: "live_points", points: [], weight: 2, flow: 0, elapsed: 2, phase: "brewing", target: null, target_source: null });
  card._renderLiveStats();
  assert.deepEqual(notes(card), ["", "", "", ""]);
  // Stored brews: with and without (brews before 0.5.0b3) a target.
  assert.equal(card._targetText(38), "target 38.0 g");
  assert.equal(card._targetText(null), "");
  assert.equal(card._targetText(undefined), "");
  // Replay ticks carry no target: the stored brew's one stays.
  const replay = makeCard();
  replay._setLive({ replay: true, source: [[0, 0, 0]], target: 38, points: [] });
  replay._applyLiveValues({ weight: 0, flow: 0, elapsed: 0, phase: "brewing", stop: null });
  replay._renderLiveStats();
  assert.equal(notes(replay)[2], "target 38.0 g");
  // Stored view: the yield stat gets the target note (render source).
  const src = await import("node:fs").then((fs) =>
    fs.readFileSync(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"), "utf8")
  );
  assert.match(src, /stat\(this\._t\("yield"\)[^\n]*this\._targetText\(summary\.target\)/);
  assert.match(src, /\.stat-note \{[^}]*min-height: 1\.3em/);
  scenarios++;
}

// 10. Render check of the stored and the live view with the real render
//     code (fake elements): stat lines are not the grey note banner; the
//     stored ratio has two decimals; live stats unchanged.
{
  const cls = () => ({ toggle() {}, add() {}, remove() {} });
  const el = () => ({ classList: cls(), setAttribute() {}, style: {}, textContent: "", innerHTML: "", title: "" });
  const card = new Card();
  card._built = true;
  card._els = Object.fromEntries(
    ["title", "badge", "badgeText", "note", "when", "detail", "nav", "prev", "replay",
     "replayIcon", "next", "pos", "message", "stats", "legend"].map((k) => [k, el()])
  );
  card._chartBox = el();
  card._renderChart = () => {};
  card._renderLegend = () => {};
  card._load = () => {};
  card.setConfig({});
  card._brews = [{
    id: "b1", start: "2026-11-21T12:54:38+00:00", dose: 16.0, dose_source: "manual",
    yield: 40.4, ratio: 2.52, duration: 28.2, target: 40.0, target_source: "ratio",
  }];
  card._index = 0;
  card._render();
  const html = card._els.stats.innerHTML;
  const stats = [...html.matchAll(/class="value">([^<]*)(?:<span class="unit">([^<]*)<\/span>)?<\/div>\s*<div class="([^"]*)">([^<]*)</g)]
    .map((m) => ({ value: m[1], line: m[3], note: m[4] }));
  assert.deepEqual(stats.map((x) => x.value), ["16.0", "40.4", "1:2.52", "28.2"]);
  assert.ok(stats.every((x) => x.line === "stat-note"), "stat lines are not the banner");
  assert.ok(!/class="note"/.test(html), "no banner class inside the stats");
  assert.deepEqual(stats.map((x) => x.note), ["corrected", "target 40.0 g", "", ""]);
  // German: two decimals, comma.
  card._hass = { language: "de" };
  card._render();
  assert.match(card._els.stats.innerHTML, /1:2,52/);
  assert.match(card._els.stats.innerHTML, /Ziel 40,0 g/);
  card._hass = null;
  // An old brew without target: the line stays empty.
  card._brews = [{ id: "b0", start: "2026-11-20T08:00:00+00:00", dose: 18, yield: 36, ratio: 2, duration: 27 }];
  card._render();
  assert.match(card._els.stats.innerHTML, /1:2\.00/);
  assert.ok(!/target/.test(card._els.stats.innerHTML));
  // Live view: time, flow, weight, dose (no ratio there), target line.
  card._onLive({ type: "live_start", start_ts: 0, dose: 16, points: [], weight: 12.3, flow: 2.1, elapsed: 9, phase: "brewing", target: 40, target_source: "ratio" });
  card._render();
  const live = card._els.stats.innerHTML;
  const liveValues = [...live.matchAll(/class="value">([^<]*)/g)].map((m) => m[1]);
  assert.equal(liveValues.length, 4);
  assert.deepEqual(liveValues.slice(1), ["2.1", "12.3", "16.0"], "flow, weight, dose");
  assert.ok(!/class="note"/.test(live) && /class="stat-note">target 40\.0 g</.test(live));
  // The banner rule stays for the real note, the stat line has its own
  // rule without background or padding.
  const css = await import("node:fs").then((fs) =>
    fs.readFileSync(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"), "utf8")
  );
  const rule = css.match(/\.stat-note \{([^}]*)\}/)[1];
  assert.ok(!/background|padding|border-radius/.test(rule), rule);
  assert.match(css, /\n\s*\.note \{[^}]*background/);
  scenarios++;
}

// 11. Live time after an auto-stop cut: frozen at the cut while the
//     weight goes on; without a cut it runs on as before.
{
  const card = makeCard();
  card._onLive({ type: "live_start", start_ts: 0, dose: 16, points: [], weight: 30, flow: 2, elapsed: 20.5, phase: "brewing", stop: null, cut: null });
  card._live.recvAt = performance.now() - 500;
  assert.ok(card._liveElapsed() > 20.5, "runs on locally before the cut");
  card._onLive({ type: "live_points", points: [], weight: 37.1, flow: 1.5, elapsed: 21.0, phase: "brewing", stop: null, cut: 21.0 });
  card._live.recvAt = performance.now() - 900;
  assert.equal(card._liveElapsed(), 21.0, "frozen at the cut");
  card._onLive({ type: "live_points", points: [], weight: 38.4, flow: 0.2, elapsed: 21.0, phase: "tail", stop: 27.5, cut: 21.0 });
  assert.equal(card._liveElapsed(), 21.0, "also in the tail (timer stop later)");
  card._renderLiveStats();
  const values = [...card._els.stats.innerHTML.matchAll(/class="value">([^<]*)/g)].map((m) => m[1]);
  assert.equal(values[0], "21.0");
  assert.equal(values[2], "38.4", "weighing goes on");
  scenarios++;
}

console.log(`card flow: ${scenarios} scenarios OK`);
