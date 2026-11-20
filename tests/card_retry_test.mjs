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

function hassWith(conn) {
  return {
    connection: conn,
    language: "en",
    callWS: (msg) =>
      conn._ok(msg.type)
        ? Promise.resolve(msg.type.endsWith("/brews") ? { brews: [] } : { brew: null })
        : Promise.reject(new Error("failed")),
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

console.log("card retry: 4 scenarios OK");
