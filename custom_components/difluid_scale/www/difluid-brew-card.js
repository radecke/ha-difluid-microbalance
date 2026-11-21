/*
 * DiFluid brew card: flow-rate profile of the brews recorded by the
 * difluid_scale integration. Plain custom element, no dependencies.
 * All colours come from Home Assistant theme variables.
 *
 * Live mode: subscribes to difluid_scale/subscribe_live. While a brew runs
 * the card switches to it (even from an older brew), draws flow and weight
 * as they arrive and shows time, flow and weight. Rendering is batched to
 * requestAnimationFrame; axes only grow and ease to their new range.
 *
 * Replay: a stored brew can be played back in real time through the same
 * live view (points released by their recorded time, 0.25 s batches).
 *
 * Ready: while the integration's "Prepare brew" switch is on (live_ready)
 * and no brew runs, the live view shows empty axes and zero values; the
 * brew then continues in it seamlessly.
 *
 * Flow curve (flow_smoothing > 0): recomputed from the weight instead of the
 * scale's flow value, which is a trailing ~3 s regression (lags 1-1.5 s).
 * The weight may only rise (vibration makes it fall back after upward
 * jumps; drops are ignored), then flow = least-squares slope over the last
 * flow_smoothing seconds. Both use only earlier points, so the live curve is
 * never revised. Plotted weight, hover and live stats use these values.
 */

const CARD_VERSION = "0.3.1";
const DOMAIN = "difluid_scale";

const STRINGS = {
  en: {
    title: "Espresso",
    dose: "Dose",
    yield: "Yield",
    ratio: "Ratio",
    time: "Time",
    flow: "Flow",
    weight: "Weight",
    corrected: "corrected",
    older: "Older brew",
    newer: "Newer brew",
    empty: "No brews yet. Start the scale's timer to record one.",
    loading: "Loading…",
    error: "Could not load brews",
    device: "Scale",
    card_title: "Title",
    picker_name: "DiFluid brew profile",
    picker_desc: "Flow-rate curve, dose, yield and ratio of the last brews.",
    live: "Live",
    brewing: "Brewing",
    tail: "Catching the last drops",
    parked: "Connection lost, waiting for the scale",
    discarded: "Not saved",
    discard_too_short: "Not saved: too short for a brew",
    discard_low_yield: "Not saved: too little in the cup",
    discard_no_samples: "Not saved: no data from the scale",
    flow_rate: "Flow",
    replay: "Replay",
    replay_start: "Replay this brew",
    replay_stop: "Stop replay",
    chart_height: "Chart height",
    chart_height_helper: "Pixels (e.g. 320) or \"fill\" to fill the screen below the card's top. Empty: default.",
    ready: "Ready – brew starts soon",
    flow_smoothing: "Flow smoothing",
    flow_smoothing_helper: "Seconds (0–10): flow curve computed from the weight over this window. 0: the scale's raw values. Empty: 3.",
  },
  de: {
    title: "Espresso",
    dose: "Dosis",
    yield: "Ausbeute",
    ratio: "Verhältnis",
    time: "Zeit",
    flow: "Fluss",
    weight: "Gewicht",
    corrected: "korrigiert",
    older: "Älterer Bezug",
    newer: "Neuerer Bezug",
    empty: "Noch keine Bezüge. Starte den Timer der Waage, um einen aufzuzeichnen.",
    loading: "Lädt …",
    error: "Bezüge konnten nicht geladen werden",
    device: "Waage",
    card_title: "Titel",
    picker_name: "DiFluid Bezugsprofil",
    picker_desc: "Flusskurve, Dosis, Ausbeute und Verhältnis der letzten Bezüge.",
    live: "Live",
    brewing: "Bezug läuft",
    tail: "Letzte Tropfen",
    parked: "Verbindung unterbrochen, warte auf die Waage",
    discarded: "Nicht gespeichert",
    discard_too_short: "Nicht gespeichert: zu kurz für einen Bezug",
    discard_low_yield: "Nicht gespeichert: zu wenig in der Tasse",
    discard_no_samples: "Nicht gespeichert: keine Daten von der Waage",
    flow_rate: "Fluss",
    replay: "Wiedergabe",
    replay_start: "Bezug abspielen",
    replay_stop: "Wiedergabe beenden",
    chart_height: "Diagrammhöhe",
    chart_height_helper: "Pixel (z. B. 320) oder \"fill\", um den Bildschirm unterhalb des Kartenanfangs zu füllen. Leer: Standard.",
    ready: "Bereit – Bezug startet gleich",
    flow_smoothing: "Flussglättung",
    flow_smoothing_helper: "Sekunden (0–10): Flusskurve aus dem Gewicht über dieses Fenster berechnet. 0: Rohwerte der Waage. Leer: 3.",
  },
};

const ICON_LEFT = "M15.41,16.58L10.83,12L15.41,7.41L14,6L8,12L14,18L15.41,16.58Z";
const ICON_RIGHT = "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z";
const ICON_PLAY = "M8,5.14V19.14L19,12.14L8,5.14Z";
const ICON_STOP = "M18,18H6V6H18V18Z";
const REPLAY_BATCH_MS = 250; // like the integration's live push
const SVG_NS = "http://www.w3.org/2000/svg";
const CHART_HEIGHT = 200; // default; also the minimum in fill mode
const CHART_HEIGHT_MIN = 100; // bounds for a fixed chart_height (px)
const CHART_HEIGHT_MAX = 2000;
const FILL_MARGIN = 8; // px kept free below the card in fill mode
const PAD = { left: 36, right: 40, top: 10, bottom: 24 };
// Live axes start at an espresso-sized range and only grow.
const LIVE_MIN = { t: 30, f: 3, w: 40 };
const LIVE_T_STEP = 10; // seconds the time axis extends by
const NOTE_MS = 6000;
// Backoff (ms) for retrying a failed subscribe/load; the last value repeats.
const RETRY_MS = [2000, 5000, 10000, 30000];
// Live line reveal: drawn up to a time that advances continuously (real
// time, faster when behind) instead of jumping with every 0.25 s batch.
const REVEAL_LAG = 0.3; // s behind the newest point before catching up
const REVEAL_MAX_LAG = 1.5; // never further behind than this
// Flow from weight (see the header). Window in seconds; 0 = raw scale values.
const FLOW_SMOOTHING_DEFAULT = 3;
const FLOW_SMOOTHING_MAX = 10;
// Flow starts at the first point this far above the initial weight (g);
// before it flow is 0, so the centred window shows no flow before the drops.
const FLOW_START_DELTA = 1.0;
const FLOW_MIN_POINTS = 3; // fewer points in the window: flow 0
const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function lang(hass) {
  const l = (hass && (hass.locale?.language || hass.language)) || "en";
  return l.toLowerCase().startsWith("de") ? "de" : "en";
}

// Axis with a "nice" step; the maximum is the first step at or above value.
function axis(value, minimum, count) {
  const v = Math.max(value, minimum);
  const raw = v / count;
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = 10 * exp;
  for (const m of [1, 2, 2.5, 5]) {
    if (m * exp >= raw) {
      step = m * exp;
      break;
    }
  }
  const max = Math.ceil(v / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; i * step <= max + 1e-9; i++) ticks.push(Math.round(i * step * 100) / 100);
  return { max, step, ticks };
}

// Smooth path through the points without overshoot (monotone cubic,
// Fritsch-Carlson tangents, as d3.curveMonotoneX): peaks stay where the
// data has them, flow never dips below zero between samples.
function monotonePath(xy) {
  const n = xy.length;
  const f = (v) => v.toFixed(1);
  if (n < 3) return xy.map((p, i) => `${i ? "L" : "M"}${f(p[0])},${f(p[1])}`).join("");
  const dx = [];
  const m = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(xy[i + 1][0] - xy[i][0]);
    m.push(dx[i] > 0 ? (xy[i + 1][1] - xy[i][1]) / dx[i] : 0);
  }
  const tan = new Array(n);
  tan[0] = m[0];
  tan[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (m[i - 1] * m[i] <= 0 || dx[i - 1] <= 0 || dx[i] <= 0) tan[i] = 0;
    else
      tan[i] =
        (3 * (dx[i - 1] + dx[i])) /
        ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
  }
  let d = `M${f(xy[0][0])},${f(xy[0][1])}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = xy[i];
    const [x1, y1] = xy[i + 1];
    if (dx[i] <= 0) {
      d += `L${f(x1)},${f(y1)}`;
      continue;
    }
    const h = dx[i] / 3;
    d += `C${f(x0 + h)},${f(y0 + tan[i] * h)},${f(x1 - h)},${f(y1 - tan[i + 1] * h)},${f(x1)},${f(y1)}`;
  }
  return d;
}

// Weight that only rises: a drop (machine vibration, the load cell springing
// back after a jump) is ignored, the last maximum is held. Causal, so a
// point once drawn never changes when later points arrive.
function monotone(values) {
  const out = [];
  let max = -Infinity;
  for (const v of values) {
    if (v > max) max = v;
    out.push(max);
  }
  return out;
}

// Flow at each point: least-squares slope of the weight over the points in
// [t - window, t] (only earlier points: the live curve is never revised). >= FLOW_MIN_POINTS points or 0;
// never negative; 0 before the flow starts (FLOW_START_DELTA).
function smoothedFlow(times, weights, window) {
  const n = times.length;
  const out = new Array(n).fill(0);
  if (!n) return out;
  let first = n;
  for (let i = 0; i < n; i++) {
    if (weights[i] > weights[0] + FLOW_START_DELTA) {
      first = i;
      break;
    }
  }
  let lo = 0;
  for (let i = 0; i < n; i++) {
    const t = times[i];
    while (lo < i && times[lo] < t - window - 1e-9) lo++;
    const hi = i;
    const count = hi - lo + 1;
    if (i < first || count < FLOW_MIN_POINTS) continue;
    let st = 0;
    let sw = 0;
    for (let j = lo; j <= hi; j++) {
      st += times[j];
      sw += weights[j];
    }
    const mt = st / count;
    const mw = sw / count;
    let num = 0;
    let den = 0;
    for (let j = lo; j <= hi; j++) {
      const d = times[j] - mt;
      num += d * (weights[j] - mw);
      den += d * d;
    }
    out[i] = den > 0 ? Math.max(0, num / den) : 0;
  }
  return out;
}

// Points as drawn: [t, monotone weight, flow from weight]; the raw points
// unchanged for smoothing 0.
function displayProfile(points, smoothing) {
  if (!smoothing || !points.length) return points;
  const times = points.map((p) => p[0]);
  const weights = monotone(points.map((p) => p[1]));
  const flow = smoothedFlow(times, weights, smoothing);
  return points.map((p, i) => [p[0], weights[i], flow[i]]);
}

// flow_smoothing option: absent -> default, else seconds 0..max.
function parseFlowSmoothing(value) {
  if (value === undefined || value === null || value === "") return FLOW_SMOOTHING_DEFAULT;
  const v = typeof value === "string" && /^\s*\d+(\.\d+)?\s*$/.test(value) ? Number(value) : value;
  if (typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= FLOW_SMOOTHING_MAX) return v;
  throw new Error(
    `flow_smoothing must be a number of seconds from 0 to ${FLOW_SMOOTHING_MAX} (0 = the scale's raw flow), got ${JSON.stringify(value)}`
  );
}

function svgEl(name, attrs) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  return el;
}

// chart_height option: absent -> null (default), "fill", or pixels.
// Throws a readable error for anything else (shown by setConfig).
function parseChartHeight(value) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value === "string") {
    const v = value.trim().toLowerCase();
    if (v === "fill") return "fill";
    const m = /^(\d+)(px)?$/.exec(v);
    if (m) value = Number(m[1]);
  }
  if (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= CHART_HEIGHT_MIN &&
    value <= CHART_HEIGHT_MAX
  ) {
    return value;
  }
  throw new Error(
    `chart_height must be a whole number of pixels from ${CHART_HEIGHT_MIN} to ${CHART_HEIGHT_MAX} or "fill", got ${JSON.stringify(value)}`
  );
}

function targetOf(config) {
  const t = {};
  if (config.device_id) t.device_id = config.device_id;
  if (config.entry_id) t.entry_id = config.entry_id;
  return t;
}

class DifluidBrewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._brews = null; // summaries, newest first
    this._index = 0;
    this._cache = new Map(); // brew id -> full brew
    this._error = null;
    this._unsub = null;
    this._loadToken = 0;
    this._hover = null;
    this._width = 0;
    this._built = false;
    this._live = null; // active brew from subscribe_live
    this._liveAxes = null;
    this._liveUnsub = null;
    this._liveIdles = 0; // live_idle messages since our own subscribe
    this._ready = false; // "Prepare brew" switch (live_ready)
    this._readyDose = null; // dose of the next brew (live_ready)
    this._smoothing = FLOW_SMOOTHING_DEFAULT;
    this._displayCache = new WeakMap(); // points array -> displayed points
    this._readyConn = null;
    this._onReady = () => {
      // Reconnected: make what failed meanwhile, then reload.
      this._cancelRetry();
      this._retryCount = 0;
      this._subscribe();
      this._resync();
    };
    this._retryTimer = null;
    this._retryCount = 0;
    this._conn = null; // connection the last hass object carried
    this._note = null;
    this._noteTimer = null;
    this._clock = null;
    this._raf = 0;
    this._pending = { stats: false, chart: false };
    this._heightMode = null; // null (default) | "fill" | pixels
    this._chartH = CHART_HEIGHT; // current chart height in px
    this._fitRaf = 0;
    this._onViewport = () => this._scheduleFit();
  }

  static getStubConfig() {
    return {};
  }

  static getConfigElement() {
    return document.createElement("difluid-brew-card-editor");
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    const heightMode = parseChartHeight(config.chart_height);
    this._smoothing = parseFlowSmoothing(config.flow_smoothing);
    const changed =
      JSON.stringify(targetOf(config)) !== JSON.stringify(targetOf(this._config));
    this._config = { ...config };
    this._setHeightMode(heightMode);
    if (changed) {
      this._brews = null;
      this._cache.clear();
      this._index = 0;
      this._error = null;
      this._ready = false;
      this._readyDose = null;
      this._setLive(null);
      if (this._hass && this.isConnected) {
        this._unsubscribe();
        this._subscribe();
        this._load();
      }
    }
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const newConn = !first && hass && hass.connection !== this._conn;
    this._conn = hass ? hass.connection : null;
    if (first && this.isConnected) {
      this._subscribe();
      this._load();
    } else if (newConn && this.isConnected) {
      // New websocket connection object: retry at once what failed.
      this._retryNow();
    }
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    // Units of ~50 px. Fill: about the visible viewport.
    if (this._heightMode === "fill") {
      const vh = typeof window !== "undefined" && window.innerHeight ? window.innerHeight : 800;
      return Math.max(5, Math.ceil(vh / 50));
    }
    if (typeof this._heightMode === "number") {
      return Math.max(3, 5 + Math.ceil((this._heightMode - CHART_HEIGHT) / 50));
    }
    return 5;
  }

  getGridOptions() {
    // The card's height depends on its content (empty state, stored brew,
    // live view, custom chart height); a fixed row count let a stored brew
    // overflow into the sections below. Let the row follow the card.
    return { columns: 12, min_columns: 6, rows: "auto", min_rows: 4 };
  }

  // ------------------------------------------------------- chart height

  _setHeightMode(mode) {
    const wasFill = this._heightMode === "fill";
    this._heightMode = mode;
    if (mode !== "fill") this._unwatchViewport();
    // Fill keeps its fitted height across setConfig calls (no flicker).
    if (!(mode === "fill" && wasFill)) {
      this._applyChartHeight(typeof mode === "number" ? mode : CHART_HEIGHT);
    }
    if (mode === "fill" && this.isConnected) {
      this._watchViewport();
      this._scheduleFit();
    }
  }

  _applyChartHeight(h) {
    const changed = h !== this._chartH;
    this._chartH = h;
    if (this._chartBox) this._chartBox.style.height = `${h}px`;
    if (this._card) this._card.classList.toggle("fill", this._heightMode === "fill");
    return changed;
  }

  // Fill mode: grow the chart so the card ends at the bottom of the visible
  // viewport. Only what lies above the chart (card offset, head, stats,
  // legend) and the card's bottom padding count, so the chart's own height
  // never feeds back into the result.
  _fit() {
    this._fitRaf = 0;
    if (this._heightMode !== "fill" || !this._chartBox || !this.isConnected) return;
    const box = this._chartBox.getBoundingClientRect();
    if (!box.width && !box.height) return; // hidden (e.g. conditional section)
    // Document coordinates: window scrolling does not change the result.
    const top = box.top + (window.scrollY || 0);
    // Layout viewport: pinch zoom must not shrink the chart.
    const vh = window.innerHeight;
    const padBottom = parseFloat(getComputedStyle(this._card).paddingBottom) || 0;
    const h = Math.max(CHART_HEIGHT, Math.floor(vh - top - padBottom - FILL_MARGIN));
    if (this._applyChartHeight(h)) this._renderChart();
  }

  _scheduleFit() {
    if (this._heightMode !== "fill" || this._fitRaf) return;
    this._fitRaf = requestAnimationFrame(() => this._fit());
  }

  _watchViewport() {
    if (this._fillObs) return;
    window.addEventListener("resize", this._onViewport);
    window.addEventListener("orientationchange", this._onViewport);
    // The card's own size (head/stats change, section shown) and the page
    // layout above it (moves the card) both trigger a refit.
    this._fillObs = new ResizeObserver(() => this._scheduleFit());
    this._fillObs.observe(this);
    this._fillObs.observe(document.documentElement);
  }

  _unwatchViewport() {
    if (this._fitRaf) cancelAnimationFrame(this._fitRaf);
    this._fitRaf = 0;
    if (!this._fillObs) return;
    window.removeEventListener("resize", this._onViewport);
    window.removeEventListener("orientationchange", this._onViewport);
    this._fillObs.disconnect();
    this._fillObs = null;
  }

  connectedCallback() {
    this._build();
    if (this._hass) {
      this._subscribe();
      this._load();
    }
    this._resize = new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (w && w !== this._width) {
        this._width = w;
        this._renderChart();
      }
    });
    this._resize.observe(this._chartBox);
    if (this._heightMode === "fill") {
      this._watchViewport();
      this._scheduleFit();
    }
  }

  disconnectedCallback() {
    this._unsubscribe();
    this._cancelRetry();
    if (this._resize) this._resize.disconnect();
    this._unwatchViewport();
    this._setLive(null);
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    this._pending = { stats: false, chart: false };
    clearTimeout(this._noteTimer);
    this._note = null;
  }

  _t(key) {
    return STRINGS[lang(this._hass)][key];
  }

  // ------------------------------------------------------------------ data

  _subscribe() {
    if (!this._hass) return;
    const conn = this._hass.connection;
    const target = targetOf(this._config);
    // The websocket library resubscribes by itself after a reconnect;
    // anything saved or corrected meanwhile was missed: reload then.
    if (this._readyConn !== conn) {
      if (this._readyConn) this._readyConn.removeEventListener("ready", this._onReady);
      conn.addEventListener("ready", this._onReady);
      this._readyConn = conn;
    }
    // Each subscription separately: only a missing one is (re)made.
    if (!this._unsub) {
      this._unsub = this._track("_unsub", conn.subscribeMessage((msg) => this._onUpdate(msg), {
        type: `${DOMAIN}/subscribe_brews`,
        ...target,
      }));
    }
    if (!this._liveUnsub) {
      this._liveIdles = 0;
      this._liveUnsub = this._track("_liveUnsub", conn.subscribeMessage((msg) => this._onLive(msg), {
        type: `${DOMAIN}/subscribe_live`,
        ...target,
      }));
    }
  }

  _track(key, promise) {
    // A failed subscribe frees its slot and is retried with backoff.
    const tracked = Promise.resolve(promise).then(
      (unsub) => {
        if (this[key] === tracked) this._retrySucceeded();
        return unsub;
      },
      () => {
        if (this[key] === tracked) {
          this[key] = null;
          this._scheduleRetry();
        }
        return null;
      }
    );
    return tracked;
  }

  _scheduleRetry() {
    if (this._retryTimer || !this.isConnected) return;
    const delay = RETRY_MS[Math.min(this._retryCount, RETRY_MS.length - 1)];
    this._retryCount++;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this._retry();
    }, delay);
  }

  _retrySucceeded() {
    // Backoff starts over once nothing is failing any more.
    if (!this._retryTimer && this._unsub && this._liveUnsub && this._brews !== null) {
      this._retryCount = 0;
    }
  }

  _retryNow() {
    this._cancelRetry();
    this._retryCount = 0;
    this._retry();
  }

  _retry() {
    if (!this.isConnected || !this._hass) return;
    const failed = !this._unsub || !this._liveUnsub || this._brews === null;
    if (!failed) return;
    this._subscribe();
    if (this._brews === null) this._load();
  }

  _cancelRetry() {
    clearTimeout(this._retryTimer);
    this._retryTimer = null;
  }

  _unsubscribe() {
    this._cancelRetry();
    this._retryCount = 0;
    if (this._readyConn) {
      this._readyConn.removeEventListener("ready", this._onReady);
      this._readyConn = null;
    }
    for (const key of ["_unsub", "_liveUnsub"]) {
      const pending = this[key];
      this[key] = null;
      if (pending) pending.then((unsub) => unsub && unsub()).catch(() => {});
    }
  }

  // ------------------------------------------------------------------ live

  _onLive(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "live_start") {
      this._setLive({
        startTs: msg.start_ts,
        dose: msg.dose,
        points: (msg.points || []).slice(),
      });
      this._applyLiveValues(msg);
      this._growAxes(true);
      const pts = this._live.points;
      this._live.drawT = pts.length ? pts[pts.length - 1][0] : 0;
      this._hover = null;
      this._render();
    } else if (msg.type === "live_points") {
      // Start missed (wait for the next subscribe), or a replay is showing.
      if (!this._live || this._live.replay) return;
      const pts = msg.points || [];
      for (const p of pts) this._live.points.push(p);
      const phaseChanged = msg.phase !== this._live.phase;
      this._applyLiveValues(msg);
      this._growAxes(false);
      this._schedule(true, pts.length > 0 || phaseChanged);
    } else if (msg.type === "live_end") {
      const live = this._live;
      const wasLive = !!live;
      this._setLive(null);
      if (msg.brew_id && msg.brew) {
        // Show the stored brew at once (live points as a stand-in profile
        // until the stored one is fetched), no flash of the previous brew.
        const brews = (this._brews || []).filter((b) => b.id !== msg.brew_id);
        this._brews = [msg.brew, ...brews];
        this._cache.set(msg.brew_id, {
          ...msg.brew,
          profile: live ? live.points : [],
          provisional: true,
        });
      } else if (msg.brew_id) {
        this._cache.delete(msg.brew_id);
      } else if (wasLive && msg.reason !== "unloaded") {
        this._showNote(this._t(`discard_${msg.reason}`) || this._t("discarded"));
      }
      // The stored brew, or back to the previous latest one.
      this._index = 0;
      this._render();
      this._load(null);
    } else if (msg.type === "live_ready") {
      const ready = !!msg.ready;
      const dose = ready && msg.dose != null ? msg.dose : null;
      if (ready === this._ready && dose === this._readyDose) return;
      this._ready = ready;
      this._readyDose = dose;
      // Only the empty ready view changes; a live brew or replay goes on.
      if (!this._live) {
        this._hover = null;
        this._render();
      }
    } else if (msg.type === "live_idle") {
      const resubscribed = this._liveIdles++ > 0;
      if (this._live && !this._live.replay) {
        // Missed the end (e.g. websocket reconnect): back to the history.
        this._setLive(null);
        this._index = 0;
        this._cache.clear();
        this._load(null);
      } else if (resubscribed) {
        this._resync();
      }
    }
  }

  _resync() {
    // After a reconnect: brews saved or corrected meanwhile were missed.
    this._cache.clear();
    const keepId = this._index > 0 && this._brews ? this._brews[this._index]?.id : null;
    this._load(keepId || null);
  }

  _applyLiveValues(msg) {
    const l = this._live;
    l.weight = msg.weight;
    l.flow = msg.flow;
    l.timer = msg.timer;
    l.elapsed = msg.elapsed || 0;
    l.phase = msg.phase || "brewing";
    l.stop = msg.stop;
    l.recvAt = performance.now();
  }

  _setLive(live) {
    // Any change (real brew starting, end, card removed) ends a replay.
    clearInterval(this._replayTimer);
    this._replayTimer = null;
    this._live = live;
    if (!live) this._liveAxes = null;
    if (live && !this._clock) {
      // Elapsed time runs on locally between messages (text only).
      this._clock = setInterval(() => this._schedule(true, false), 100);
    } else if (!live && this._clock) {
      clearInterval(this._clock);
      this._clock = null;
    }
  }

  _liveElapsed() {
    const l = this._live;
    if (!l) return 0;
    if (l.phase === "tail" && l.stop != null) return l.stop;
    if (l.phase === "tail") return l.elapsed;
    // Between messages, run on locally; never more than 1 s ahead.
    const ahead = Math.min(1, (performance.now() - (l.recvAt || 0)) / 1000);
    return l.elapsed + Math.max(0, ahead);
  }

  // Grow-only targets; the drawn range eases towards them (see _frame).
  _growAxes(snap) {
    const l = this._live;
    const pts = this._display(l.points); // the displayed (smoothed) values
    let fMax = 0;
    let wMax = 0;
    const tLast = pts.length ? pts[pts.length - 1][0] : 0;
    for (const p of pts) {
      if (p[2] > fMax) fMax = p[2];
      if (p[1] > wMax) wMax = p[1];
    }
    const now = this._liveNow();
    if (now.weight > wMax) wMax = now.weight;
    if (now.flow > fMax) fMax = now.flow;
    const tSpan = Math.max(tLast, l.elapsed || 0) + 3;
    const tNeed = Math.max(LIVE_MIN.t, Math.ceil(tSpan / LIVE_T_STEP) * LIVE_T_STEP);
    const fNeed = axis(fMax * 1.1, LIVE_MIN.f, 4).max;
    const wNeed = axis(wMax * 1.05, LIVE_MIN.w, 4).max;
    const prev = this._liveAxes;
    const grow = (key, need) => {
      const target = Math.max(need, prev ? prev[key].target : 0);
      const cur = snap || !prev || reducedMotion() ? target : prev[key].cur;
      return { cur, target };
    };
    this._liveAxes = { t: grow("t", tNeed), f: grow("f", fNeed), w: grow("w", wNeed) };
  }

  _schedule(stats, chart) {
    this._pending.stats = this._pending.stats || stats;
    this._pending.chart = this._pending.chart || chart;
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => this._frame());
  }

  _frame() {
    this._raf = 0;
    const { stats, chart } = this._pending;
    this._pending = { stats: false, chart: false };
    if (!this._live) return;
    if (stats) this._renderLiveStats();
    let animating = this._advanceReveal();
    const axes = this._liveAxes;
    if (axes) {
      for (const a of Object.values(axes)) {
        if (a.cur < a.target) {
          a.cur += (a.target - a.cur) * 0.2;
          if (a.target - a.cur < a.target * 0.002) a.cur = a.target;
          animating = true;
        }
      }
    }
    if (chart || animating) this._renderChart();
    if (animating) this._schedule(false, true);
  }

  // Moves the drawn end of the live line towards the newest point; true
  // while it still has to move (keeps the frame loop running).
  _advanceReveal() {
    const l = this._live;
    const pts = l.points;
    const target = pts.length ? pts[pts.length - 1][0] : 0;
    const now = performance.now();
    if (l.drawT == null || reducedMotion()) l.drawT = target;
    if (l.drawT >= target) {
      l.lastFrame = null; // idle: next batch starts moving from here
      return false;
    }
    const dt = l.lastFrame == null ? 0 : Math.min(0.1, (now - l.lastFrame) / 1000);
    l.lastFrame = now;
    const lag = target - l.drawT;
    const speed = 1 + Math.max(0, lag - REVEAL_LAG) * 3;
    l.drawT = Math.min(target, Math.max(l.drawT + dt * speed, target - REVEAL_MAX_LAG));
    return true;
  }

  // Displayed points (see displayProfile), cached per points array; live
  // and replay arrays only grow, so their length tells when to recompute.
  _display(points) {
    const hit = this._displayCache.get(points);
    if (hit && hit.length === points.length && hit.smoothing === this._smoothing) {
      return hit.out;
    }
    const out = displayProfile(points, this._smoothing);
    this._displayCache.set(points, { length: points.length, smoothing: this._smoothing, out });
    return out;
  }

  // Current live flow and weight as displayed: the smoothed values at the
  // newest point (the scale's values with smoothing 0 or before any point).
  _liveNow() {
    const l = this._live;
    if (!l) return { weight: 0, flow: 0 };
    if (this._smoothing && l.points.length) {
      const pts = this._display(l.points);
      const last = pts[pts.length - 1];
      return { weight: last[1], flow: last[2] };
    }
    return { weight: l.weight, flow: l.flow };
  }

  // Ready view: "Prepare brew" is on and no brew (or replay) is showing.
  _readyView() {
    return this._ready && !this._live;
  }

  // Live points up to the drawn end, with an interpolated end point.
  _liveVisible(pts) {
    const l = this._live;
    const drawT = l.drawT == null ? Infinity : l.drawT;
    let k = pts.length;
    while (k > 0 && pts[k - 1][0] > drawT) k--;
    const out = pts.slice(0, k);
    if (k > 0 && k < pts.length) {
      const a = pts[k - 1];
      const b = pts[k];
      const r = (drawT - a[0]) / (b[0] - a[0] || 1);
      out.push([drawT, a[1] + (b[1] - a[1]) * r, a[2] + (b[2] - a[2]) * r]);
    }
    return out;
  }

  // ---------------------------------------------------------------- replay

  _toggleReplay() {
    if (this._live && this._live.replay) {
      this._setLive(null);
      this._render();
      return;
    }
    const brew = this._currentBrew();
    const src = (brew && brew.profile) || [];
    if (this._live || !src.length) return;
    this._setLive({
      replay: true,
      source: src,
      cursor: 0,
      duration: brew.duration,
      dose: brew.dose,
      points: [],
      startedAt: performance.now(),
    });
    this._applyLiveValues({ weight: 0, flow: 0, elapsed: 0, phase: "brewing", stop: null });
    this._growAxes(true);
    this._live.drawT = 0;
    this._hover = null;
    this._render();
    this._replayTimer = setInterval(() => this._replayTick(), REPLAY_BATCH_MS);
  }

  _replayTick() {
    const l = this._live;
    if (!l || !l.replay) return;
    const t = (performance.now() - l.startedAt) / 1000;
    const src = l.source;
    let added = 0;
    while (l.cursor < src.length && src[l.cursor][0] <= t) {
      l.points.push(src[l.cursor++]);
      added++;
    }
    const last = l.points[l.points.length - 1];
    const inTail = l.duration != null && t >= l.duration;
    const phaseBefore = l.phase;
    this._applyLiveValues({
      weight: last ? last[1] : 0,
      flow: last ? last[2] : 0,
      elapsed: inTail ? l.duration : t,
      phase: inTail ? "tail" : "brewing",
      stop: inTail ? l.duration : null,
    });
    this._growAxes(false);
    this._schedule(true, added > 0 || phaseBefore !== l.phase);
    // Done once every point is out and drawn: back to the stored view.
    if (l.cursor >= src.length && l.drawT >= src[src.length - 1][0]) {
      this._setLive(null);
      this._render();
    }
  }

  _showNote(text) {
    clearTimeout(this._noteTimer);
    this._note = text;
    this._noteTimer = setTimeout(() => {
      this._note = null;
      this._render();
    }, NOTE_MS);
  }

  _onUpdate(msg) {
    // A new brew or a dose correction: drop the cached copy and reload.
    if (msg && msg.last) this._cache.delete(msg.last.id);
    const viewingLatest = this._index === 0;
    const currentId = this._brews && this._brews[this._index]?.id;
    this._load(viewingLatest ? null : currentId);
  }

  async _load(keepId) {
    if (!this._hass) return;
    const token = ++this._loadToken;
    try {
      const res = await this._hass.callWS({
        type: `${DOMAIN}/brews`,
        ...targetOf(this._config),
      });
      if (token !== this._loadToken) return;
      this._brews = res.brews || [];
      this._error = null;
      this._retrySucceeded();
      const idx = keepId ? this._brews.findIndex((b) => b.id === keepId) : 0;
      this._index = idx > 0 ? idx : 0;
      await this._ensureBrew();
    } catch (err) {
      if (token !== this._loadToken) return;
      this._error = (err && err.message) || this._t("error");
      this._brews = null;
      this._scheduleRetry();
    }
    this._render();
  }

  async _ensureBrew() {
    const summary = this._brews && this._brews[this._index];
    if (!summary) return;
    const cached = this._cache.get(summary.id);
    if (cached && !cached.provisional) return;
    const res = await this._hass.callWS({
      type: `${DOMAIN}/brew`,
      brew_id: summary.id,
      ...targetOf(this._config),
    });
    if (res.brew) this._cache.set(summary.id, res.brew);
  }

  async _step(delta) {
    if (!this._brews) return;
    const next = this._index + delta;
    if (next < 0 || next >= this._brews.length) return;
    this._index = next;
    this._hover = null;
    this._render();
    try {
      await this._ensureBrew();
    } catch (err) {
      this._error = (err && err.message) || this._t("error");
    }
    this._render();
  }

  // ---------------------------------------------------------------- render

  _build() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { height: 100%; box-sizing: border-box; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
        .head { display: flex; align-items: center; gap: 8px; }
        .titles { flex: 1; min-width: 0; }
        .title { font-size: 1.1rem; font-weight: 500; color: var(--primary-text-color); line-height: 1.3; }
        .when { font-size: 0.85rem; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .nav { display: flex; align-items: center; gap: 2px; color: var(--secondary-text-color); font-size: 0.8rem; font-variant-numeric: tabular-nums; }
        button { all: unset; cursor: pointer; width: 36px; height: 36px; display: grid; place-items: center; border-radius: 50%; color: var(--primary-text-color); }
        button:hover { background: var(--secondary-background-color); }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
        button[disabled] { cursor: default; color: var(--disabled-text-color, var(--secondary-text-color)); opacity: 0.4; background: none; }
        button svg { width: 22px; height: 22px; fill: currentColor; }
        .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
        .stat .label { font-size: 0.75rem; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
        .stat .value { font-size: 1.35rem; color: var(--primary-text-color); font-variant-numeric: tabular-nums; white-space: nowrap; }
        .stat .unit { font-size: 0.85rem; color: var(--secondary-text-color); margin-left: 2px; }
        .stat .note { font-size: 0.7rem; color: var(--secondary-text-color); }
        .legend { display: flex; gap: 16px; font-size: 0.8rem; color: var(--secondary-text-color); align-items: center; min-height: 1.2em; }
        .legend .key { display: inline-flex; align-items: center; gap: 6px; }
        .legend .swatch { width: 14px; height: 0; border-top: 2px solid; }
        .legend .flow .swatch { border-color: var(--primary-color); }
        .legend .weight .swatch { border-color: var(--secondary-text-color); border-top-style: dashed; }
        .legend .readout { margin-left: auto; font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
        .chart { position: relative; height: ${CHART_HEIGHT}px; touch-action: pan-y; }
        .chart svg { display: block; overflow: visible; }
        .message { color: var(--secondary-text-color); padding: 24px 0; text-align: center; }
        .title { display: flex; align-items: center; gap: 8px; }
        .nav.replaying .prev, .nav.replaying .next, .nav.replaying .pos { display: none; }
        .nav .replay svg { width: 20px; height: 20px; }
        .badge { display: inline-flex; align-items: center; gap: 6px; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--primary-color); padding: 1px 8px 1px 6px; border-radius: 999px; border: 1px solid currentColor; line-height: 1.4; }
        .badge .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: pulse 1.6s ease-in-out infinite; }
        .badge.parked { color: var(--warning-color, var(--secondary-text-color)); }
        .badge.parked .dot, .badge.tail .dot { animation: none; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
        @media (prefers-reduced-motion: reduce) { .badge .dot { animation: none; } }
        .note { font-size: 0.85rem; color: var(--secondary-text-color); background: var(--secondary-background-color); border-radius: 8px; padding: 6px 10px; }
        .stats.live .stat.big .value { font-size: 2rem; font-weight: 500; line-height: 1.15; }
        .stats.live .stat.big .unit { font-size: 0.95rem; }
        .stats.live .stat.flow .value { color: var(--primary-color); }
        ha-card.fill .stats.live .stat.big .value { font-size: 2.75rem; }
        ha-card.fill .stats.live .stat.big .unit { font-size: 1.1rem; }
        ha-card.fill .stats.live .stat.big .label { font-size: 0.85rem; }
        .hidden { display: none !important; }
      </style>
      <ha-card>
        <div class="head">
          <div class="titles">
            <div class="title"><span class="title-text"></span><span class="badge hidden"><span class="dot"></span><span class="badge-text"></span></span></div>
            <div class="when"></div>
          </div>
          <div class="nav">
            <button class="replay"><svg viewBox="0 0 24 24"><path d="${ICON_PLAY}"></path></svg></button>
            <button class="prev"><svg viewBox="0 0 24 24"><path d="${ICON_LEFT}"></path></svg></button>
            <span class="pos"></span>
            <button class="next"><svg viewBox="0 0 24 24"><path d="${ICON_RIGHT}"></path></svg></button>
          </div>
        </div>
        <div class="message hidden"></div>
        <div class="note hidden" role="status"></div>
        <div class="stats"></div>
        <div class="legend"></div>
        <div class="chart"></div>
      </ha-card>`;
    const $ = (s) => this.shadowRoot.querySelector(s);
    this._els = {
      title: $(".title-text"),
      badge: $(".badge"),
      badgeText: $(".badge-text"),
      note: $(".note"),
      when: $(".when"),
      nav: $(".nav"),
      prev: $(".prev"),
      replay: $(".replay"),
      replayIcon: $(".replay path"),
      next: $(".next"),
      pos: $(".pos"),
      message: $(".message"),
      stats: $(".stats"),
      legend: $(".legend"),
    };
    this._chartBox = $(".chart");
    this._card = $("ha-card");
    this._applyChartHeight(this._chartH);
    // Brews are newest first: "older" moves up the list.
    this._els.prev.addEventListener("click", () => this._step(1));
    this._els.next.addEventListener("click", () => this._step(-1));
    this._els.replay.addEventListener("click", () => this._toggleReplay());
    this._chartBox.addEventListener("pointermove", (ev) => this._onPointer(ev));
    this._chartBox.addEventListener("pointerleave", () => {
      this._hover = null;
      this._renderChart();
      this._renderLegend();
    });
  }

  _fmt(value, digits) {
    if (value === null || value === undefined) return "–";
    return Number(value).toLocaleString(lang(this._hass), {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  _render() {
    if (!this._built) this._build();
    const e = this._els;
    e.title.textContent = this._config.title || this._t("title");
    e.prev.title = this._t("older");
    e.prev.setAttribute("aria-label", this._t("older"));
    e.next.title = this._t("newer");
    e.next.setAttribute("aria-label", this._t("newer"));
    e.note.textContent = this._note || "";
    const readyView = this._readyView();
    e.note.classList.toggle("hidden", !this._note || !!this._live || readyView);
    e.badge.classList.toggle("hidden", !this._live);
    e.stats.classList.toggle("live", !!this._live || readyView);
    const replaying = !!(this._live && this._live.replay);
    const replayLabel = this._t(replaying ? "replay_stop" : "replay_start");
    e.replay.title = replayLabel;
    e.replay.setAttribute("aria-label", replayLabel);
    e.replayIcon.setAttribute("d", replaying ? ICON_STOP : ICON_PLAY);
    e.nav.classList.toggle("replaying", replaying);
    if (this._live || readyView) {
      e.message.classList.add("hidden");
      // A real live brew has no navigation; a replay keeps its stop button.
      e.nav.classList.toggle("hidden", !replaying);
      for (const el of [e.stats, e.legend, this._chartBox]) el.classList.remove("hidden");
      this._hover = null;
      this._renderLegend();
      this._renderLiveStats();
      this._renderChart();
      return;
    }
    this._lastStatsHtml = null;

    let message = null;
    if (this._error) message = this._error;
    else if (this._brews === null) message = this._t("loading");
    else if (this._brews.length === 0) message = this._t("empty");

    const showData = message === null;
    e.message.textContent = message || "";
    e.message.classList.toggle("hidden", showData);
    for (const el of [e.stats, e.legend, this._chartBox, e.nav]) {
      el.classList.toggle("hidden", !showData);
    }
    if (!showData) {
      e.when.textContent = "";
      return;
    }

    const summary = this._brews[this._index];
    const total = this._brews.length;
    e.pos.textContent = `${total - this._index} / ${total}`;
    e.prev.disabled = this._index >= total - 1;
    e.replay.disabled = !((this._currentBrew() || {}).profile || []).length;
    e.next.disabled = this._index <= 0;
    e.when.textContent = new Date(summary.start).toLocaleString(lang(this._hass), {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });

    const stat = (label, value, unit, note) => `
      <div class="stat">
        <div class="label">${label}</div>
        <div class="value">${value}${unit ? `<span class="unit">${unit}</span>` : ""}</div>
        ${note ? `<div class="note">${note}</div>` : ""}
      </div>`;
    const ratio = summary.ratio == null ? "–" : `1:${this._fmt(summary.ratio, 1)}`;
    e.stats.innerHTML =
      stat(
        this._t("dose"),
        this._fmt(summary.dose, 1),
        summary.dose == null ? "" : "g",
        summary.dose_source === "manual" ? this._t("corrected") : ""
      ) +
      stat(this._t("yield"), this._fmt(summary.yield, 1), "g") +
      stat(this._t("ratio"), ratio, "") +
      stat(this._t("time"), this._fmt(summary.duration, 1), "s");

    this._renderLegend();
    this._renderChart();
  }

  _renderLiveStats() {
    const e = this._els;
    const l = this._live;
    if (!e || (!l && !this._readyView())) return;
    let elapsed = 0;
    let now = { weight: 0, flow: 0 };
    let dose = null;
    if (l) {
      const phase = l.phase || "brewing";
      e.badgeText.textContent = this._t(l.replay ? "replay" : "live");
      e.badge.classList.toggle("parked", phase === "parked");
      e.badge.classList.toggle("tail", phase === "tail");
      e.when.textContent = this._t(phase) || "";
      elapsed = this._liveElapsed();
      now = this._liveNow();
      dose = l.dose;
    } else {
      e.when.textContent = this._t("ready");
      dose = this._readyDose;
    }
    const stat = (cls, label, value, unit) => `
      <div class="stat ${cls}">
        <div class="label">${label}</div>
        <div class="value">${value}${unit ? `<span class="unit">${unit}</span>` : ""}</div>
      </div>`;
    const html =
      stat("big", this._t("time"), this._fmt(elapsed, 1), "s") +
      stat("big flow", this._t("flow_rate"), this._fmt(now.flow, 1), "g/s") +
      stat("big", this._t("weight"), this._fmt(now.weight, 1), "g") +
      stat("", this._t("dose"), this._fmt(dose, 1), dose == null ? "" : "g");
    if (html !== this._lastStatsHtml) {
      e.stats.innerHTML = html;
      this._lastStatsHtml = html;
    }
  }

  _renderLegend() {
    const e = this._els;
    if (!e) return;
    let readout = "";
    if (this._hover) {
      const [t, w, f] = this._hover;
      readout = `${this._fmt(t, 1)} s · ${this._fmt(f, 1)} g/s · ${this._fmt(w, 1)} g`;
    }
    e.legend.innerHTML = `
      <span class="key flow"><span class="swatch"></span>${this._t("flow")} (g/s)</span>
      <span class="key weight"><span class="swatch"></span>${this._t("weight")} (g)</span>
      <span class="readout">${readout}</span>`;
  }

  _currentBrew() {
    if (this._live) {
      const l = this._live;
      return { profile: l.points, duration: l.phase === "tail" ? l.stop : null, live: true };
    }
    const summary = this._brews && this._brews[this._index];
    return summary ? this._cache.get(summary.id) : null;
  }

  // What the chart draws: displayed points (live: up to the drawn end);
  // the ready view is an empty live chart.
  _chartBrew() {
    if (this._readyView()) return { profile: [], duration: null, live: true };
    const brew = this._currentBrew();
    if (!brew) return null;
    const pts = this._display(brew.profile || []);
    return { ...brew, profile: brew.live ? this._liveVisible(pts) : pts };
  }

  _geometry(brew) {
    const pts = brew.profile || [];
    const width = this._width || this._chartBox.clientWidth || 300;
    const plotW = Math.max(10, width - PAD.left - PAD.right);
    const tCount = Math.max(2, Math.floor(plotW / 70));
    let tAxis;
    let fAxis;
    let wAxis;
    let tMax;
    let fMax;
    let wMax;
    if (brew.live) {
      // Ticks from the target range, drawn on the (easing) current range.
      // Ready view (no live axes yet): the range a live brew starts with.
      const start = (v) => ({ cur: v, target: v });
      const a = this._liveAxes || { t: start(LIVE_MIN.t), f: start(LIVE_MIN.f), w: start(LIVE_MIN.w) };
      const keep = (ax, max) => ({ ...ax, ticks: ax.ticks.filter((v) => v <= max + 1e-6) });
      tMax = a.t.cur;
      fMax = a.f.cur;
      wMax = a.w.cur;
      tAxis = keep(axis(a.t.target, LIVE_MIN.t, tCount), tMax);
      fAxis = keep(axis(a.f.target, LIVE_MIN.f, 4), fMax);
      wAxis = keep(axis(a.w.target, LIVE_MIN.w, 4), wMax);
    } else {
      let fPeak = 0;
      let wPeak = 0;
      for (const p of pts) {
        if (p[2] > fPeak) fPeak = p[2];
        if (p[1] > wPeak) wPeak = p[1];
      }
      tAxis = axis(pts.length ? pts[pts.length - 1][0] : 0, 5, tCount);
      fAxis = axis(fPeak, 1, 4);
      wAxis = axis(wPeak, 10, 4);
      tMax = tAxis.max;
      fMax = fAxis.max;
      wMax = wAxis.max;
    }
    const plotH = this._chartH - PAD.top - PAD.bottom;
    return {
      pts,
      width,
      tAxis,
      fAxis,
      wAxis,
      tMax,
      fMax,
      wMax,
      plotW,
      plotH,
      x: (t) => PAD.left + (t / tMax) * plotW,
      yF: (f) => PAD.top + plotH - (Math.max(0, f) / fMax) * plotH,
      yW: (w) => PAD.top + plotH - (Math.max(0, w) / wMax) * plotH,
    };
  }

  _renderChart() {
    const box = this._chartBox;
    if (!box) return;
    box.textContent = "";
    const brew = this._chartBrew();
    // Live: draw the empty axes right away, the lines grow into them.
    if (!brew || (!(brew.profile || []).length && !brew.live)) return;
    const g = this._geometry(brew);
    const svg = svgEl("svg", {
      width: g.width,
      height: this._chartH,
      viewBox: `0 0 ${g.width} ${this._chartH}`,
      role: "img",
      "aria-label": `${this._t("flow")} / ${this._t("weight")}`,
    });
    const text = (x, y, anchor, label) => {
      const el = svgEl("text", {
        x,
        y,
        "text-anchor": anchor,
        "font-size": 11,
        fill: "var(--secondary-text-color)",
      });
      el.textContent = label;
      svg.appendChild(el);
    };

    // Grid + left (flow) axis
    for (const f of g.fAxis.ticks) {
      const y = g.yF(f);
      svg.appendChild(
        svgEl("line", {
          x1: PAD.left,
          x2: PAD.left + g.plotW,
          y1: y,
          y2: y,
          stroke: "var(--divider-color)",
          "stroke-width": 1,
        })
      );
      text(PAD.left - 6, y + 4, "end", this._fmt(f, g.fAxis.step < 1 ? 1 : 0));
    }
    // Right (weight) axis: own scale, labels only
    for (const w of g.wAxis.ticks) {
      text(PAD.left + g.plotW + 6, g.yW(w) + 4, "start", this._fmt(w, 0));
    }
    // Time axis
    for (const t of g.tAxis.ticks) {
      text(g.x(t), this._chartH - 6, "middle", `${this._fmt(t, 0)}s`);
    }
    // Timer stop marker (the rest is the capture tail)
    if (brew.duration && brew.duration < g.tMax) {
      const xs = g.x(brew.duration);
      svg.appendChild(
        svgEl("line", {
          x1: xs,
          x2: xs,
          y1: PAD.top,
          y2: PAD.top + g.plotH,
          stroke: "var(--secondary-text-color)",
          "stroke-width": 1,
          "stroke-dasharray": "2 3",
          opacity: 0.6,
        })
      );
    }

    const base = PAD.top + g.plotH;
    if (!g.pts.length) {
      box.appendChild(svg);
      return;
    }
    // Same smoothing in the live and the stored view, so the curve does not
    // change shape when a live brew turns into the stored one.
    const line = (yFn, idx) => monotonePath(g.pts.map((p) => [g.x(p[0]), yFn(p[idx])]));
    const flowPath = line(g.yF, 2);
    const first = g.pts[0];
    const last = g.pts[g.pts.length - 1];
    svg.appendChild(
      svgEl("path", {
        d: `${flowPath}L${g.x(last[0]).toFixed(1)},${base}L${g.x(first[0]).toFixed(1)},${base}Z`,
        fill: "var(--primary-color)",
        "fill-opacity": 0.12,
        stroke: "none",
      })
    );
    svg.appendChild(
      svgEl("path", {
        d: line(g.yW, 1),
        fill: "none",
        stroke: "var(--secondary-text-color)",
        "stroke-width": 1.5,
        "stroke-dasharray": "4 3",
        "stroke-linejoin": "round",
      })
    );
    svg.appendChild(
      svgEl("path", {
        d: flowPath,
        fill: "none",
        stroke: "var(--primary-color)",
        "stroke-width": 2.25,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      })
    );

    if (brew.live) {
      // "Now" marker at the newest flow point.
      svg.appendChild(
        svgEl("circle", {
          cx: g.x(last[0]),
          cy: g.yF(last[2]),
          r: 4,
          fill: "var(--primary-color)",
          stroke: "var(--card-background-color, var(--ha-card-background))",
          "stroke-width": 2,
        })
      );
    }
    if (this._hover && !brew.live) {
      const [t, w, f] = this._hover;
      const xh = g.x(t);
      svg.appendChild(
        svgEl("line", {
          x1: xh,
          x2: xh,
          y1: PAD.top,
          y2: base,
          stroke: "var(--primary-text-color)",
          "stroke-width": 1,
          opacity: 0.35,
        })
      );
      svg.appendChild(
        svgEl("circle", {
          cx: xh,
          cy: g.yF(f),
          r: 4,
          fill: "var(--primary-color)",
          stroke: "var(--card-background-color, var(--ha-card-background))",
          "stroke-width": 2,
        })
      );
      svg.appendChild(
        svgEl("circle", {
          cx: xh,
          cy: g.yW(w),
          r: 3,
          fill: "var(--secondary-text-color)",
        })
      );
    }
    box.appendChild(svg);
  }

  _onPointer(ev) {
    if (this._live || this._readyView()) return;
    const brew = this._chartBrew();
    if (!brew || !(brew.profile || []).length) return;
    const g = this._geometry(brew);
    const rect = this._chartBox.getBoundingClientRect();
    const t = ((ev.clientX - rect.left - PAD.left) / g.plotW) * g.tMax;
    let best = g.pts[0];
    for (const p of g.pts) {
      if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    }
    if (this._hover === best) return;
    this._hover = best;
    this._renderChart();
    this._renderLegend();
  }
}

class DifluidBrewCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const s = STRINGS[lang(this._hass)];
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) =>
        ({ device_id: s.device, chart_height: s.chart_height, flow_smoothing: s.flow_smoothing }[
          schema.name
        ] || s.card_title);
      this._form.computeHelper = (schema) =>
        ({ chart_height: s.chart_height_helper, flow_smoothing: s.flow_smoothing_helper }[schema.name]);
      this._form.addEventListener("value-changed", (ev) => {
        const value = { ...ev.detail.value };
        for (const k of Object.keys(value)) {
          if (value[k] === "" || value[k] === undefined || value[k] === null) delete value[k];
        }
        // "320" from the text field -> 320 in the YAML.
        if (typeof value.chart_height === "string" && /^\s*\d+\s*$/.test(value.chart_height)) {
          value.chart_height = Number(value.chart_height);
        }
        this._config = { type: this._config.type, ...value };
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: this._config },
            bubbles: true,
            composed: true,
          })
        );
      });
      this.appendChild(this._form);
    }
    this._form.schema = [
      { name: "device_id", selector: { device: { integration: DOMAIN } } },
      { name: "title", selector: { text: {} } },
      { name: "chart_height", selector: { text: {} } },
      {
        name: "flow_smoothing",
        selector: { number: { min: 0, max: FLOW_SMOOTHING_MAX, step: 0.5, mode: "box", unit_of_measurement: "s" } },
      },
    ];
    this._form.hass = this._hass;
    // The text field shows numbers as strings.
    const data = { ...this._config };
    if (typeof data.chart_height === "number") data.chart_height = String(data.chart_height);
    this._form.data = data;
  }
}

// Pure helpers, for tests (tests/card_flow_test.mjs).
DifluidBrewCard.math = { monotone, smoothedFlow, displayProfile, parseFlowSmoothing };

if (!customElements.get("difluid-brew-card")) {
  customElements.define("difluid-brew-card", DifluidBrewCard);
}
if (!customElements.get("difluid-brew-card-editor")) {
  customElements.define("difluid-brew-card-editor", DifluidBrewCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "difluid-brew-card")) {
  const l = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
  window.customCards.push({
    type: "difluid-brew-card",
    name: STRINGS[l].picker_name,
    description: STRINGS[l].picker_desc,
    preview: false,
    documentationURL: "https://github.com/radecke/ha-difluid-microbalance",
  });
}

console.info(`%c DIFLUID-BREW-CARD %c ${CARD_VERSION} `, "font-weight:600", "");
