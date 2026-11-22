// Node-only test of the brew card's expansion (expand_when_active): when it
// is active, edit mode / preview, animations cancelled mid-way, clean-up,
// reduced motion. No browser: a small fake DOM records styles, classes and
// Web Animations. Run: node tests/card_expand_test.mjs (also run by
// test_card.py). Visual correctness is not tested here.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ------------------------------------------------------------ fake DOM
let reduced = false;
const listeners = new Map(); // window event -> Set of handlers

class FakeAnimation {
  constructor(target, keyframes, opts) {
    this.effect = { target };
    this.keyframes = keyframes;
    this.opts = opts || {};
    this.playState = "running";
    this.onfinish = null;
  }
  cancel() {
    this.playState = "idle";
  }
  finish() {
    if (this.playState !== "running") return;
    this.playState = "finished";
    if (this.onfinish) this.onfinish();
  }
}

class FakeClassList {
  constructor() {
    this.set = new Set();
  }
  add(...c) {
    c.forEach((x) => this.set.add(x));
  }
  remove(...c) {
    c.forEach((x) => this.set.delete(x));
  }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c);
    else this.set.delete(c);
    return on;
  }
  contains(c) {
    return this.set.has(c);
  }
}

const allAnims = [];
class FakeEl {
  constructor(tag, rect) {
    this.localName = tag;
    this.style = {};
    this.classList = new FakeClassList();
    this.children = [];
    this.parentNode = null;
    this.rect = rect || { left: 0, top: 0, width: 0, height: 0 };
    this.cs = {}; // computed style overrides
    this.textContent = "";
    this.title = "";
    this._html = "";
  }
  setAttribute() {}
  addEventListener(ev, fn, opts) {
    (this.listeners ??= []).push({ ev, fn, opts });
  }
  removeEventListener(ev, fn) {
    this.listeners = (this.listeners || []).filter((l) => !(l.ev === ev && l.fn === fn));
  }
  getBoundingClientRect() {
    const r = this.rectFn ? this.rectFn() : this.rect;
    return { ...r, right: r.left + r.width, bottom: r.top + r.height };
  }
  animate(keyframes, opts) {
    const a = new FakeAnimation(this, keyframes, opts);
    allAnims.push(a);
    return a;
  }
  set innerHTML(v) {
    this._html = v;
    if (this.isStats) {
      const n = (v.match(/<div class="stat[ "]/g) || []).length;
      this.children = Array.from({ length: n }, () => new FakeEl("div"));
    }
  }
  get innerHTML() {
    return this._html;
  }
}

const px = (v) => parseFloat(v) || 0;
function cardRect(card) {
  if (card.classList.contains("expanded")) {
    return { left: px(card.style.left), top: px(card.style.top), width: px(card.style.width), height: px(card.style.height) };
  }
  return card.host.getBoundingClientRect();
}

class FakeShadowRoot {
  constructor(host) {
    this.host = host;
    this.parentNode = null;
  }
  set innerHTML(_v) {
    const els = {};
    for (const s of [".title-text", ".badge", ".badge-text", ".note", ".when", ".detail", ".nav", ".prev",
      ".replay", ".replay path", ".next", ".pos", ".message", ".stats", ".legend", ".chart", ".head",
      ".backdrop"]) {
      els[s] = new FakeEl("div");
    }
    els[".stats"].isStats = true;
    const card = new FakeEl("ha-card");
    card.host = this.host;
    card.rectFn = () => cardRect(card);
    card.cs = { paddingBottom: "16px", borderTopLeftRadius: "12px" };
    card.children = [".head", ".message", ".note", ".stats", ".legend", ".chart"].map((s) => els[s]);
    els["ha-card"] = card;
    const chart = els[".chart"];
    chart.rectFn = () => {
      const c = cardRect(card);
      return { left: c.left + 16, top: c.top + 180, width: c.width - 32, height: px(chart.style.height) };
    };
    this.els = els;
  }
  querySelector(s) {
    return this.els[s] || null;
  }
}

class FakeHTMLElement {
  constructor() {
    this.style = {};
    this.parentNode = null;
    this.slot = { left: 600, top: 300, width: 400, height: 420 };
  }
  attachShadow() {
    this.shadowRoot = new FakeShadowRoot(this);
    return this.shadowRoot;
  }
  getBoundingClientRect() {
    const r = this.slot;
    return { ...r, right: r.left + r.width, bottom: r.top + r.height };
  }
}

const registry = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = { get: (n) => registry.get(n), define: (n, c) => registry.set(n, c) };
globalThis.window = globalThis;
globalThis.navigator ??= { language: "en" };
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.innerHeight = 716;
globalThis.innerWidth = 1470;
globalThis.document = { documentElement: { clientWidth: 1470, clientHeight: 716 } };
globalThis.matchMedia = (q) => ({ matches: reduced && q.includes("reduce") });
globalThis.getComputedStyle = (el) => ({ opacity: "1", display: "block", visibility: "visible", ...(el.cs || {}) });
globalThis.ResizeObserver = class {
  observe() {}
  disconnect() {}
};
globalThis.addEventListener = (ev, fn) => {
  if (!listeners.has(ev)) listeners.set(ev, new Set());
  listeners.get(ev).add(fn);
};
globalThis.removeEventListener = (ev, fn) => listeners.get(ev)?.delete(fn);
console.info = () => {};

const here = path.dirname(fileURLToPath(import.meta.url));
await import(path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js"));
const Card = customElements.get("difluid-brew-card");

// HA-like ancestry: hui-root (header 56 px) > hui-view-container (left 256)
// > hui-view > hui-sections-view > [shadow] .container (padding 24/32) >
// hui-grid-section > hui-card > card.
function mountInDashboard(card, { header = 56, viewLeft = 256, lovelace = { editMode: false } } = {}) {
  const chain = (child, parent) => {
    child.parentNode = parent;
    return parent;
  };
  const root = new FakeEl("hui-root");
  root.lovelace = lovelace;
  const headerEl = new FakeEl("div", { left: 0, top: 0, width: 1470, height: header });
  root.shadowRoot = { querySelector: (s) => (s === ".header" ? headerEl : null) };
  const rootShadow = { host: root, parentNode: null };
  const container = new FakeEl("hui-view-container", { left: viewLeft, top: header, width: 1470 - viewLeft, height: 716 - header });
  const view = new FakeEl("hui-view", container.rect);
  const sv = new FakeEl("hui-sections-view", container.rect);
  const svShadow = { host: sv, parentNode: null };
  const svContainer = new FakeEl("div");
  svContainer.cs = { paddingLeft: "32px", paddingTop: "24px" };
  const section = new FakeEl("hui-grid-section");
  const huiCard = new FakeEl("hui-card");
  chain(card, huiCard);
  chain(huiCard, section);
  chain(section, svContainer);
  chain(svContainer, svShadow);
  chain(sv, view);
  chain(view, container);
  chain(container, rootShadow);
  return { root, view, huiCard, lovelace };
}

let connected = false;
function makeCard(config = { expand_when_active: true }, { mount = true, dash = {} } = {}) {
  const card = new Card();
  Object.defineProperty(card, "isConnected", { get: () => card._connected });
  card._connected = false;
  card._load = () => {};
  card._subscribe = () => {};
  card._renderChart = () => {};
  card.setConfig(config);
  if (mount) card.ha = mountInDashboard(card, dash);
  card._connected = true;
  card.connectedCallback();
  return card;
}

// The live subscription's snapshot (live_idle + live_ready), after which
// changes are animated.
function settle(card, ready = false) {
  card._onLive({ type: "live_idle" });
  card._onLive({ type: "live_ready", ready });
}

const anims = (card, target) => allAnims.filter((a) => a.effect.target === target);
const running = () => allAnims.filter((a) => a.playState === "running");
const finishAll = () => {
  for (let i = 0; i < 10 && running().length; i++) running().forEach((a) => a.finish());
};
function reset() {
  finishAll();
  allAnims.length = 0;
  reduced = false;
  listeners.clear();
}
// Consistent collapsed end state: nothing fixed, clipped, held or animating.
function assertCollapsed(card, msg) {
  assert.equal(card._expanded, false, `${msg}: not expanded`);
  assert.equal(card._xPhase, "idle", msg);
  assert.ok(!card._card.classList.contains("expanded"), `${msg}: no fixed class`);
  assert.ok(!card._backdrop.classList.contains("on"), `${msg}: backdrop off`);
  assert.equal(card._card.style.left || "", "", msg);
  assert.equal(card.style.height || "", "", `${msg}: placeholder size removed`);
  assert.equal(card._renderHeld, false, msg);
  assert.equal(running().filter((a) => a.opts.fill && a.opts.fill !== "backwards").length, 0, `${msg}: no fill left`);
  assert.ok(!listeners.get("resize")?.has(card._onExpandResize), `${msg}: resize listener removed`);
  assert.ok(!listeners.get("orientationchange")?.has(card._onExpandResize), msg);
}

let scenarios = 0;

// 1. _isActive(): ready view and a real live brew (brewing, tail, parked)
//    count; a replay and the idle history do not.
{
  reset();
  const card = makeCard({});
  const live = (extra) => ({ points: [], phase: "brewing", ...extra });
  assert.equal(card._isActive(), false, "idle");
  card._ready = true;
  assert.equal(card._isActive(), true, "ready view");
  card._ready = false;
  for (const phase of ["brewing", "tail", "parked"]) {
    card._live = live({ phase });
    assert.equal(card._isActive(), true, phase);
  }
  card._live = live({ replay: true });
  assert.equal(card._isActive(), false, "replay");
  card._ready = true;
  assert.equal(card._isActive(), false, "replay hides the ready view");
  card._live = live({ phase: "tail" });
  assert.equal(card._isActive(), true, "ready during a brew");
  card._live = null;
  scenarios++;
}

// 2. Option off (default): the ready view and a brew never expand.
{
  reset();
  const card = makeCard({});
  settle(card);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._expanded, false);
  card._onLive({ type: "live_start", start_ts: 1, dose: 18, points: [], weight: 0, flow: 0 });
  assert.equal(card._expanded, false);
  assert.equal(allAnims.length, 0, "no animation");
  assert.equal(card.style.height, undefined, "host untouched");
  assert.equal(card._chartH, 200, "chart height unchanged");
  scenarios++;
}

// 3. Animated expand and collapse: placeholder size, fixed rect below the
//    header and right of the sidebar with the sections inset, clip-path
//    reveal from the slot, content after the surface (<= 300 ms), fill
//    height; collapse restores the configured chart height.
{
  reset();
  const card = makeCard({ expand_when_active: true, chart_height: 320 });
  settle(card);
  assert.equal(card._expanded, false);
  assert.equal(card._chartH, 320);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._expanded, true);
  assert.equal(card._xPhase, "expanding");
  assert.equal(card.style.height, "420px", "placeholder keeps the slot height");
  assert.ok(card._card.classList.contains("expanded"));
  assert.ok(card._backdrop.classList.contains("on"));
  // Area: left 256, top 56, to 1470 x 716; inset 32 / 24.
  assert.deepEqual(
    [card._backdrop.style.left, card._backdrop.style.top, card._backdrop.style.width, card._backdrop.style.height],
    ["256px", "56px", "1214px", "660px"]
  );
  const r = card._expandRect;
  assert.deepEqual([r.left, r.top, r.width, r.height], [288, 80, 1150, 612]);
  const [surface] = anims(card, card._card);
  assert.equal(surface.opts.duration, 380);
  assert.equal(surface.opts.easing, "cubic-bezier(0.2, 0, 0, 1)");
  // Slot 600,300 400x420 inside 288,80 1150x612.
  assert.equal(surface.keyframes[0].clipPath, "inset(220px 438px 0px 312px round 12px)");
  assert.equal(surface.keyframes[1].clipPath, "inset(0px 0px 0px 0px round 12px)");
  const [backdrop] = anims(card, card._backdrop);
  assert.deepEqual(backdrop.keyframes.map((k) => k.opacity), [0, 1]);
  // Content enters after the surface, done <= 300 ms later.
  const content = allAnims.filter((a) => ![card._card, card._backdrop].includes(a.effect.target));
  assert.deepEqual(
    content.map((a) => a.effect.target),
    [card._head, card._els.stats, card._els.legend, card._chartBox],
    "stable containers, not the replaced stat children"
  );
  for (const a of content) {
    assert.ok(a.opts.delay >= 380, "after the surface");
    assert.ok(a.opts.delay + a.opts.duration - 380 <= 300, "within 300 ms");
    assert.equal(a.opts.fill, "backwards");
  }
  assert.ok(card._card.classList.contains("fill"), "large live figures");
  // Fill height: card bottom 692 - chart top (80 + 180) - padding 16.
  assert.equal(card._chartH, 692 - 260 - 16);
  assert.ok(listeners.get("resize")?.size, "follows window resizes");
  surface.finish();
  assert.equal(card._xPhase, "expanded");
  finishAll();
  // A brew starts: stays expanded, no new surface animation.
  const before = allAnims.length;
  card._onLive({ type: "live_start", start_ts: 1, dose: 18, points: [], weight: 0, flow: 0 });
  assert.equal(card._xPhase, "expanded");
  assert.equal(allAnims.length, before);
  // Resize: re-laid out to the new viewport.
  document.documentElement.clientWidth = 1000;
  for (const fn of listeners.get("resize")) fn();
  assert.equal(card._expandRect.width, 1000 - 256 - 64);
  document.documentElement.clientWidth = 1470;
  for (const fn of listeners.get("resize")) fn();
  assert.equal(card._expandRect.width, 1150);
  // Brew ends: collapse; the live view stays while it fades out.
  card._els.stats.innerHTML = '<div class="stat big">LIVE</div>';
  // The integration turns "Prepare brew" off before live_end.
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(card._xPhase, "expanded", "the brew is still active");
  card._onLive({ type: "live_end", brew_id: "b1", brew: { id: "b1", start: 0, dose: 18, yield: 36 } });
  assert.equal(card._xPhase, "collapsing");
  assert.equal(card._renderHeld, true);
  assert.match(card._els.stats.innerHTML, /LIVE/, "render deferred during the collapse");
  const collapse = anims(card, card._card).at(-1);
  assert.equal(collapse.opts.delay, 120);
  assert.equal(collapse.opts.duration, 300);
  assert.equal(collapse.keyframes[0].clipPath, "inset(0px 0px 0px 0px round 12px)");
  assert.equal(collapse.keyframes[1].clipPath, "inset(220px 438px 0px 312px round 12px)");
  const fades = allAnims.filter((a) => a.playState === "running" && a.opts.duration === 120);
  assert.equal(fades.length, 6, "every content child fades out");
  collapse.finish();
  assertCollapsed(card, "after collapse");
  assert.equal(card._chartH, 320, "configured chart height back");
  assert.ok(!card._card.classList.contains("fill"));
  assert.doesNotMatch(card._els.stats.innerHTML, /LIVE/, "saved brew rendered");
  const back = running().filter((a) => a.opts.duration === 150);
  assert.equal(back.length, 6, "saved brew fades in");
  scenarios++;
}

// 4. Flips mid-animation (cup lifted right after placing it, and placed
//    again): each animation starts from the current clip; any end state is
//    consistent.
{
  reset();
  const card = makeCard();
  settle(card);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._xPhase, "expanding");
  const expand1 = anims(card, card._card)[0];
  // Halfway visually.
  card._card.cs.clipPath = "inset(100px 200px 0px 150px round 12px)";
  card._backdrop.cs = { opacity: "0.6" };
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(expand1.playState, "idle", "expand cancelled");
  assert.equal(card._xPhase, "collapsing");
  const collapse1 = anims(card, card._card).at(-1);
  assert.equal(collapse1.keyframes[0].clipPath, "inset(100px 200px 0px 150px round 12px)");
  assert.equal(collapse1.opts.fill, "both");
  assert.equal(anims(card, card._backdrop).at(-1).keyframes[0].opacity, 0.6);
  // Placed again during the collapse: expands from where it is.
  card._card.cs.clipPath = "inset(150px 300px 0px 250px round 12px)";
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(collapse1.playState, "idle", "collapse cancelled");
  assert.equal(card._xPhase, "expanding");
  assert.equal(card._renderHeld, false, "active view rendered again");
  assert.equal(card.style.height, "420px", "placeholder kept");
  const expand2 = anims(card, card._card).at(-1);
  assert.equal(expand2.keyframes[0].clipPath, "inset(150px 300px 0px 250px round 12px)");
  assert.equal(running().filter((a) => a.opts.fill === "forwards").length, 0, "fade-outs cancelled");
  // Lifted again and everything runs out: back in the slot.
  card._card.cs.clipPath = "none";
  card._onLive({ type: "live_ready", ready: false });
  finishAll();
  assertCollapsed(card, "after flips");
  // The cancelled first collapse's finish handler must not act later.
  collapse1.onfinish && collapse1.onfinish();
  assertCollapsed(card, "stale finish ignored");
  scenarios++;
}

// 5. Never in edit mode or the editor preview; turning edit mode on while
//    expanded collapses.
{
  reset();
  for (const setup of [
    (c) => (c.editMode = true),
    (c) => (c.preview = true),
    (c) => (c.ha.lovelace.editMode = true),
    (c) => (c.ha.huiCard.parentNode.localName = "hui-card-edit-mode"),
  ]) {
    const card = makeCard();
    settle(card);
    setup(card);
    card._onLive({ type: "live_ready", ready: true });
    assert.equal(card._expanded, false, String(setup));
    assert.equal(card._readyView(), true, "the ready view itself still shows");
  }
  // Editor preview: the card sits in hui-card-preview inside a dialog.
  const preview = makeCard(undefined, { mount: false });
  const host = new FakeEl("hui-card-preview");
  host.parentNode = new FakeEl("hui-dialog-edit-card");
  preview.parentNode = host;
  settle(preview, true);
  assert.equal(preview._expanded, false, "editor preview");
  // Edit mode switched on while expanded (only a hass update follows).
  const card = makeCard();
  settle(card, true);
  assert.equal(card._expanded, true);
  card.ha.lovelace.editMode = true;
  card.hass = { connection: card._conn, language: "en" };
  assert.ok(["collapsing", "idle"].includes(card._xPhase));
  finishAll();
  assertCollapsed(card, "edit mode");
  scenarios++;
}

// 6. Disconnect mid-animation: nothing stays fixed, clipped or animating.
{
  reset();
  const card = makeCard();
  settle(card);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._xPhase, "expanding");
  card._connected = false;
  card.disconnectedCallback();
  assert.equal(running().length, 0, "all animations cancelled");
  assertCollapsed(card, "disconnected");
  // Disconnected while collapsing, too.
  const c2 = makeCard();
  settle(c2, true);
  c2._onLive({ type: "live_ready", ready: false });
  c2._card.cs.clipPath = "none";
  c2._onLive({ type: "live_ready", ready: true });
  c2._onLive({ type: "live_ready", ready: false });
  assert.equal(c2._xPhase, "collapsing");
  c2._connected = false;
  c2.disconnectedCallback();
  assert.equal(running().length, 0);
  assertCollapsed(c2, "disconnected while collapsing");
  scenarios++;
}

// 7. Reduced motion: switches instantly, no animation at all.
{
  reset();
  reduced = true;
  const card = makeCard();
  settle(card);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._xPhase, "expanded");
  assert.ok(card._card.classList.contains("expanded"));
  card._onLive({ type: "live_ready", ready: false });
  assertCollapsed(card, "reduced motion");
  assert.equal(allAnims.length, 0, "no animation");
  scenarios++;
}

// 8. Page load during a brew: the subscription's snapshot expands at once;
//    the brew's end afterwards animates. A replay never expands.
{
  reset();
  const card = makeCard();
  card._onLive({ type: "live_start", start_ts: 1, dose: 18, points: [[0, 0, 0]], weight: 0, flow: 0 });
  assert.equal(card._xPhase, "expanded", "snapshot: instant");
  assert.equal(allAnims.length, 0);
  card._onLive({ type: "live_ready", ready: false });
  card._onLive({ type: "live_end", brew_id: "b2", brew: { id: "b2", start: 0 } });
  assert.equal(card._xPhase, "collapsing", "later changes animate");
  finishAll();
  assertCollapsed(card, "after load-time brew");
  // Replay of a stored brew.
  card._brews = [{ id: "b2", start: 0, duration: 2 }];
  card._cache.set("b2", { id: "b2", duration: 2, profile: [[0, 0, 0], [1, 1, 1]] });
  card._index = 0;
  card._toggleReplay();
  assert.ok(card._live && card._live.replay);
  assert.equal(card._expanded, false, "replay does not expand");
  card._toggleReplay();
  scenarios++;
}

// 9. Without HA's structure (e.g. a plain page): the whole viewport with the
//    default inset; option turned off while expanded collapses.
{
  reset();
  const card = makeCard(undefined, { mount: false });
  settle(card, true);
  assert.equal(card._expanded, true);
  const r = card._expandRect;
  assert.deepEqual([r.left, r.top, r.width, r.height], [16, 16, 1470 - 32, 716 - 32]);
  card.setConfig({ expand_when_active: false });
  finishAll();
  assertCollapsed(card, "option turned off");
  assert.equal(card._chartH, 200);
  // Kiosk: header hidden (height 0), no sidebar.
  const kiosk = makeCard(undefined, { dash: { header: 0, viewLeft: 0 } });
  settle(kiosk, true);
  const k = kiosk._expandRect;
  assert.deepEqual([k.left, k.top], [32, 24]);
  scenarios++;
}

// 10. "Prepare brew" on during a replay ends the replay and expands.
{
  reset();
  const card = makeCard();
  settle(card);
  card._brews = [{ id: "b3", start: 0, duration: 2 }];
  card._cache.set("b3", { id: "b3", duration: 2, profile: [[0, 0, 0], [1, 1, 1]] });
  card._index = 0;
  card._toggleReplay();
  assert.ok(card._live && card._live.replay);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._live, null, "replay ended");
  assert.equal(card._readyView(), true);
  assert.equal(card._xPhase, "expanding");

  // Option off: the replay keeps running, as before 0.5.0b8.
  reset();
  const off = makeCard({});
  settle(off);
  off._brews = [{ id: "b3", start: 0, duration: 2 }];
  off._cache.set("b3", { id: "b3", duration: 2, profile: [[0, 0, 0], [1, 1, 1]] });
  off._index = 0;
  off._toggleReplay();
  off._onLive({ type: "live_ready", ready: true });
  assert.ok(off._live && off._live.replay, "replay kept with the option off");
  scenarios++;
}

// 11. Re-attach (view switch): the ready state is reset on disconnect, and
//     nothing expands until the new subscription's snapshot arrives; then
//     at once.
{
  reset();
  const card = makeCard();
  settle(card, true);
  assert.equal(card._expanded, true);
  card._connected = false;
  card.disconnectedCallback();
  assert.equal(card._ready, false);
  assert.equal(card._readyDose, null);
  assert.equal(card._readyTarget, null);
  card._ready = true; // even a stale value must not expand before the snapshot
  card._connected = true;
  card.hass = { connection: {}, language: "en" };
  card.connectedCallback();
  card._render();
  assert.equal(card._expanded, false, "waits for the snapshot");
  card._onLive({ type: "live_idle" });
  assert.equal(card._expanded, false, "live_idle alone: ready not yet known");
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._xPhase, "expanded", "snapshot: instant");
  assert.equal(allAnims.filter((a) => a.playState === "running").length, 0);
  scenarios++;
}

// 12. editMode / preview setters re-render only on a change.
{
  reset();
  const card = makeCard({});
  let renders = 0;
  const orig = card._render.bind(card);
  card._render = () => {
    renders++;
    orig();
  };
  card.editMode = false;
  card.preview = false;
  assert.equal(renders, 0, "unchanged: no render");
  card.editMode = true;
  card.editMode = true;
  card.preview = true;
  assert.equal(renders, 2);
  scenarios++;
}

// 13. Scroll-through: non-passive touchmove/wheel blockers on backdrop and
//     card while expanded, removed afterwards.
{
  reset();
  const card = makeCard();
  settle(card, true);
  for (const el of [card._backdrop, card._card]) {
    for (const ev of ["touchmove", "wheel"]) {
      const l = (el.listeners || []).find((x) => x.ev === ev);
      assert.ok(l, `${ev} blocked`);
      assert.equal(l.opts.passive, false);
      let prevented = false;
      l.fn({ cancelable: true, preventDefault: () => (prevented = true) });
      assert.ok(prevented);
    }
  }
  card._onLive({ type: "live_ready", ready: false });
  finishAll();
  assertCollapsed(card, "blockers");
  for (const el of [card._backdrop, card._card]) {
    assert.equal((el.listeners || []).filter((x) => ["touchmove", "wheel"].includes(x.ev)).length, 0);
  }
  scenarios++;
}

// 14. Resize during a running animation: it ends at its end state first.
{
  reset();
  const card = makeCard();
  settle(card);
  card._onLive({ type: "live_ready", ready: true });
  assert.equal(card._xPhase, "expanding");
  for (const fn of [...listeners.get("resize")]) fn();
  assert.equal(card._xPhase, "expanded");
  assert.equal(running().length, 0, "expand finished");
  assert.ok(card._card.classList.contains("expanded"));
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(card._xPhase, "collapsing");
  for (const fn of [...listeners.get("resize")]) fn();
  assertCollapsed(card, "resize during collapse");
  scenarios++;
}

// 15. Expand interrupting a collapse: content fades in from its current
//     opacity, not from 0.
{
  reset();
  const card = makeCard();
  settle(card, true);
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(card._xPhase, "collapsing");
  card._card.cs.clipPath = "inset(50px 50px 50px 50px round 12px)";
  for (const el of card._card.children) el.cs = { opacity: "0.4" };
  const n = allAnims.length;
  card._onLive({ type: "live_ready", ready: true });
  const enter = allAnims.slice(n).filter((a) => [card._head, card._els.stats, card._els.legend, card._chartBox].includes(a.effect.target));
  assert.equal(enter.length, 4);
  for (const a of enter) assert.equal(a.keyframes[0].opacity, 0.4, "from the current opacity");
  for (const el of card._card.children) el.cs = {};
  scenarios++;
}

// 16. chart_height: fill set while expanded: the viewport watch is
//     attached and the fill height applies after the collapse.
{
  reset();
  const card = makeCard({ expand_when_active: true });
  settle(card, true);
  card.setConfig({ expand_when_active: true, chart_height: "fill" });
  card._onLive({ type: "live_ready", ready: false });
  finishAll();
  assertCollapsed(card, "fill set while expanded");
  assert.ok(card._fillObs, "viewport watched");
  assert.ok(card._card.classList.contains("fill"));
  scenarios++;
}

console.log(`card expand: ${scenarios} scenarios OK`);
