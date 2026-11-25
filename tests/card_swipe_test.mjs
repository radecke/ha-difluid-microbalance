// Node-only test of the brew card's swipe between brews (0.7.0b7):
// direction, thresholds (a quick flick steps, a slow drag does not), the
// chart readout, the same locks as the buttons, the end of the list, no
// click after a swipe, reduced motion. Run: node tests/card_swipe_test.mjs
// (also run by test_card.py)
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

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
let reduced = false;
globalThis.matchMedia = (q) => ({ matches: reduced && q.includes("reduce") });
console.info = () => {};

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
await import(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"));
const Card = customElements.get("difluid-brew-card");
let scenarios = 0;

const PROFILE = [];
for (let i = 0; i <= 100; i++) {
  const t = i * 0.25;
  PROFILE.push([t, t < 4 ? 0 : Math.round((t - 4) * 1.7 * 10) / 10, t < 4 ? 0 : 1.7]);
}
const brew = (i) => ({ id: `b${i}`, start: `2026-11-2${i}T08:00:00+00:00`, dose: 18, yield: 36, ratio: 2, duration: 25 });

// Elements with a real class list (the locks read "hidden"/"replaying")
// and recorded animations.
function el() {
  const set = new Set();
  return {
    classList: {
      toggle(c, on) { if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); },
      add(c) { set.add(c); },
      remove(c) { set.delete(c); },
      contains(c) { return set.has(c); },
    },
    setAttribute() {},
    style: {},
    textContent: "",
    innerHTML: "",
    title: "",
    disabled: false,
    anims: [],
    animate(frames, opts) { this.anims.push({ frames, opts }); return { cancel() {} }; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 220 }),
  };
}

function makeCard(count = 3, index = 1) {
  const card = new Card();
  card._load = () => {};
  card._fetchBrew = async () => {};
  card._render = () => {};
  card.setConfig({});
  delete card._render;
  card._built = true;
  card._els = Object.fromEntries(
    ["title", "badge", "badgeText", "note", "when", "detail", "nav", "prev", "replay",
     "replayIcon", "next", "pos", "message", "stats", "legend", "del", "confirm",
     "confirmText", "confirmError", "confirmCancel", "confirmOk"].map((k) => [k, el()])
  );
  const box = Object.assign(node("div"), el());
  box.clientWidth = 400;
  card._chartBox = box;
  card._head = el();
  card._card = el();
  card._width = 400;
  card._chartH = 220;
  card._brews = Array.from({ length: count }, (_, i) => brew(i));
  for (const b of card._brews) card._cache.set(b.id, { ...b, profile: PROFILE });
  card._index = index;
  card._render();
  return card;
}

// A gesture: optional rest (finger down, still) for ``rest`` ms, then an
// even move by (dx, dy) in ``ms``; pointermoves every 8 ms (120 Hz) as the
// card and the chart's readout listener see them.
function gesture(card, { x0 = 200, y0 = 100, dx = 0, dy = 0, ms = 150, rest = 0, type = "touch", during }) {
  const base = 1000;
  card._swipeStart({ isPrimary: true, pointerType: type, button: 0, pointerId: 1, clientX: x0, clientY: y0, timeStamp: base });
  const move = (ev) => {
    card._swipeMove(ev);
    card._onPointer(ev);
  };
  for (let t = 8; t <= rest; t += 8) move({ pointerId: 1, clientX: x0, clientY: y0, timeStamp: base + t });
  if (during) during(card);
  for (let t = 8; t < ms; t += 8) {
    const k = t / ms;
    move({ pointerId: 1, clientX: x0 + dx * k, clientY: y0 + dy * k, timeStamp: base + rest + t });
  }
  return card._swipeEnd({ pointerId: 1, clientX: x0 + dx, clientY: y0 + dy, timeStamp: base + rest + ms });
}

// 1. Direction: finger to the right = older (index up, like "older"),
//    to the left = newer; the new brew slides in from the finger's side.
{
  const card = makeCard(3, 1);
  assert.equal(gesture(card, { dx: 120, ms: 100 }), 1);
  assert.equal(card._index, 2, "right = older");
  const slide = card._chartBox.anims.at(-1);
  assert.match(slide.frames[0].transform, /translateX\(-24px\)/, "comes in from the left");
  assert.equal(gesture(card, { dx: -120, ms: 100 }), -1);
  assert.equal(card._index, 1, "left = newer");
  assert.match(card._chartBox.anims.at(-1).frames[0].transform, /translateX\(24px\)/);
  scenarios++;
}

// 2. Thresholds (speed over the last 80 ms): a flick steps - also after a
//    short rest (B); a quick even readout drag (A), a slow drag, a short
//    flick, a long hold and a diagonal / vertical move do not.
{
  const cases = [
    [{ dx: 120, ms: 100 }, 1, "flick (1.2 px/ms)"],
    [{ dx: 70, ms: 80, rest: 100 }, 1, "B: rest 100 ms, then 70 px in 80 ms"],
    [{ dx: 200, ms: 300 }, null, "A: quick readout drag, 200 px in 300 ms"],
    [{ dx: 150, ms: 300 }, null, "A: 150 px in 300 ms"],
    [{ dx: 150, ms: 1200 }, null, "slow drag (0.13 px/ms)"],
    [{ dx: 40, ms: 30 }, null, "too short (40 px)"],
    [{ dx: 100, ms: 80, rest: 900 }, null, "a long hold first: readout"],
    [{ dx: 100, dy: 60, ms: 80 }, null, "too steep (|dx| < 2 |dy|)"],
    [{ dx: 10, dy: 200, ms: 100 }, null, "vertical (scroll)"],
  ];
  for (const [g, want, why] of cases) {
    const card = makeCard(3, 1);
    assert.equal(gesture(card, g), want, why);
    assert.equal(card._index, want === 1 ? 2 : 1, why);
  }
  scenarios++;
}

// 3. Readout: a slow drag on the chart shows the hover value and keeps it
//    (unchanged behaviour); a swipe leaves no readout behind, also when it
//    cannot step.
{
  let card = makeCard(3, 1);
  gesture(card, { x0: 60, dx: 150, ms: 1200 });
  assert.ok(card._hover, "slow drag: readout shown");
  card = makeCard(3, 1);
  gesture(card, { x0: 60, dx: 150, ms: 100 });
  assert.equal(card._hover, null, "swipe: no frozen readout");
  card = makeCard(3, 2); // oldest: cannot go older
  gesture(card, { x0: 60, dx: 150, ms: 100 });
  assert.equal(card._hover, null, "no step: readout cleared too");
  scenarios++;
}

// 4. Same locks as the buttons: live, ready view, replay, confirmation
//    open, message (nav hidden), disabled button.
{
  const locks = {
    live: (c) => { c._live = { points: [], phase: "brewing" }; },
    replay: (c) => { c._live = { replay: true, points: [] }; },
    ready: (c) => { c._readyView = () => true; },
    confirm: (c) => { c._confirm = { id: "b1", busy: false }; },
    message: (c) => { c._els.nav.classList.add("hidden"); },
    disabled: (c) => { c._els.prev.disabled = true; },
  };
  for (const [name, lock] of Object.entries(locks)) {
    const card = makeCard(3, 1);
    lock(card);
    assert.equal(card._navUsable(1), false, name);
    assert.notEqual(gesture(card, { dx: 120, ms: 100 }), 1, name);
    assert.equal(card._index, 1, name);
  }
  // The buttons take the same path.
  const card = makeCard(3, 1);
  card._confirm = { id: "b1", busy: false };
  assert.equal(card._navStep(1), false);
  scenarios++;
}

// 5. End of the list: no change, a small spring in the swipe's direction.
{
  const card = makeCard(3, 0); // newest
  card._chartBox.anims = [];
  assert.equal(gesture(card, { dx: -120, ms: 100 }), 0);
  assert.equal(card._index, 0);
  const anim = card._chartBox.anims.at(-1);
  assert.ok(anim, "spring");
  assert.equal(anim.frames[1].transform, "translateX(-10px)");
  assert.equal(anim.frames.at(-1).transform, "none");
  scenarios++;
}

// 6. No click after a swipe: the click its pointerup triggers (e.g. on
//    "delete") is stopped; a later click goes through.
{
  const card = makeCard(3, 1);
  gesture(card, { dx: 120, ms: 100 }); // ends at timeStamp 1150
  let stopped = 0;
  const click = (t) => ({ timeStamp: t, stopPropagation() { stopped++; }, preventDefault() {} });
  card._swipeClickGuard(click(1160));
  assert.equal(stopped, 1, "click from the swipe dropped");
  card._swipeClickGuard(click(1150 + 1000));
  assert.equal(stopped, 1, "a later click is a click");
  // A tap (no swipe) is never blocked.
  const tap = makeCard(3, 1);
  assert.equal(gesture(tap, { dx: 3, ms: 80 }), null);
  tap._swipeClickGuard(click(1100));
  assert.equal(stopped, 1);
  scenarios++;
}

// 7. Reduced motion: the step happens, no animation.
{
  reduced = true;
  const card = makeCard(3, 1);
  card._chartBox.anims = [];
  assert.equal(gesture(card, { dx: 120, ms: 100 }), 1);
  assert.equal(card._index, 2);
  assert.equal(card._chartBox.anims.length, 0);
  assert.equal(gesture(makeCard(3, 2), { dx: 120, ms: 100 }), 0);
  reduced = false;
  // While the fade-in after a collapse runs: the step, no slide on top.
  const busy = makeCard(3, 1);
  busy._anims.content = [{ playState: "running" }];
  busy._chartBox.anims = [];
  assert.equal(gesture(busy, { dx: 120, ms: 100 }), 1);
  assert.equal(busy._chartBox.anims.length, 0);
  scenarios++;
}

// 8. Touch and pen only: a mouse flick never switches (dragging reads
//    values / selects text); a pen does. A second finger during a running
//    swipe ends it (pinch); a vertical scroll (pointercancel) never ends
//    as a swipe.
{
  const card = makeCard(3, 1);
  assert.equal(gesture(card, { dx: 120, ms: 100, type: "mouse" }), null, "mouse: never");
  assert.equal(card._index, 1);
  assert.equal(gesture(card, { dx: 120, ms: 100, type: "pen" }), 1, "pen: yes");
  const two = makeCard(3, 1);
  const second = (c) => c._swipeStart({ isPrimary: false, pointerType: "touch", button: 0, pointerId: 2, clientX: 300, clientY: 100, timeStamp: 1050 });
  assert.equal(gesture(two, { dx: 120, ms: 100, rest: 50, during: second }), null, "second finger");
  assert.equal(two._index, 1);
  card._swipeStart({ isPrimary: true, pointerType: "touch", button: 0, pointerId: 3, clientX: 0, clientY: 0, timeStamp: 0 });
  card._swipe = null; // pointercancel (the browser took the scroll)
  assert.equal(card._swipeEnd({ pointerId: 3, clientX: 200, clientY: 0, timeStamp: 100 }), null);
  scenarios++;
}

// 9. The locks as the finger went down: a brew that starts (live) or a
//    confirmation opened during the swipe, or one that ends meanwhile -
//    the card does not jump.
{
  let card = makeCard(3, 1);
  const goLive = (c) => { c._live = { points: [], phase: "brewing" }; };
  assert.notEqual(gesture(card, { dx: 120, ms: 100, rest: 40, during: goLive }), 1);
  assert.equal(card._index, 1, "started live mid-swipe");
  card = makeCard(3, 1);
  card._live = { points: [], phase: "brewing" };
  const endLive = (c) => { c._live = null; };
  assert.notEqual(gesture(card, { dx: 120, ms: 100, rest: 40, during: endLive }), 1);
  assert.equal(card._index, 1, "live ended mid-swipe: no jump");
  // ... and no spring either (0.7.0b8): it was not the end of the list.
  card = makeCard(3, 1);
  card._live = { points: [], phase: "brewing" };
  card._chartBox.anims = [];
  gesture(card, { dx: 120, ms: 100, rest: 40, during: endLive });
  assert.equal(card._chartBox.anims.length, 0, "no spring after live ended");
  card = makeCard(3, 1);
  card._chartBox.anims = [];
  gesture(card, { dx: 120, ms: 100, rest: 40, during: goLive });
  assert.equal(card._chartBox.anims.length, 0, "no spring when live started");
  // At the end of the list the spring stays.
  card = makeCard(3, 2);
  card._chartBox.anims = [];
  assert.equal(gesture(card, { dx: 120, ms: 100 }), 0);
  assert.equal(card._chartBox.anims.length, 1, "spring at the oldest brew");
  scenarios++;
}

// 10. Wiring in _build: the listeners on the card (pointerdown, move, up,
//     cancel; the click guard in the capture phase).
{
  const card = makeCard(3, 1);
  const seen = [];
  card._wireSwipe({ addEventListener: (type, fn, capture) => seen.push([type, capture === true, fn]) });
  assert.deepEqual(seen.map(([t, c]) => [t, c]), [
    ["pointerdown", false], ["pointermove", false], ["pointerup", false],
    ["pointercancel", false], ["click", true],
  ]);
  const fire = (type, ev) => seen.find((x) => x[0] === type)[2](ev);
  fire("pointerdown", { isPrimary: true, pointerType: "touch", pointerId: 7, clientX: 0, clientY: 0, timeStamp: 0 });
  fire("pointercancel", {});
  assert.equal(card._swipe, null, "pointercancel ends the gesture");
  const src = await import("node:fs").then((fs) =>
    fs.readFileSync(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"), "utf8")
  );
  assert.match(src, /this\._wireSwipe\(this\._card\);/, "wired in _build");
  // iOS: the chart's svg (redrawn on every hover) is never the pointer
  // target, the stable .chart box is.
  assert.match(src, /\.chart svg \{[^}]*pointer-events: none;/);
  assert.match(src, /touch-action: pan-y pinch-zoom;/);
  scenarios++;
}

console.log(`card swipe: ${scenarios} scenarios OK`);
