// Node-only test of the brew card's subscribe/load retry (no browser).
// Run: node tests/card_retry_test.mjs   (also run by tests/test_card.py)
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ------------------------------------------------------------ fake timers
let now = 0;
let timers = [];
let nextId = 1;
globalThis.setTimeout = (fn, ms) => {
  const id = nextId++;
  timers.push({ id, at: now + ms, fn });
  return id;
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter((t) => t.id !== id);
};
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift();
    now = t.at;
    t.fn();
    await flush();
  }
  now = end;
  await flush();
}

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
globalThis.cancelAnimationFrame = () => {};
console.info = () => {};

const here = path.dirname(fileURLToPath(import.meta.url));
await import(
  path.join(here, "../custom_components/difluid_scale/www/difluid-brew-card.js")
);
const Card = customElements.get("difluid-brew-card");

// ------------------------------------------------------------ fake backend
class FakeConn {
  constructor(plan) {
    this.plan = plan; // type -> array of booleans (true = succeed), last repeats
    this.calls = [];
    this.listeners = new Map();
  }
  _ok(type) {
    const p = this.plan[type] || [true];
    const n = this.calls.filter((c) => c === type).length;
    this.calls.push(type);
    return p[Math.min(n, p.length - 1)];
  }
  subscribeMessage(_cb, msg) {
    return this._ok(msg.type)
      ? Promise.resolve(() => {})
      : Promise.reject(new Error("unknown_command"));
  }
  addEventListener(ev, fn) {
    this.listeners.set(ev, fn);
  }
  removeEventListener(ev, fn) {
    if (this.listeners.get(ev) === fn) this.listeners.delete(ev);
  }
  count(type) {
    return this.calls.filter((c) => c === type).length;
  }
}

// opts.brews: the stored list; opts.hold: answers wait until released
// (conn.release()), like a reload still in flight on the network.
function hassWith(conn, opts = {}) {
  conn.held = [];
  conn.release = () => conn.held.splice(0).forEach((go) => go());
  // The call is counted when it is made, answered now or on release.
  const answer = (msg, ok = conn._ok(msg.type)) =>
    ok
      ? Promise.resolve(
          msg.type.endsWith("/brews")
            ? { brews: opts.brews || [] }
            : { brew: opts.brews ? { id: msg.brew_id, profile: [] } : null }
        )
      : Promise.reject(new Error("failed"));
  return {
    connection: conn,
    language: "en",
    callWS: (msg) =>
      opts.hold
        ? ((ok) => new Promise((resolve) => conn.held.push(() => resolve(answer(msg, ok)))))(
            conn._ok(msg.type)
          )
        : answer(msg),
  };
}

function makeCard() {
  const card = new Card();
  card._connected = true;
  Object.defineProperty(card, "isConnected", { get: () => card._connected });
  card._render = () => {};
  card._renderChart = () => {};
  card.setConfig({});
  return card;
}

const SUB = "difluid_scale/subscribe_brews";
const LIVE = "difluid_scale/subscribe_live";
const LIST = "difluid_scale/brews";
const BREW = "difluid_scale/brew";

async function reset() {
  now = 0;
  timers = [];
}

// 1. A failed subscription and load are retried with backoff and recover.
{
  await reset();
  const conn = new FakeConn({ [SUB]: [false, false, true], [LIVE]: [false, true], [LIST]: [false, true] });
  const card = makeCard();
  card.hass = hassWith(conn);
  await flush();
  assert.equal(card._unsub, null, "failed subscribe frees its slot");
  assert.equal(card._liveUnsub, null);
  assert.equal(card._brews, null);
  assert.equal(timers.length, 1, "one retry pending");
  await advance(1999);
  assert.equal(conn.count(SUB), 1, "no retry before 2 s");
  await advance(1);
  assert.equal(conn.count(SUB), 2);
  assert.equal(conn.count(LIVE), 2, "live retried");
  assert.ok(card._liveUnsub, "live subscribed");
  assert.deepEqual(card._brews, [], "brews loaded");
  assert.equal(card._unsub, null, "brews subscribe failed again");
  await advance(4999);
  assert.equal(conn.count(SUB), 2, "second retry after 5 s, not 2 s");
  await advance(1);
  assert.equal(conn.count(SUB), 3);
  assert.ok(card._unsub);
  assert.equal(conn.count(LIVE), 2, "a working subscription is not redone");
  await advance(120000);
  assert.equal(conn.count(SUB), 3, "no retries once everything works");
  assert.equal(timers.length, 0);
}

// 2. Backoff 2, 5, 10, 30, 30 s while it keeps failing; stops when removed.
{
  await reset();
  const conn = new FakeConn({ [SUB]: [false], [LIVE]: [true], [LIST]: [true] });
  const card = makeCard();
  card.hass = hassWith(conn);
  await flush();
  const at = [];
  for (let i = 0; i < 5; i++) {
    const before = conn.count(SUB);
    const start = now;
    while (conn.count(SUB) === before) await advance(1000);
    at.push(now - start);
  }
  assert.deepEqual(at, [2000, 5000, 10000, 30000, 30000]);
  card.disconnectedCallback();
  assert.equal(timers.length, 0, "retry cancelled on removal");
  const n = conn.count(SUB);
  await advance(300000);
  assert.equal(conn.count(SUB), n, "no retries after removal");
}

// 3. The websocket's ready event retries at once (and resets the backoff).
{
  await reset();
  const conn = new FakeConn({ [SUB]: [false, false, false, true], [LIVE]: [true], [LIST]: [true] });
  const card = makeCard();
  card.hass = hassWith(conn);
  await flush();
  await advance(2000); // second failure, next retry in 5 s
  assert.equal(conn.count(SUB), 2);
  conn.listeners.get("ready")();
  await flush();
  assert.equal(conn.count(SUB), 3, "ready retries immediately");
  assert.equal(card._unsub, null);
  await advance(2000);
  assert.equal(conn.count(SUB), 4, "backoff restarted at 2 s");
  assert.ok(card._unsub);
}

// 4. A new connection object on a hass update retries at once.
{
  await reset();
  const conn = new FakeConn({ [SUB]: [false], [LIVE]: [false], [LIST]: [false] });
  const card = makeCard();
  card.hass = hassWith(conn);
  await flush();
  const conn2 = new FakeConn({});
  const hass2 = hassWith(conn2);
  card.hass = hass2;
  await flush();
  assert.equal(conn2.count(SUB), 1);
  assert.equal(conn2.count(LIVE), 1);
  assert.equal(conn2.count(LIST), 1);
  assert.ok(card._unsub && card._liveUnsub && card._brews);
  assert.equal(timers.length, 0, "pending retry cancelled");
  card.hass = hass2; // same connection: nothing happens
  await flush();
  assert.equal(conn2.count(SUB), 1);
}

// 5. Audit C1: a failed brew switch (_step) does not leave the card on the
//    error: the brew is fetched again with backoff, list and position kept.
{
  await reset();
  const conn = new FakeConn({ [BREW]: [true, false, false, true] });
  const card = makeCard();
  card.hass = hassWith(conn, { brews: [{ id: "a" }, { id: "b" }] });
  await flush();
  assert.equal(conn.count(BREW), 1, "newest brew fetched");
  await card._step(1);
  assert.equal(card._index, 1);
  assert.equal(card._error, "failed", "the failure is shown");
  assert.equal(timers.length, 1, "and retried");
  await advance(2000);
  assert.equal(conn.count(BREW), 3, "retried after 2 s");
  assert.equal(card._error, "failed", "still failing");
  await advance(5000);
  assert.equal(conn.count(BREW), 4, "backoff: next try after 5 s");
  assert.equal(card._error, null, "recovered");
  assert.equal(card._index, 1, "position kept");
  assert.ok(card._cache.get("b"), "brew shown");
  assert.equal(conn.count(LIST), 1, "the list is not reloaded");
  assert.equal(timers.length, 0, "no further retries");
  assert.equal(card._retryCount, 0, "backoff reset");
  // Stepping works again afterwards.
  await card._step(-1);
  assert.equal(card._index, 0);
  assert.equal(card._error, null);
}

// 6. A failed step is also recovered by the reconnect reload, which
//    cancels the pending retry.
{
  await reset();
  const conn = new FakeConn({ [BREW]: [true, false, true] });
  const card = makeCard();
  card.hass = hassWith(conn, { brews: [{ id: "a" }, { id: "b" }] });
  await flush();
  await card._step(1);
  assert.equal(card._error, "failed");
  conn.listeners.get("ready")();
  await flush();
  assert.equal(card._error, null, "reloaded");
  assert.equal(card._index, 1, "on the same brew");
  assert.equal(timers.length, 0, "retry cancelled");
}

// 7. Audit C5: a reconnect loads the history once, although both the
//    connection's ready event and the resubscribed live snapshot ask.
{
  await reset();
  const conn = new FakeConn({});
  const card = makeCard();
  card.hass = hassWith(conn, { brews: [{ id: "a" }], hold: true });
  await flush();
  card._onLive({ type: "live_idle" }); // first snapshot
  conn.release();
  await flush();
  conn.release();
  await flush();
  assert.equal(conn.count(LIST), 1);
  // Reconnect: ready first, then the library's resubscribe snapshot,
  // while the reload is still in flight.
  conn.listeners.get("ready")();
  card._onLive({ type: "live_idle" });
  card._onLive({ type: "live_ready", ready: false });
  assert.equal(conn.count(LIST), 2, "one reload for the reconnect");
  conn.release();
  await flush();
  conn.release();
  await flush();
  assert.equal(conn.count(LIST), 2);
  assert.equal(card._resyncing, null, "done");
  // The next reconnect reloads again.
  conn.listeners.get("ready")();
  card._onLive({ type: "live_idle" });
  assert.equal(conn.count(LIST), 3);
  conn.release();
  await flush();
  conn.release();
  await flush();
  // A brew whose end was missed during the reconnect: still one reload.
  card._onLive({ type: "live_start", start_ts: 0, dose: 18, points: [], weight: 0, flow: 0, elapsed: 1, phase: "brewing" });
  conn.listeners.get("ready")();
  card._onLive({ type: "live_idle" });
  assert.equal(card._live, null, "back to the history");
  assert.equal(conn.count(LIST), 4, "one reload");
  conn.release();
  await flush();
  conn.release();
  await flush();
  assert.equal(conn.count(LIST), 4);
}

console.log("card retry: 7 scenarios OK");
