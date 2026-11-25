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
 *
 * Smoothed flow (stored brews only): a second, calm flow line. The whole
 * brew is known, so it is centred: single outlier frames (more than
 * TREND_SPIKE_G from the median of their neighbours) are replaced, the
 * weight is fitted non-decreasing (isotonic regression), then flow = slope
 * of a Gaussian-weighted local linear fit (sigma TREND_SIGMA s). Only points
 * from the first drop (FLOW_START_DELTA above the start weight) to the brew
 * end (the stored duration: auto-stop cut, else timer stop) count, and the
 * line spans exactly that range, so the kernel neither invents flow before
 * the drops nor gets pulled down by the drips after the cut.
 *
 * Delete (stored brews only): a bin button next to the navigation asks in
 * the card itself (no window.confirm, which the iOS app's web view may not
 * show) and then calls the delete_brew service; the card moves on to the
 * next older brew (else the newer one, else the empty state). Never in the
 * live, ready or replay view.
 *
 * Detail line (stored brews): bean and the shot metrics the integration
 * computes per brew (time to first drop, peak flow, mean flow and its
 * variation in the main phase); parts that are missing are left out.
 *
 * Target: under the yield (stored brew) or the weight (live, ready) the
 * auto-stop target of that brew in small text; nothing while the auto-stop
 * is off or for brews stored without one. The line is always reserved, so
 * the stats do not change height between the views.
 *
 * Expansion (expand_when_active): while the card is active (ready view, or
 * a real live brew incl. its tail; not a replay) it lifts itself out of its
 * grid slot into a position: fixed layer over the dashboard view (below the
 * app header, inset like the sections view) on an opaque theme backdrop,
 * with the chart filling the height. Its slot keeps the old size, so the
 * grid does not reflow. The surface opens with a clip-path reveal from the
 * old rect, then the content enters; collapsing reverses it and the saved
 * brew fades in. Web Animations, cancellable mid-way; instant with reduced
 * motion and when the card appears while active (after the live
 * subscription's snapshot); never in edit mode or the editor.
 *
 * Pump throttle (0.6.0): for a brew with a known pump relay start the time
 * stat shows the shot time (relay on -> end) and the first drop in the
 * detail line is in shot time too. A brew the throttle followed gets a band
 * under the chart with the pump power as a step line (0-100 %, dashed when
 * only observed: what it would have set), a marker at the target shot time
 * (in profile time: target - relay offset) and "grind finer" under the
 * time when the shot was predicted too fast. Live and stored alike.
 *
 * Pump profiles (0.7.0): the same band shows a profile shot's commanded
 * power (dashed when only observed); the legend names the profile. The
 * target time marker only for the Zielzeit profile (its records alone
 * carry a target time). A profile skipped because the brew was not
 * prepared ("Prepare brew" off at the relay start) is named under the time
 * ("<profile> skipped (not prepared): full power").
 *
 * 0.7.0b3: the pump line, its hover marker and the legend key in the pump
 * colour token (--difluid-pump-color, default yellow); the key dashed like
 * the line when only observed (record "controlled": the pump was not
 * controlled; older records: observe or not applied); the hover readout
 * adds the pump power at that time (from the drawn step line), only when
 * the pump was controlled. No band, key or hover value without a pump
 * power entity (record "pump"; older records: only applied ones). The target time marker keeps the accent colour. The same file
 * registers the tile feature "custom:difluid-profile-preview" (below).
 *
 * 0.7.0b4: the profile preview has no axis labels: chips over the graph
 * name what ends each phase (exits, duration; "target weight" at the end),
 * the phase names under it, fine ticks at the borders, a flow phase's
 * target flow in its hatched area. Standard shows its power setting as
 * − value + (fixed 100 %, greyed out, without a pump entity); every view
 * ends with a Brew Ratio row.
 *
 * 0.7.0b5: built-in profiles by id, their names (and the preview's phase
 * names, sent by the integration) in Home Assistant's language; older
 * records' German labels shown as the built-in's name. The preview's
 * target time and pump power inputs are labelled rows like the ratio
 * row, with more room around the blocks.
 *
 * 0.7.0b6: live points from a backlog (frames held back, then delivered at
 * once) are replaced at their scale time (``replace_from``); the pump
 * profile of a brew shows in its info line.
 *
 * 0.7.0b7: swipe between brews - a quick horizontal flick anywhere on the
 * card does what "older" (finger to the right) / "newer" do, with the same
 * locks; a slow drag on the chart stays the hover readout.
 *
 * 0.7.0b8: the end-of-list spring only when the navigation was open as the
 * finger went down (no spring when a brew started or ended meanwhile).
 */

const CARD_VERSION = "0.7.0";
const DOMAIN = "difluid_scale";

const STRINGS = {
  en: {
    title: "Espresso",
    dose: "Dose",
    yield: "Yield",
    ratio: "Ratio",
    time: "Time",
    flow: "Flow",
    trend: "Smoothed flow",
    weight: "Weight",
    corrected: "corrected",
    older: "Older brew",
    newer: "Newer brew",
    delete: "Delete this brew",
    delete_confirm: "Delete the brew of {when}?",
    delete_confirm_learned: "It is also removed from the auto-stop learning.",
    delete_cancel: "Cancel",
    delete_ok: "Delete",
    delete_error: "Could not delete the brew",
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
    expand_when_active: "Full screen while active",
    expand_when_active_helper: "While \"Prepare brew\" is on or a brew runs, the card expands over the dashboard view and collapses afterwards.",
    ready: "Ready – brew starts soon",
    flow_smoothing: "Flow smoothing",
    flow_smoothing_helper: "Seconds (0–10): flow curve computed from the weight over this window. 0: the scale's raw values. Empty: 3.",
    first_drop: "first drop",
    peak_flow: "peak flow",
    target: "target",
    detail_hint: "Bean · pump profile · time to first drop · peak flow · mean flow ± variation in the main phase",
    pump_profile: "Pump profile",
    pump: "Pump",
    preview_name: "DiFluid pump profile",
    preview_target_weight: "Target weight",
    preview_label_time: "Target time",
    preview_label_power: "Pump power",
    preview_label_ratio: "Brew Ratio",
    preview_off: "off",
    preview_dec_time: "Decrease target shot time",
    preview_inc_time: "Increase target shot time",
    preview_dec_power: "Decrease pump power",
    preview_inc_power: "Increase pump power",
    preview_dec_ratio: "Decrease Brew Ratio",
    preview_inc_ratio: "Increase Brew Ratio",
    grind_finer: "grind finer",
    profile_skipped: "skipped (not prepared): full power",
    profile_skipped_no_tare: "skipped (no tare): full power",
    unreachable_floor: "floor reached",
    unreachable_rate_limit: "corrections limited",
    unreachable_late_trigger: "triggered too late",
    unreachable_trigger_cap: "pump not learned – enable learning",
  },
  de: {
    title: "Espresso",
    dose: "Dosis",
    yield: "Ausbeute",
    ratio: "Verhältnis",
    time: "Zeit",
    flow: "Fluss",
    trend: "Fluss geglättet",
    weight: "Gewicht",
    corrected: "korrigiert",
    older: "Älterer Bezug",
    newer: "Neuerer Bezug",
    delete: "Diesen Bezug löschen",
    delete_confirm: "Bezug vom {when} löschen?",
    delete_confirm_learned: "Er wird auch aus dem Lernstopp entfernt.",
    delete_cancel: "Abbrechen",
    delete_ok: "Löschen",
    delete_error: "Bezug konnte nicht gelöscht werden",
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
    expand_when_active: "Vollbild während aktiv",
    expand_when_active_helper: "Solange \"Bezug vorbereiten\" an ist oder ein Bezug läuft, legt sich die Karte über die Dashboard-Ansicht und kehrt danach zurück.",
    ready: "Bereit – Bezug startet gleich",
    flow_smoothing: "Flussglättung",
    flow_smoothing_helper: "Sekunden (0–10): Flusskurve aus dem Gewicht über dieses Fenster berechnet. 0: Rohwerte der Waage. Leer: 3.",
    first_drop: "erster Tropfen",
    peak_flow: "Spitzenfluss",
    target: "Ziel",
    detail_hint: "Bohne · Pumpenprofil · Zeit bis zum ersten Tropfen · Spitzenfluss · mittlerer Fluss ± Schwankung in der Hauptphase",
    pump_profile: "Pumpenprofil",
    pump: "Pumpe",
    preview_name: "DiFluid Pumpenprofil",
    preview_target_weight: "Zielgewicht",
    preview_label_time: "Zielzeit",
    preview_label_power: "Pumpenleistung",
    preview_label_ratio: "Brew Ratio",
    preview_off: "aus",
    preview_dec_time: "Zielzeit verringern",
    preview_inc_time: "Zielzeit erhöhen",
    preview_dec_power: "Pumpenleistung verringern",
    preview_inc_power: "Pumpenleistung erhöhen",
    preview_dec_ratio: "Brew Ratio verringern",
    preview_inc_ratio: "Brew Ratio erhöhen",
    grind_finer: "feiner mahlen",
    profile_skipped: "übersprungen (nicht vorbereitet): volle Leistung",
    profile_skipped_no_tare: "übersprungen (nicht tariert): volle Leistung",
    unreachable_floor: "Untergrenze erreicht",
    unreachable_rate_limit: "Korrekturen begrenzt",
    unreachable_late_trigger: "zu spät ausgelöst",
    unreachable_trigger_cap: "Pumpe nicht gelernt – Lernen einschalten",
  },
};

const ICON_LEFT = "M15.41,16.58L10.83,12L15.41,7.41L14,6L8,12L14,18L15.41,16.58Z";
const ICON_RIGHT = "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z";
const ICON_PLAY = "M8,5.14V19.14L19,12.14L8,5.14Z";
const ICON_STOP = "M18,18H6V6H18V18Z";
const ICON_DELETE = "M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19M8,9H16V19H8V9M15.5,4L14.5,3H9.5L8.5,4H5V6H19V4H15.5Z"; // mdi:delete-outline
const REPLAY_BATCH_MS = 250; // like the integration's live push
const SVG_NS = "http://www.w3.org/2000/svg";
const CHART_HEIGHT = 200; // default; also the minimum in fill mode
const CHART_HEIGHT_MIN = 100; // bounds for a fixed chart_height (px)
const CHART_HEIGHT_MAX = 2000;
const FILL_MARGIN = 8; // px kept free below the card in fill mode
// Expansion (expand_when_active): fixed layer over the dashboard view.
const EXPAND_Z = 3; // HA's app header is z-index 4, the sections lie below
const EXPAND_INSET = 16; // px around the card when the view padding is unknown
const EXPAND_INSET_MAX = 64; // a measured inset above this is not the padding
const EXPAND_MS = 380; // surface reveal
const EXPAND_EASE = "cubic-bezier(0.2, 0, 0, 1)";
const BACKDROP_MS = 200;
const ENTER_MS = 150; // content after the surface; stats staggered
const ENTER_STAGGER = 50;
const CHART_ENTER_DELAY = 40;
const CHART_ENTER_MS = 260;
const LEAVE_MS = 120; // content out before collapsing
const COLLAPSE_MS = 300;
const COLLAPSE_EASE = "cubic-bezier(0.4, 0, 0.2, 1)";
const RETURN_MS = 150; // saved brew fading in after the collapse
// Ancestors that mean "dashboard edit mode / card editor": never expand.
const EDITOR_TAGS = new Set([
  "hui-card-preview",
  "hui-card-picker",
  "hui-dialog-edit-card",
  "hui-card-edit-mode",
  "hui-card-options",
  "hui-dialog-edit-section",
  "hui-section-edit-mode",
]);
const PAD = { left: 36, right: 40, top: 10, bottom: 24 };
const BAND_H = 26; // pump power band under the plot (gap + band)
// Pump power line, its hover marker and legend key: a theme token, yellow
// by default.
const PUMP_COLOR = "var(--difluid-pump-color, var(--yellow-color, #fdd835))";
const BAND_GAP = 6;
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
// Smoothed flow line of stored brews (see the header).
const TREND_SIGMA = 2.5; // s, Gaussian kernel of the local slope
const TREND_SPIKE_K = 2; // neighbours on each side for the outlier median
const TREND_SPIKE_G = 1.0; // g away from that median: outlier frame
// Swipe between brews (0.7.0b7): a quick, mostly horizontal flick with a
// finger or pen anywhere on the card (never with a mouse: there dragging
// reads values or selects text). A slow drag or a hold on the chart stays
// the hover readout.
// SWIPE_MIN_PX: well above finger jitter on a tap (~10 px) and about a
// sixth of a phone-width card, so a readout drag ending in a twitch is no
// swipe.
// SWIPE_MIN_SPEED (px/ms) over the last SWIPE_SPEED_MS before lifting the
// finger: a flick is fast as it leaves the glass (0.8-3 px/ms), even after
// resting first (70 px in 80 ms = 0.88); a quick readout drag moves evenly
// at up to ~0.65 px/ms (200 px in 300 ms) and must not switch. 80 ms is a
// few pointer samples (60-120 Hz) and no more than a flick itself lasts.
// SWIPE_MAX_MS: a hold longer than this is a readout, whatever follows.
// SWIPE_RATIO: |dx| > 2 |dy| (within ~27 degrees of horizontal), so a
// scroll (vertical, handed to the browser by touch-action) never counts.
const SWIPE_MIN_PX = 60;
const SWIPE_MIN_SPEED = 0.8;
const SWIPE_SPEED_MS = 80;
const SWIPE_MAX_MS = 800;
const SWIPE_RATIO = 2;
// Clicks right after a swipe (its pointerup lands on a button) are dropped:
// a swipe never presses "delete" or "replay".
const SWIPE_CLICK_GUARD_MS = 400;
const SWIPE_SLIDE_PX = 24; // the new brew slides in from this far
const SWIPE_ANIM_MS = 180;
const SWIPE_BOUNCE_PX = 10; // end of the list: a small spring, no change

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

// Replace single outlier frames by the median of their neighbourhood. The
// window is symmetric (shrunk at the edges), so a steady rise is left alone.
function despike(values, k = TREND_SPIKE_K, limit = TREND_SPIKE_G) {
  const n = values.length;
  return values.map((v, i) => {
    const kk = Math.min(k, i, n - 1 - i);
    if (kk < 1) return v;
    const win = values.slice(i - kk, i + kk + 1).sort((a, b) => a - b);
    const m = win.length % 2 ? win[(win.length - 1) / 2] : (win[win.length / 2 - 1] + win[win.length / 2]) / 2;
    return Math.abs(v - m) > limit ? m : v;
  });
}

// Least-squares non-decreasing fit (pool adjacent violators).
function isotonic(values) {
  const blocks = []; // [mean, count]
  for (const v of values) {
    blocks.push([v, 1]);
    while (blocks.length > 1 && blocks[blocks.length - 2][0] > blocks[blocks.length - 1][0]) {
      const [m2, n2] = blocks.pop();
      const b = blocks[blocks.length - 1];
      b[0] = (b[0] * b[1] + m2 * n2) / (b[1] + n2);
      b[1] += n2;
    }
  }
  const out = [];
  for (const [m, n] of blocks) for (let i = 0; i < n; i++) out.push(m);
  return out;
}

// Smoothed flow of a stored brew: [[t, flow], ...] from the first drop to
// `end` (the stored duration; all points without one). Centred Gaussian
// local linear slope of the despiked, isotonic weight; never negative.
function trendFlow(points, end, sigma = TREND_SIGMA) {
  const upTo = end ? points.filter((p) => p[0] <= end + 1e-9) : points;
  if (upTo.length < FLOW_MIN_POINTS) return [];
  const all = isotonic(despike(upTo.map((p) => p[1])));
  const first = all.findIndex((w) => w > all[0] + FLOW_START_DELTA);
  if (first < 0 || upTo.length - first < FLOW_MIN_POINTS) return [];
  const times = upTo.slice(first).map((p) => p[0]);
  const weights = all.slice(first);
  const reach = 3 * sigma;
  return times.map((t0) => {
    let sk = 0;
    let sd = 0;
    let swt = 0;
    for (let j = 0; j < times.length; j++) {
      const d = times[j] - t0;
      if (Math.abs(d) > reach) continue;
      const k = Math.exp(-0.5 * (d / sigma) ** 2);
      sk += k;
      sd += k * d;
      swt += k * weights[j];
    }
    const md = sd / sk;
    const mw = swt / sk;
    let num = 0;
    let den = 0;
    for (let j = 0; j < times.length; j++) {
      const d = times[j] - t0;
      if (Math.abs(d) > reach) continue;
      const k = Math.exp(-0.5 * (d / sigma) ** 2);
      num += k * (d - md) * (weights[j] - mw);
      den += k * (d - md) ** 2;
    }
    return [t0, den > 0 ? Math.max(0, num / den) : 0];
  });
}

// Smoothed point at time t (same timestamps as the raw points); none after
// the line's end.
function trendAt(trend, t) {
  if (!trend || !trend.length || t > trend[trend.length - 1][0] + 1e-9) return null;
  let best = trend[0];
  for (const p of trend) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
  return best;
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

// clip-path inset() with [top, right, bottom, left] in px.
function insetCss(v, radius) {
  return `inset(${v.map((n) => `${Math.round(n * 10) / 10}px`).join(" ")} round ${radius}px)`;
}

// Computed clip-path "inset(a b c d round r)" -> [top, right, bottom, left];
// null for anything else (none, unparsable).
function parseInset(value) {
  const m = /^\s*inset\(([^)]*)\)/.exec(value || "");
  if (!m) return null;
  const nums = m[1].split(/\s+round\s+/)[0].trim().split(/\s+/).map(parseFloat);
  if (!nums.length || nums.some((n) => !Number.isFinite(n))) return null;
  const [t, r = t, b = t, l = r] = nums;
  return [t, r, b, l];
}

// Inset that clips the rect `outer` down to the rect `inner` (both
// viewport rects); parts of `inner` outside `outer` are clamped.
function insetBetween(inner, outer) {
  return [
    Math.max(0, inner.top - outer.top),
    Math.max(0, outer.right - inner.right),
    Math.max(0, outer.bottom - inner.bottom),
    Math.max(0, inner.left - outer.left),
  ];
}

// Composed parent: through shadow roots to their host.
function composedParent(node) {
  if (!node) return null;
  if (node.assignedSlot) return node.assignedSlot;
  return node.parentNode || node.host || null;
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

// Which scale the card shows (websocket target fields), not a brew target.
// Pump throttle view of a brew: power trace [[t, %]] in profile time, the
// target shot time in profile time, observe mode, grind-finer hint.
// ``throttle``: the brew's (or live) throttle record; ``offset``: relay on ->
// profile t = 0 (s). Null when there is nothing to draw.
// ``pump``: the power is real (set or read through a configured pump power
// entity). Without one the trace is an assumption and is not shown: no band,
// no legend key, no hover value; the target time marker stays.
function throttleView(throttle, offset) {
  if (!throttle || !throttle.mode || throttle.mode === "off") return null;
  const pump = hasPump(throttle);
  const power = pump && Array.isArray(throttle.power) ? throttle.power : [];
  const target =
    throttle.target_time != null && offset != null ? throttle.target_time - offset : null;
  if (!pump && target == null) return null;
  if (power.length < 1 && target == null) return null;
  return {
    pump,
    power,
    targetT: target,
    observe: observedOnly(throttle),
    grindFiner: !!throttle.grind_finer,
    profile: typeof throttle.profile === "string" && throttle.profile ? throttle.profile : null,
  };
}

// Whether the power was only observed (dashed line, no hover value): the
// engine did not control the pump (mode observe, or no usable pump entity).
// Since 0.7.0b3 the record and the live info say so ("controlled"), the
// same for a brew live and stored. Older records: mode observe or not
// applied (a controlled shot that never had to write the pump counts as
// observed there).
function observedOnly(throttle) {
  if (typeof throttle.controlled === "boolean") return !throttle.controlled;
  return throttle.mode === "observe" || throttle.applied === false;
}

// Whether a throttle record had a pump power entity. Since 0.7.0b3 the
// record says so ("pump"). Older records: only a brew whose power was really
// written ("applied") proves a pump; an observed one may have had none (its
// trace then is a 100 % assumption), so it shows no band.
function hasPump(throttle) {
  if (typeof throttle.pump === "boolean") return throttle.pump;
  return throttle.applied === true;
}

// Built-in pump profiles (0.7.0b5): ids, their names (the same as the
// integration's select state translations; a test keeps them equal) and
// the German labels they had up to 0.7.0b4 (older records carry those).
const BUILTIN_PROFILES = {
  standard: { en: "Standard", de: "Standard" },
  soft_preinfusion: { en: "Soft preinfusion", de: "Sanfte Vorbrühung" },
  blooming: { en: "Blooming", de: "Blooming" },
  allonge: { en: "Allongé", de: "Allongé" },
  lever: { en: "Lever approximation", de: "Hebel-Annäherung" },
  target_time: { en: "Target time", de: "Zielzeit" },
};
const LEGACY_PROFILES = {
  "sanfte vorbrühung": "soft_preinfusion",
  "allongé": "allonge",
  "hebel-annäherung": "lever",
  zielzeit: "target_time",
};

// A profile's name for people: built-ins (by id or old label) in the
// language, imported ones as they are.
function profileName(label, language) {
  const key = String(label).trim().toLowerCase();
  const id = BUILTIN_PROFILES[key] ? key : LEGACY_PROFILES[key];
  return id ? BUILTIN_PROFILES[id][language === "de" ? "de" : "en"] : label;
}

// Text for innerHTML (profile labels are user data).
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// Step line points for a power trace up to tEnd: each value holds until the
// next change.
function powerSteps(power, tEnd) {
  const out = [];
  for (let i = 0; i < power.length; i++) {
    const [t, d] = power[i];
    if (out.length) out.push([t, out[out.length - 1][1]]);
    out.push([t, d]);
  }
  if (out.length && tEnd != null && tEnd > out[out.length - 1][0]) {
    out.push([tEnd, out[out.length - 1][1]]);
  }
  return out;
}

// Value of a drawn step line (powerSteps points) at t: the step that holds
// there (at a change the new value), null outside the drawn line.
function stepAt(pts, t) {
  if (!pts.length || t == null) return null;
  if (t < Math.max(0, pts[0][0]) - 1e-6 || t > pts[pts.length - 1][0] + 1e-6) return null;
  let v = pts[0][1];
  for (const p of pts) {
    if (p[0] <= t + 1e-6) v = p[1];
    else break;
  }
  return v;
}

function scaleOf(config) {
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
    this._confirm = null; // brew asked to delete: { id, start, busy, error }
    this._pendingDelete = null; // { id, keep }: delete call running
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
    this._readyTarget = null; // its auto-stop target in g (live_ready)
    this._smoothing = FLOW_SMOOTHING_DEFAULT;
    this._displayCache = new WeakMap(); // points array -> displayed points
    this._trendCache = new WeakMap(); // stored brew -> smoothed flow line
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
    this._brewFailed = false; // fetching the shown brew failed (_step): retried
    this._resyncing = null; // { keepId } of the reconnect reload in flight
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
    // Expansion state: idle | expanding | expanded | collapsing.
    this._xPhase = "idle";
    this._expanded = false; // card in the fixed layer (incl. both animations)
    this._expandRect = null; // card rect while expanded (viewport px)
    this._anims = { surface: null, backdrop: null, content: [] };
    this._enterPending = false; // content enters after the next render
    this._relayout = false; // chart width/height to re-measure after render
    this._renderHeld = false; // collapse running: keep the live view
    this._renderDeferred = false;
    this._instant = true; // initial state snapshot: expand without animation
    this._liveKnown = false; // live_start or live_ready arrived since (re)subscribing
    this._enterFrom = null; // content opacities when an expand interrupts a collapse
    this._hostHeight = null; // host's inline height before the placeholder
    this._editMode = false;
    this._preview = false;
    this._onExpandResize = () => {
      if (!this._expanded) return;
      // A running animation ends at its end state first (its rects are
      // stale), then the layer is laid out anew.
      if (this._xPhase === "collapsing") {
        this._finishCollapse(true);
        return;
      }
      if (this._xPhase === "expanding") {
        this._stopExpandAnims();
        this._xPhase = "expanded";
      }
      this._layoutExpanded();
      this._fit();
    };
    // Swipes and wheel on the fixed layer must not scroll the dashboard
    // underneath (the slot would move away from the collapse target).
    this._blockScroll = (ev) => {
      if (ev.cancelable) ev.preventDefault();
    };
  }

  // Set by Home Assistant on cards in edit mode / the editor preview.
  set editMode(v) {
    if (!!v === this._editMode) return;
    this._editMode = !!v;
    if (this._built) this._render();
  }

  get editMode() {
    return this._editMode;
  }

  set preview(v) {
    if (!!v === this._preview) return;
    this._preview = !!v;
    if (this._built) this._render();
  }

  get preview() {
    return this._preview;
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
      JSON.stringify(scaleOf(config)) !== JSON.stringify(scaleOf(this._config));
    this._config = { ...config };
    // A confirmation or a delete in flight belongs to the old config.
    this._confirm = null;
    this._pendingDelete = null;
    this._setHeightMode(heightMode);
    if (changed) {
      this._brews = null;
      this._cache.clear();
      this._index = 0;
      this._error = null;
      this._brewFailed = false;
      this._ready = false;
      this._readyDose = null;
      this._setLive(null);
      this._instant = true;
      this._liveKnown = false;
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
    // Edit mode is not always announced to the card: re-check it.
    if (this._built && this._config.expand_when_active === true) {
      const on = this._xPhase === "expanding" || this._xPhase === "expanded";
      if (this._wantExpanded() !== on) this._render();
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
    if (this._expanded) {
      // Expanded: the chart fills the layer whatever the configured mode;
      // the mode applies again after the collapse.
      if (mode === "fill" && this.isConnected) this._watchViewport();
      this._scheduleFit();
      return;
    }
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
    if (this._card) this._card.classList.toggle("fill", this._fills());
    return changed;
  }

  // Chart fills the height: chart_height: fill, or expanded.
  _fills() {
    return this._heightMode === "fill" || this._expanded;
  }

  // Configured chart height again (after a collapse).
  _restoreHeight() {
    if (this._heightMode === "fill") {
      if (this._card) this._card.classList.add("fill");
      if (this.isConnected) this._watchViewport();
      this._fit();
    } else {
      this._applyChartHeight(typeof this._heightMode === "number" ? this._heightMode : CHART_HEIGHT);
    }
  }

  // Fill mode: grow the chart so the card ends at the bottom of the visible
  // viewport. Only what lies above the chart (card offset, head, stats,
  // legend) and the card's bottom padding count, so the chart's own height
  // never feeds back into the result.
  _fit() {
    this._fitRaf = 0;
    if (!this._fills() || !this._chartBox || !this.isConnected) return;
    const box = this._chartBox.getBoundingClientRect();
    if (this._expanded) {
      // The card has a fixed height: the chart takes what is left of it.
      const card = this._card.getBoundingClientRect();
      const pad = parseFloat(getComputedStyle(this._card).paddingBottom) || 0;
      const fit = Math.max(CHART_HEIGHT_MIN, Math.floor(card.bottom - box.top - pad));
      if (this._applyChartHeight(fit)) this._renderChart();
      return;
    }
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
    if (!this._fills() || this._fitRaf) return;
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
    // (Re)connected: no expansion until the live subscription's snapshot
    // has arrived, then at once without animation (page load or view
    // switch during a brew, edit mode left).
    this._instant = true;
    this._liveKnown = false;
    if (this._hass) this._render();
  }

  disconnectedCallback() {
    this._teardownExpansion();
    this._unsubscribe();
    this._cancelRetry();
    if (this._resize) this._resize.disconnect();
    this._unwatchViewport();
    this._setLive(null);
    // The ready state comes again with the next subscription's snapshot.
    this._ready = false;
    this._readyDose = null;
    this._readyTarget = null;
    this._liveKnown = false;
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
    const scale = scaleOf(this._config);
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
        ...scale,
      }));
    }
    if (!this._liveUnsub) {
      this._liveIdles = 0;
      this._liveUnsub = this._track("_liveUnsub", conn.subscribeMessage((msg) => this._onLive(msg), {
        type: `${DOMAIN}/subscribe_live`,
        ...scale,
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
    if (
      !this._retryTimer &&
      this._unsub &&
      this._liveUnsub &&
      this._brews !== null &&
      !this._brewFailed
    ) {
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
    const failed =
      !this._unsub || !this._liveUnsub || this._brews === null || this._brewFailed;
    if (!failed) return;
    this._subscribe();
    if (this._brews === null) this._load();
    else if (this._brewFailed) this._fetchBrew();
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
      this._liveKnown = true;
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
      // 0.7.0b6: frames that came in a backlog (held back, then delivered
      // at once) were placed back at their scale time: the points from
      // replace_from on are replaced by this message's points. Live points
      // are otherwise never changed once drawn.
      if (msg.replace_from != null) {
        const live = this._live.points;
        let cut = live.length;
        while (cut > 0 && live[cut - 1][0] >= msg.replace_from) cut--;
        live.splice(cut);
      }
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
      const target = ready && msg.target != null ? msg.target : null;
      const known = this._liveKnown;
      this._liveKnown = true;
      // With expand_when_active, "Prepare brew" turned on ends a replay so
      // the ready view shows and the card expands. Without it: unchanged.
      const endReplay =
        ready &&
        this._config.expand_when_active === true &&
        !!(this._live && this._live.replay);
      if (endReplay) this._setLive(null);
      if (
        endReplay ||
        !known ||
        ready !== this._ready ||
        dose !== this._readyDose ||
        target !== this._readyTarget
      ) {
        this._ready = ready;
        this._readyDose = dose;
        this._readyTarget = target;
        // Only the empty ready view changes; a live brew goes on.
        if (!this._live) {
          this._hover = null;
          this._render();
        }
      }
      // live_ready closes the snapshot a new subscription starts with;
      // from now on (de)activation is animated.
      this._instant = false;
    } else if (msg.type === "live_idle") {
      // Not yet known: the ready state follows in live_ready.
      const resubscribed = this._liveIdles++ > 0;
      if (this._live && !this._live.replay) {
        // Missed the end (e.g. websocket reconnect): back to the history.
        this._setLive(null);
        this._index = 0;
        this._resync();
      } else if (resubscribed) {
        this._resync();
      }
    }
  }

  _resync() {
    // After a reconnect: brews saved or corrected meanwhile were missed.
    // One reconnect asks twice (the connection's ready event, then the
    // resubscribed live snapshot): while that reload is in flight a second
    // one for the same brew would only fetch the same list again.
    const keepId =
      (this._index > 0 && this._brews ? this._brews[this._index]?.id : null) || null;
    if (this._resyncing && this._resyncing.keepId === keepId) return;
    this._cache.clear();
    const run = { keepId };
    this._resyncing = run;
    Promise.resolve(this._load(keepId)).finally(() => {
      if (this._resyncing === run) this._resyncing = null;
    });
  }

  _applyLiveValues(msg) {
    const l = this._live;
    l.weight = msg.weight;
    l.flow = msg.flow;
    l.timer = msg.timer;
    l.elapsed = msg.elapsed || 0;
    l.phase = msg.phase || "brewing";
    l.stop = msg.stop;
    l.cut = msg.cut != null ? msg.cut : null;
    // Replay ticks carry no target: the stored brew's one stays.
    if ("target" in msg) l.target = msg.target;
    if ("throttle" in msg) l.throttle = msg.throttle || null;
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
    // Auto-stop cut: the brew time stops there (pump off), weighing goes on.
    if (l.cut != null) return l.cut;
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
    const th = l.throttle ? throttleView(l.throttle, l.throttle.relay_offset) : null;
    const tTarget = th && th.targetT != null ? th.targetT + 1 : 0;
    const tSpan = Math.max(tLast, l.elapsed || 0, tTarget) + 3;
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
    // The live throttle info (pump key, observed or applied) can arrive or
    // change after the brew start: the legend follows without a full render.
    if (this._pumpKey() !== this._legendPumpKey) this._renderLegend();
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

  // Active: the ready view, or a real live brew (any phase incl. tail);
  // a replay of a stored brew is not.
  _isActive() {
    return this._readyView() || !!(this._live && !this._live.replay);
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
      target: brew.target != null ? brew.target : null,
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
    const pending = this._pendingDelete;
    if (pending && currentId === pending.id) {
      // Our own delete: go on to its neighbour, not to the newest brew.
      this._load(pending.keep);
      return;
    }
    this._load(viewingLatest ? null : currentId);
  }

  // --------------------------------------------------------------- delete

  _askDelete() {
    if (this._live || this._readyView() || !this._brews) return;
    const summary = this._brews[this._index];
    if (!summary) return;
    this._confirm = {
      id: summary.id,
      start: summary.start,
      learned: summary.learned === true, // in the auto-stop learning journal
      busy: false,
      error: null,
    };
    this._render();
    this._els.confirmCancel.focus?.();
  }

  _cancelDelete() {
    if (!this._confirm || this._confirm.busy) return;
    this._confirm = null;
    this._render();
  }

  // "23.11. 19:47" (de) / "Nov 23, 7:47 PM" (en): the brew start, local time.
  _deleteWhen(start) {
    const d = new Date(start);
    const l = lang(this._hass);
    const date = d.toLocaleDateString(
      l,
      l === "de" ? { day: "2-digit", month: "2-digit" } : { day: "numeric", month: "short" }
    );
    const time = d.toLocaleTimeString(l, { hour: "2-digit", minute: "2-digit" });
    return l === "de" ? `${date} ${time}` : `${date}, ${time}`;
  }

  _renderConfirm() {
    const e = this._els;
    const c = this._confirm;
    e.confirm.classList.toggle("hidden", !c);
    if (!c) return;
    const ask = this._t("delete_confirm").replace("{when}", this._deleteWhen(c.start));
    e.confirmText.textContent = c.learned ? `${ask} ${this._t("delete_confirm_learned")}` : ask;
    e.confirmError.textContent = c.error || "";
    e.confirmError.classList.toggle("hidden", !c.error);
    e.confirmCancel.textContent = this._t("delete_cancel");
    e.confirmOk.textContent = this._t("delete_ok");
    e.confirmCancel.disabled = c.busy;
    e.confirmOk.disabled = c.busy;
  }

  // A failed service call's message in the UI language (the integration's
  // translated exception, as HA's own toast does), else its raw message.
  async _serviceError(err) {
    const key = err && err.translation_key;
    const hass = this._hass;
    if (key && hass && typeof hass.loadBackendTranslation === "function") {
      try {
        const domain = err.translation_domain || DOMAIN;
        const localize = (await hass.loadBackendTranslation("exceptions", domain)) || hass.localize;
        const text =
          typeof localize === "function" &&
          localize(`component.${domain}.exceptions.${key}.message`, err.translation_placeholders || {});
        if (text) return text;
      } catch (_e) {
        // fall through to the raw message
      }
    }
    return (err && err.message) || this._t("delete_error");
  }

  async _confirmDelete() {
    const c = this._confirm;
    if (!c || c.busy || !this._hass) return;
    const brews = this._brews || [];
    const idx = brews.findIndex((b) => b.id === c.id);
    // Next older brew, else the newer one, else none (empty state).
    const neighbour = idx >= 0 ? brews[idx + 1] || brews[idx - 1] || null : null;
    const keep = neighbour ? neighbour.id : null;
    c.busy = true;
    c.error = null;
    const pending = { id: c.id, keep };
    this._pendingDelete = pending;
    this._renderConfirm();
    try {
      // No HA toast (notifyOnError false): the card shows the error.
      await this._hass.callService(
        DOMAIN,
        "delete_brew",
        { brew_id: c.id, ...scaleOf(this._config) },
        undefined,
        false
      );
    } catch (err) {
      if (this._pendingDelete === pending) this._pendingDelete = null;
      const message = await this._serviceError(err);
      if (this._confirm === c) {
        c.busy = false;
        c.error = message;
        this._render();
      }
      return;
    }
    // setConfig meanwhile (other scale): its state is not ours to change.
    if (this._pendingDelete !== pending) return;
    this._pendingDelete = null;
    if (this._confirm === c) this._confirm = null;
    this._cache.delete(c.id);
    if (this._brews) {
      this._brews = this._brews.filter((b) => b.id !== c.id);
      const k = keep ? this._brews.findIndex((b) => b.id === keep) : -1;
      this._index = k > 0 ? k : 0;
    }
    this._hover = null;
    this._render();
    this._load(keep);
  }

  async _load(keepId) {
    if (!this._hass) return;
    const token = ++this._loadToken;
    try {
      const res = await this._hass.callWS({
        type: `${DOMAIN}/brews`,
        ...scaleOf(this._config),
      });
      if (token !== this._loadToken) return;
      this._brews = res.brews || [];
      this._error = null;
      this._brewFailed = false;
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
      ...scaleOf(this._config),
    });
    if (res.brew) this._cache.set(summary.id, res.brew);
  }

  // Whether the "older" (delta 1) / "newer" (-1) button can be used right
  // now: shown (not live, ready, replaying, no message, no confirmation
  // open) and enabled (not at the end of the list). The swipe uses the same.
  _navUsable(delta) {
    if (!this._navOpen()) return false;
    const btn = delta > 0 ? this._els.prev : this._els.next;
    return !!btn && !btn.disabled;
  }

  // The navigation is shown at all (stored brews, no live/ready view, no
  // replay, no message, no confirmation open); a button may still be
  // disabled at the end of the list.
  _navOpen() {
    const e = this._els;
    if (!e || !this._brews || !this._brews.length) return false;
    if (this._live || this._readyView() || this._confirm) return false;
    return !(e.nav.classList.contains("hidden") || e.nav.classList.contains("replaying"));
  }

  // The buttons' path (and the swipe's): one step if the button is usable.
  _navStep(delta) {
    if (!this._navUsable(delta)) return false;
    this._step(delta);
    return true;
  }

  // --------------------------------------------------------------- swipe

  // Swipe between brews (0.7.0b7), anywhere on the card.
  _wireSwipe(card) {
    card.addEventListener("pointerdown", (ev) => this._swipeStart(ev));
    card.addEventListener("pointermove", (ev) => this._swipeMove(ev));
    card.addEventListener("pointerup", (ev) => this._swipeEnd(ev));
    card.addEventListener("pointercancel", () => { this._swipe = null; });
    // Capture: a click from a swipe's pointerup never reaches a button.
    card.addEventListener("click", (ev) => this._swipeClickGuard(ev), true);
  }

  _swipeStart(ev) {
    // Touch and pen only; a second finger (pinch) ends a running swipe.
    if (!ev.isPrimary || ev.pointerType === "mouse") {
      this._swipe = null;
      return;
    }
    this._swipe = {
      id: ev.pointerId,
      x: ev.clientX,
      y: ev.clientY,
      t: ev.timeStamp,
      samples: [[ev.timeStamp, ev.clientX]],
      // The buttons' locks as the finger went down: a brew that starts
      // (or ends) meanwhile does not make the card jump.
      usable: { 1: this._navUsable(1), [-1]: this._navUsable(-1) },
      open: this._navOpen(),
    };
  }

  _swipeMove(ev) {
    const s = this._swipe;
    if (!s || ev.pointerId !== s.id) return;
    s.samples.push([ev.timeStamp, ev.clientX]);
    // Keep only what the end speed needs (and one sample before it).
    const from = ev.timeStamp - SWIPE_SPEED_MS;
    while (s.samples.length > 2 && s.samples[1][0] <= from) s.samples.shift();
  }

  // Speed (px/ms) over the last SWIPE_SPEED_MS up to (t, x): from the
  // newest sample at or before that window's start.
  static _endSpeed(samples, t, x) {
    const from = t - SWIPE_SPEED_MS;
    let ref = samples[0];
    for (const smp of samples) {
      if (smp[0] <= from) ref = smp;
    }
    return Math.abs(x - ref[1]) / Math.max(1, t - ref[0]);
  }

  // Classify the gesture at pointerup: a swipe needs distance, speed, a
  // short duration and a mostly horizontal direction (constants above).
  // Returns the step made (1 older, -1 newer), 0 for a swipe without a
  // step, null for no swipe.
  _swipeEnd(ev) {
    const s = this._swipe;
    this._swipe = null;
    if (!s || ev.pointerId !== s.id) return null;
    const dx = ev.clientX - s.x;
    const dy = ev.clientY - s.y;
    const dt = Math.max(1, ev.timeStamp - s.t);
    const swipe =
      Math.abs(dx) >= SWIPE_MIN_PX &&
      Math.abs(dx) > SWIPE_RATIO * Math.abs(dy) &&
      dt <= SWIPE_MAX_MS &&
      DifluidBrewCard._endSpeed(s.samples, ev.timeStamp, ev.clientX) >= SWIPE_MIN_SPEED;
    if (!swipe) return null;
    this._swipedAt = ev.timeStamp;
    // Finger to the right = the older brew (as the "older" button).
    const dir = dx > 0 ? 1 : -1;
    // Usable when the finger went down and still now.
    const moved = s.usable[dir] ? this._navStep(dir) : false;
    if (moved) {
      this._swipeSlide(dir);
      return dir;
    }
    // No step (end of the list, live, ...): no readout left frozen by the
    // swipe's pointermoves; at the end of the list a small spring.
    if (this._hover) {
      this._hover = null;
      this._renderChart();
      this._renderLegend();
    }
    // A spring only at the end of the list: the navigation was open when
    // the finger went down (and still is), this direction only blocked by
    // the list's end - not by a brew that started or ended meanwhile.
    if (s.open && !s.usable[dir] && this._navOpen()) this._swipeBounce(dir);
    return 0;
  }

  _swipeClickGuard(ev) {
    if (this._swipedAt != null && ev.timeStamp - this._swipedAt <= SWIPE_CLICK_GUARD_MS) {
      ev.stopPropagation();
      ev.preventDefault();
    }
  }

  // The content a swipe moves: header, stats, legend, chart.
  _swipeTargets() {
    return [this._head, this._els.stats, this._els.legend, this._chartBox].filter(
      (el) => el && typeof el.animate === "function"
    );
  }

  // An expand/collapse animation still runs on the content (e.g. the
  // fade-in after a live brew collapsed): no swipe animation on top.
  _contentAnimating() {
    return (this._anims.content || []).some((a) => a && a.playState === "running");
  }

  // New brew: slides in from the side the finger came from.
  _swipeSlide(dir) {
    if (reducedMotion() || this._contentAnimating()) return;
    const frames = [
      { transform: `translateX(${-dir * SWIPE_SLIDE_PX}px)`, opacity: 0.4 },
      { transform: "none", opacity: 1 },
    ];
    for (const el of this._swipeTargets()) {
      el.animate(frames, { duration: SWIPE_ANIM_MS, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    }
  }

  // End of the list: a small spring in the swipe's direction, no change.
  _swipeBounce(dir) {
    if (reducedMotion() || this._contentAnimating()) return;
    const frames = [
      { transform: "none" },
      { transform: `translateX(${dir * SWIPE_BOUNCE_PX}px)` },
      { transform: "none" },
    ];
    for (const el of this._swipeTargets()) {
      el.animate(frames, { duration: SWIPE_ANIM_MS + 60, easing: "ease-out" });
    }
  }

  async _step(delta) {
    if (!this._brews) return;
    const next = this._index + delta;
    if (next < 0 || next >= this._brews.length) return;
    this._index = next;
    this._hover = null;
    this._render();
    await this._fetchBrew();
  }

  // The shown brew's profile (after a step, or retried after it failed).
  // A failure shows the error (which hides the navigation), so it is
  // retried with the subscribe/load backoff until the brew arrives; the
  // list and the position are kept.
  async _fetchBrew() {
    const token = this._loadToken;
    const index = this._index;
    try {
      await this._ensureBrew();
    } catch (err) {
      // A reload or another step since then owns the card now.
      if (token !== this._loadToken || index !== this._index) return;
      this._error = (err && err.message) || this._t("error");
      this._brewFailed = true;
      this._scheduleRetry();
      this._render();
      return;
    }
    if (token !== this._loadToken || index !== this._index) return;
    if (this._brewFailed) {
      this._brewFailed = false;
      this._error = null;
      this._retrySucceeded();
    }
    this._render();
  }

  // ------------------------------------------------------------- expansion

  _wantExpanded() {
    return (
      this._built &&
      this.isConnected &&
      this._config.expand_when_active === true &&
      this._liveKnown &&
      this._isActive() &&
      !this._inEditor()
    );
  }

  // Dashboard edit mode or the card editor preview: properties Home
  // Assistant sets on the card, an editor ancestor, or a lovelace object in
  // edit mode on the way up to hui-root.
  _inEditor() {
    if (this._editMode || this._preview) return true;
    let n = composedParent(this);
    for (let i = 0; n && i < 200; i++) {
      const tag = (n.localName || "").toLowerCase();
      if (EDITOR_TAGS.has(tag)) return true;
      if (n.editMode === true || n.preview === true) return true;
      if (n.lovelace && n.lovelace.editMode) return true;
      if (tag === "hui-root") break;
      n = composedParent(n);
    }
    return false;
  }

  // Area the card expands over (viewport px): the dashboard view below the
  // app header, to the viewport's right and bottom edges; the whole viewport
  // when HA's structure is not found. Plus the inset the sections view uses.
  _expandArea() {
    const de = document.documentElement || {};
    const vw = de.clientWidth || window.innerWidth || 0;
    const vh = de.clientHeight || window.innerHeight || 0;
    const path = [];
    let view = null;
    let root = null;
    let n = composedParent(this);
    for (let i = 0; n && i < 200; i++) {
      const tag = (n.localName || "").toLowerCase();
      if (tag) path.push(n);
      if (tag === "hui-view-container" || tag === "hui-view") view = n; // outermost wins
      if (tag === "hui-root") {
        root = n;
        break;
      }
      n = composedParent(n);
    }
    let left = 0;
    let top = 0;
    if (view && view.getBoundingClientRect) {
      left = Math.max(0, Math.min(vw, view.getBoundingClientRect().left));
    }
    const header = root && root.shadowRoot && root.shadowRoot.querySelector(".header");
    if (header && header.getBoundingClientRect) {
      const cs = getComputedStyle(header);
      const r = header.getBoundingClientRect();
      if (cs.display !== "none" && cs.visibility !== "hidden" && r.height > 0) {
        top = Math.max(0, Math.min(vh, r.bottom));
      }
    }
    return { left, top, right: vw, bottom: vh, inset: this._expandInset(path, view) };
  }

  // Padding of the view around its sections: the computed paddings of the
  // ancestors between the card's section and the view (inclusive). The
  // default when the structure is not found or the sum is implausible.
  _expandInset(path, view) {
    const fallback = { x: EXPAND_INSET, y: EXPAND_INSET };
    if (!view) return fallback;
    const end = path.indexOf(view);
    let start = path.findIndex((el) => /^hui-(grid-)?section$/.test(el.localName || ""));
    if (end < 0 || start < 0 || start >= end) return fallback;
    let x = 0;
    let y = 0;
    for (let i = start + 1; i <= end; i++) {
      const cs = getComputedStyle(path[i]);
      x += (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.borderLeftWidth) || 0);
      y += (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.borderTopWidth) || 0);
    }
    if (!(x >= 0 && x <= EXPAND_INSET_MAX && y >= 0 && y <= EXPAND_INSET_MAX)) return fallback;
    return { x, y };
  }

  // Positions backdrop and card in the fixed layer (also on resize).
  _layoutExpanded() {
    const a = this._expandArea();
    const w = Math.max(0, a.right - a.left);
    const h = Math.max(0, a.bottom - a.top);
    const ix = Math.min(a.inset.x, w / 4);
    const iy = Math.min(a.inset.y, h / 4);
    const rect = {
      left: a.left + ix,
      top: a.top + iy,
      width: Math.max(0, w - 2 * ix),
      height: Math.max(0, h - 2 * iy),
    };
    rect.right = rect.left + rect.width;
    rect.bottom = rect.top + rect.height;
    this._expandRect = rect;
    const bs = this._backdrop.style;
    bs.left = `${a.left}px`;
    bs.top = `${a.top}px`;
    bs.width = `${w}px`;
    bs.height = `${h}px`;
    this._backdrop.classList.add("on");
    const cs = this._card.style;
    cs.left = `${rect.left}px`;
    cs.top = `${rect.top}px`;
    cs.width = `${rect.width}px`;
    cs.height = `${rect.height}px`;
    this._card.classList.add("expanded");
    this._relayout = true;
  }

  _cardRadius() {
    const r = parseFloat(getComputedStyle(this._card).borderTopLeftRadius);
    return Number.isFinite(r) ? r : 12;
  }

  // Cancels every expansion animation; returns what was visible just before
  // (clip inset of the surface, backdrop opacity, content opacities), so the
  // next animation starts where the last one stopped.
  _stopExpandAnims() {
    const a = this._anims;
    const seen = { clip: null, backdrop: null, content: new Map() };
    if (a.surface) seen.clip = parseInset(getComputedStyle(this._card).clipPath);
    if (a.backdrop) {
      const o = parseFloat(getComputedStyle(this._backdrop).opacity);
      if (Number.isFinite(o)) seen.backdrop = o;
    }
    for (const anim of a.content) {
      const el = anim.effect && anim.effect.target;
      if (el && !seen.content.has(el)) {
        const o = parseFloat(getComputedStyle(el).opacity);
        if (Number.isFinite(o)) seen.content.set(el, o);
      }
    }
    for (const anim of [a.surface, a.backdrop, ...a.content]) if (anim) anim.cancel();
    this._anims = { surface: null, backdrop: null, content: [] };
    return seen;
  }

  // Called by every render: expands or collapses when the wanted state
  // differs from the current one.
  _syncExpansion() {
    const want = !!this._wantExpanded();
    const on = this._xPhase === "expanding" || this._xPhase === "expanded";
    if (want === on) return;
    const animate =
      !this._instant && !reducedMotion() && typeof this._card.animate === "function";
    if (want) this._expand(animate);
    else this._collapse(animate);
  }

  _expand(animate) {
    const seen = this._stopExpandAnims();
    let from = seen.clip;
    if (this._xPhase === "idle") {
      // The host stays in the grid with its current size (placeholder).
      const slot = this.getBoundingClientRect();
      this._hostHeight = this.style.height;
      this.style.height = `${slot.height}px`;
      this._expanded = true;
      this._layoutExpanded();
      from = insetBetween(slot, this._expandRect);
      window.addEventListener("resize", this._onExpandResize);
      window.addEventListener("orientationchange", this._onExpandResize);
      for (const el of [this._backdrop, this._card]) {
        el.addEventListener("touchmove", this._blockScroll, { passive: false });
        el.addEventListener("wheel", this._blockScroll, { passive: false });
      }
      this._enterFrom = null;
    } else {
      // Interrupting a collapse: the content fades in from where it is.
      this._enterFrom = seen.content;
    }
    // From collapsing: the deferred render now shows the active view.
    this._renderHeld = false;
    this._renderDeferred = false;
    this._card.classList.add("fill");
    if (!animate) {
      this._xPhase = "expanded";
      this._enterPending = false;
      return;
    }
    this._xPhase = "expanding";
    const radius = this._cardRadius();
    const surface = this._card.animate(
      [{ clipPath: insetCss(from || [0, 0, 0, 0], radius) }, { clipPath: insetCss([0, 0, 0, 0], radius) }],
      { duration: EXPAND_MS, easing: EXPAND_EASE }
    );
    surface.onfinish = () => {
      if (this._anims.surface !== surface) return;
      this._anims.surface = null;
      this._xPhase = "expanded";
    };
    this._anims.surface = surface;
    const b0 = seen.backdrop != null ? seen.backdrop : 0;
    this._anims.backdrop = this._backdrop.animate([{ opacity: b0 }, { opacity: 1 }], {
      duration: BACKDROP_MS * (1 - b0),
      easing: "linear",
    });
    this._enterPending = true; // after the render: the new elements enter
  }

  _collapse(animate) {
    const seen = this._stopExpandAnims();
    this._enterPending = false;
    if (!animate || !this._expandRect) {
      this._finishCollapse(false);
      return;
    }
    this._xPhase = "collapsing";
    // The live view stays visible while it fades and the surface shrinks.
    this._renderHeld = true;
    this._renderDeferred = true;
    const radius = this._cardRadius();
    const to = insetBetween(this.getBoundingClientRect(), this._expandRect);
    const content = [];
    for (const el of Array.from(this._card.children)) {
      const o0 = seen.content.has(el) ? seen.content.get(el) : 1;
      content.push(
        el.animate([{ opacity: o0 }, { opacity: 0 }], { duration: LEAVE_MS, easing: "ease-in", fill: "forwards" })
      );
    }
    this._anims.content = content;
    const surface = this._card.animate(
      [{ clipPath: insetCss(seen.clip || [0, 0, 0, 0], radius) }, { clipPath: insetCss(to, radius) }],
      { duration: COLLAPSE_MS, delay: LEAVE_MS, easing: COLLAPSE_EASE, fill: "both" }
    );
    surface.onfinish = () => {
      if (this._anims.surface === surface) this._finishCollapse(true);
    };
    this._anims.surface = surface;
    const b0 = seen.backdrop != null ? seen.backdrop : 1;
    this._anims.backdrop = this._backdrop.animate([{ opacity: b0 }, { opacity: 0 }], {
      duration: BACKDROP_MS,
      delay: LEAVE_MS + COLLAPSE_MS - BACKDROP_MS,
      easing: "linear",
      fill: "both",
    });
  }

  // Back into the grid slot. `fade`: after an animated collapse, render the
  // deferred view and fade it in.
  _finishCollapse(fade) {
    this._stopExpandAnims();
    this._unfix();
    this._xPhase = "idle";
    this._restoreHeight();
    this._relayout = true;
    const deferred = this._renderDeferred;
    this._renderHeld = false;
    this._renderDeferred = false;
    if (!fade) return; // called from a render, which goes on
    if (deferred) this._render();
    else this._afterRender();
    if (typeof this._card.animate !== "function") return;
    for (const el of Array.from(this._card.children)) {
      this._anims.content.push(
        el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: RETURN_MS, easing: "ease-out" })
      );
    }
  }

  // Removes the fixed layer and the placeholder size.
  _unfix() {
    window.removeEventListener("resize", this._onExpandResize);
    window.removeEventListener("orientationchange", this._onExpandResize);
    for (const el of [this._backdrop, this._card]) {
      if (!el) continue;
      el.removeEventListener("touchmove", this._blockScroll, { passive: false });
      el.removeEventListener("wheel", this._blockScroll, { passive: false });
    }
    this._expanded = false;
    this._expandRect = null;
    this._enterFrom = null;
    if (this._card) {
      this._card.classList.remove("expanded");
      for (const k of ["left", "top", "width", "height"]) this._card.style[k] = "";
      this._card.classList.toggle("fill", this._heightMode === "fill");
    }
    if (this._backdrop) this._backdrop.classList.remove("on");
    if (this._hostHeight !== null) {
      this.style.height = this._hostHeight;
      this._hostHeight = null;
    }
  }

  // Card removed: nothing may stay fixed, clipped or animating.
  _teardownExpansion() {
    if (!this._built) return;
    this._stopExpandAnims();
    const wasExpanded = this._expanded;
    this._unfix();
    this._xPhase = "idle";
    this._enterPending = false;
    this._renderHeld = false;
    this._renderDeferred = false;
    if (wasExpanded) this._restoreHeight();
  }

  // After the content is rendered: re-measure the chart for a new layout,
  // then let the content enter (expand animation).
  _afterRender() {
    if (this._relayout) {
      this._relayout = false;
      const w = Math.round(this._chartBox.getBoundingClientRect().width);
      if (w && w !== this._width) {
        this._width = w;
        this._renderChart();
      }
    }
    if (this._expanded) this._fit();
    if (this._enterPending) {
      this._enterPending = false;
      this._enter();
    }
  }

  // Ready/live content entering after the surface: head, stats and legend
  // (stable containers; the live stats replace their children every frame)
  // fade in and move up a little, staggered; the chart wipes in from the
  // left. After an interrupted collapse each starts at its current opacity.
  _enter() {
    if (typeof this._card.animate !== "function") return;
    const from = this._enterFrom;
    this._enterFrom = null;
    const o0 = (el) => (from && from.has(el) ? from.get(el) : 0);
    const opts = (delay, duration) => ({ duration, delay, easing: EXPAND_EASE, fill: "backwards" });
    const lift = (el) => [
      { opacity: o0(el), transform: from ? "none" : "translateY(6px)" },
      { opacity: 1, transform: "none" },
    ];
    const content = this._anims.content;
    // The legend ends ENTER_STAGGER * 2 + ENTER_MS = 250 ms, the chart
    // CHART_ENTER_DELAY + CHART_ENTER_MS = 300 ms after the surface.
    [this._head, this._els.stats, this._els.legend].forEach((el, i) =>
      content.push(el.animate(lift(el), opts(EXPAND_MS + i * ENTER_STAGGER, ENTER_MS)))
    );
    const chart = this._chartBox;
    const chartFrames = from
      ? [{ opacity: o0(chart) }, { opacity: 1 }]
      : [
          { opacity: 0, clipPath: "inset(0 100% 0 0)" },
          { opacity: 1, clipPath: "inset(0 0 0 0)" },
        ];
    content.push(chart.animate(chartFrames, opts(EXPAND_MS + CHART_ENTER_DELAY, CHART_ENTER_MS)));
  }

  // ---------------------------------------------------------------- render

  _build() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { height: 100%; box-sizing: border-box; padding: 16px; display: flex; flex-direction: column; gap: 12px; touch-action: pan-y pinch-zoom; }
        .head { display: flex; align-items: center; gap: 8px; }
        .titles { flex: 1; min-width: 0; }
        .title { font-size: 1.1rem; font-weight: 500; color: var(--primary-text-color); line-height: 1.3; }
        .when { font-size: 0.85rem; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .detail { font-size: 0.75rem; line-height: 1.3; min-height: 1.3em; height: 1.3em; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
        .detail .brew-profile { display: inline-block; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: top; }
        .invisible { visibility: hidden; }
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
        .stat-note { font-size: 0.7rem; line-height: 1.3; min-height: 1.3em; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        /* One row that never wraps (the chart height counts it): on a narrow
           card long keys (a profile name) shrink with an ellipsis instead of
           overflowing the card. */
        .legend { display: flex; gap: 16px; font-size: 0.8rem; color: var(--secondary-text-color); align-items: center; min-height: 1.2em; min-width: 0; overflow: hidden; white-space: nowrap; }
        .legend .key { display: inline-flex; align-items: center; gap: 6px; flex: 0 1 auto; min-width: 0; }
        .legend .key-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .legend .swatch { width: 14px; height: 0; border-top: 2px solid; flex: none; }
        .legend .flow .swatch { border-color: var(--primary-color); }
        .legend .trend .swatch { border-color: var(--primary-color); border-top-style: dotted; }
        .legend .weight .swatch { border-color: var(--secondary-text-color); border-top-style: dashed; }
        .legend .pump .swatch { border-color: ${PUMP_COLOR}; }
        .legend .pump.observe .swatch { border-top-style: dashed; }
        .legend .readout { margin-left: auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
        .chart { position: relative; height: ${CHART_HEIGHT}px; touch-action: pan-y; }
        .chart svg { display: block; overflow: visible; pointer-events: none; }
        .message { color: var(--secondary-text-color); padding: 24px 0; text-align: center; }
        .title { display: flex; align-items: center; gap: 8px; }
        .nav.replaying .prev, .nav.replaying .next, .nav.replaying .pos { display: none; }
        .nav .replay svg { width: 20px; height: 20px; }
        .nav .delete svg { width: 20px; height: 20px; }
        .nav.replaying .delete { display: none; }
        ha-card { position: relative; }
        .confirm { position: absolute; inset: 0; z-index: 1; display: grid; place-items: center; padding: 16px; border-radius: inherit; background: color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 85%, transparent); }
        .confirm-box { max-width: 320px; display: flex; flex-direction: column; gap: 12px; padding: 16px; border-radius: 12px; background: var(--ha-card-background, var(--card-background-color)); border: 1px solid var(--divider-color); color: var(--primary-text-color); font-size: 0.95rem; line-height: 1.4; }
        .confirm-error { font-size: 0.85rem; color: var(--error-color); }
        .confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }
        .confirm-actions button { width: auto; height: 36px; padding: 0 12px; border-radius: 18px; font-weight: 500; }
        .confirm-actions .confirm-cancel { color: var(--primary-color); }
        .confirm-actions .confirm-ok { color: var(--error-color); }
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
        .backdrop { display: none; position: fixed; z-index: ${EXPAND_Z}; background: var(--lovelace-background, var(--primary-background-color)); background-attachment: fixed; }
        .backdrop.on { display: block; }
        ha-card.expanded { position: fixed; z-index: ${EXPAND_Z}; margin: 0; overflow: hidden; transition: none; overscroll-behavior: contain; }
        .backdrop.on { overscroll-behavior: contain; }
        ha-card.expanded .chart { touch-action: none; }
      </style>
      <div class="backdrop" aria-hidden="true"></div>
      <ha-card>
        <div class="head">
          <div class="titles">
            <div class="title"><span class="title-text"></span><span class="badge hidden"><span class="dot"></span><span class="badge-text"></span></span></div>
            <div class="when"></div>
            <div class="detail invisible"></div>
          </div>
          <div class="nav">
            <button class="delete"><svg viewBox="0 0 24 24"><path d="${ICON_DELETE}"></path></svg></button>
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
        <div class="confirm hidden" role="alertdialog" aria-modal="true">
          <div class="confirm-box">
            <div class="confirm-text"></div>
            <div class="confirm-error hidden" role="alert"></div>
            <div class="confirm-actions">
              <button class="confirm-cancel"></button>
              <button class="confirm-ok"></button>
            </div>
          </div>
        </div>
      </ha-card>`;
    const $ = (s) => this.shadowRoot.querySelector(s);
    this._els = {
      title: $(".title-text"),
      badge: $(".badge"),
      badgeText: $(".badge-text"),
      note: $(".note"),
      when: $(".when"),
      detail: $(".detail"),
      nav: $(".nav"),
      prev: $(".prev"),
      replay: $(".replay"),
      replayIcon: $(".replay path"),
      del: $(".delete"),
      confirm: $(".confirm"),
      confirmText: $(".confirm-text"),
      confirmError: $(".confirm-error"),
      confirmCancel: $(".confirm-cancel"),
      confirmOk: $(".confirm-ok"),
      next: $(".next"),
      pos: $(".pos"),
      message: $(".message"),
      stats: $(".stats"),
      legend: $(".legend"),
    };
    this._chartBox = $(".chart");
    this._card = $("ha-card");
    this._backdrop = $(".backdrop");
    this._head = $(".head");
    this._applyChartHeight(this._chartH);
    // Brews are newest first: "older" moves up the list.
    this._els.prev.addEventListener("click", () => this._navStep(1));
    this._els.next.addEventListener("click", () => this._navStep(-1));
    this._wireSwipe(this._card);
    this._els.replay.addEventListener("click", () => this._toggleReplay());
    this._els.del.addEventListener("click", () => this._askDelete());
    this._els.confirmCancel.addEventListener("click", () => this._cancelDelete());
    this._els.confirmOk.addEventListener("click", () => this._confirmDelete());
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
    this._syncExpansion();
    if (this._renderHeld) {
      // Collapsing: the live view stays until the card is back in its slot.
      this._renderDeferred = true;
      return;
    }
    this._renderContent();
    this._afterRender();
  }

  _renderContent() {
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
    e.del.title = this._t("delete");
    e.del.setAttribute("aria-label", this._t("delete"));
    if (this._live || readyView) {
      // Deleting is for stored brews only: a brew (or replay) taking over
      // the card drops an open confirmation.
      if (this._confirm && !this._confirm.busy) this._confirm = null;
      e.confirm.classList.add("hidden");
      // Space kept (not hidden): the header does not jump at the brew end.
      // A running brew shows its pump profile there (0.7.0b6).
      const l = this._live;
      const shown = l && l.replay ? (this._brews || [])[this._index] : null;
      const liveProfile = l ? this._profileHtml(shown ? shown.throttle : l.throttle) : "";
      e.detail.innerHTML = liveProfile;
      e.detail.title = liveProfile ? this._t("pump_profile") : "";
      e.detail.classList.toggle("invisible", !liveProfile);
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
    if (!showData && this._confirm && !this._confirm.busy) this._confirm = null;
    this._renderConfirm();
    if (!showData) {
      e.when.textContent = "";
      e.detail.classList.add("invisible");
      return;
    }

    const summary = this._brews[this._index];
    const total = this._brews.length;
    e.pos.textContent = `${total - this._index} / ${total}`;
    e.prev.disabled = this._index >= total - 1;
    e.replay.disabled = !((this._currentBrew() || {}).profile || []).length;
    e.next.disabled = this._index <= 0;
    e.del.disabled = !!this._confirm;
    e.when.textContent = new Date(summary.start).toLocaleString(lang(this._hass), {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });

    const detail = this._detailHtml(summary);
    e.detail.innerHTML = detail;
    e.detail.title = this._t("detail_hint");
    e.detail.classList.toggle("invisible", !detail);

    const stat = (label, value, unit, note) => `
      <div class="stat">
        <div class="label">${label}</div>
        <div class="value">${value}${unit ? `<span class="unit">${unit}</span>` : ""}</div>
        <div class="stat-note">${note || ""}</div>
      </div>`;
    // Stored brews: two decimals (1:2.43), as stored.
    const ratio = summary.ratio == null ? "–" : `1:${this._fmt(summary.ratio, 2)}`;
    e.stats.innerHTML =
      stat(
        this._t("dose"),
        this._fmt(summary.dose, 1),
        summary.dose == null ? "" : "g",
        summary.dose_source === "manual" ? this._t("corrected") : ""
      ) +
      stat(this._t("yield"), this._fmt(summary.yield, 1), "g", this._targetText(summary.target)) +
      stat(this._t("ratio"), ratio, "") +
      stat(
        this._t("time"),
        this._fmt(summary.shot_time != null ? summary.shot_time : summary.duration, 1),
        "s",
        this._timeNote(summary.throttle)
      );

    this._renderLegend();
    this._renderChart();
  }

  // Under the time: "grind finer" for a shot the throttle found too fast.
  // With the reason when the target time was not reached.
  _timeNote(throttle) {
    // A pump profile that did not run: "Prepare brew" was off at the relay
    // start. Brews stored before 0.7.0b2 carry the old gate "no_tare"; they
    // keep their own text, which is what really happened to them.
    const reason = throttle && throttle.profile ? throttle.profile_reason : null;
    const skipped = reason === "not_prepared"
      ? "profile_skipped"
      : reason === "no_tare" ? "profile_skipped_no_tare" : null;
    if (skipped) return `${escapeHtml(profileName(throttle.profile, lang(this._hass)))} ${this._t(skipped)}`;
    if (!throttle || !throttle.grind_finer) return "";
    const why = throttle.unreachable && throttle.unreachable_reason
      ? this._t(`unreachable_${throttle.unreachable_reason}`)
      : "";
    return why ? `${this._t("grind_finer")} · ${why}` : this._t("grind_finer");
  }

  // Throttle view of what the chart shows (live or stored), or null.
  _chartThrottle() {
    if (this._readyView()) return null;
    if (this._live) {
      const th = this._live.throttle;
      return th ? throttleView(th, th.relay_offset) : null;
    }
    const summary = this._brews && this._brews[this._index];
    if (!summary) return null;
    return throttleView(summary.throttle, summary.relay_offset);
  }

  // "target 40.0 g" for a target in grams; "" without one.
  _targetText(target) {
    if (target === null || target === undefined) return "";
    return `${this._t("target")} ${this._fmt(target, 1)} g`;
  }

  // The pump profile a brew ran with (0.7.0b6, its throttle record's
  // profile), as HTML: a span of its own so a long name is cut by an
  // ellipsis without pushing the rest out. "" for records without one
  // (before 0.7.0) - never a made-up "Standard".
  _profileHtml(throttle) {
    const label = throttle && throttle.profile;
    if (label == null || String(label).trim() === "") return "";
    const name = escapeHtml(profileName(label, lang(this._hass)));
    return `<span class="brew-profile" title="${name}">${name}</span>`;
  }

  // Bean, pump profile and shot metrics of a stored brew as HTML (user
  // data escaped), "" when there is nothing.
  _detailHtml(summary) {
    const parts = this._detailParts(summary).map((p) => escapeHtml(p));
    const profile = this._profileHtml(summary.throttle);
    if (profile) parts.splice(summary.bean ? 1 : 0, 0, profile);
    return parts.join(" · ");
  }

  // Bean and shot metrics of a stored brew (plain text parts).
  _detailParts(summary) {
    const parts = [];
    if (summary.bean) parts.push(summary.bean);
    const m = summary.metrics || {};
    // In shot time (relay on) when known, else from the scale timer start.
    const drop = summary.first_drop_shot != null ? summary.first_drop_shot : m.first_drop;
    if (drop != null) parts.push(`${this._t("first_drop")} ${this._fmt(drop, 1)} s`);
    if (m.peak_flow != null) parts.push(`${this._t("peak_flow")} ${this._fmt(m.peak_flow, 1)} g/s`);
    if (m.mean_flow != null) {
      const cv = m.flow_cv != null ? ` ±${this._fmt(m.flow_cv * 100, 0)} %` : "";
      parts.push(`Ø ${this._fmt(m.mean_flow, 1)} g/s${cv}`);
    }
    return parts;
  }

  _renderLiveStats() {
    const e = this._els;
    const l = this._live;
    if (!e || (!l && !this._readyView())) return;
    let elapsed = 0;
    let now = { weight: 0, flow: 0 };
    let dose = null;
    let target = null;
    if (l) {
      const phase = l.phase || "brewing";
      e.badgeText.textContent = this._t(l.replay ? "replay" : "live");
      e.badge.classList.toggle("parked", phase === "parked");
      e.badge.classList.toggle("tail", phase === "tail");
      e.when.textContent = this._t(phase) || "";
      elapsed = this._liveElapsed();
      now = this._liveNow();
      dose = l.dose;
      target = l.target;
    } else {
      e.when.textContent = this._t("ready");
      dose = this._readyDose;
      target = this._readyTarget;
    }
    const stat = (cls, label, value, unit, note) => `
      <div class="stat ${cls}">
        <div class="label">${label}</div>
        <div class="value">${value}${unit ? `<span class="unit">${unit}</span>` : ""}</div>
        <div class="stat-note">${note || ""}</div>
      </div>`;
    // Shot time (from the pump relay) once the relay start is known.
    const th = l ? l.throttle : null;
    const offset = th && th.relay_offset != null ? th.relay_offset : 0;
    const html =
      stat("big", this._t("time"), this._fmt(elapsed + offset, 1), "s", this._timeNote(th)) +
      stat("big flow", this._t("flow_rate"), this._fmt(now.flow, 1), "g/s") +
      stat("big", this._t("weight"), this._fmt(now.weight, 1), "g", this._targetText(target)) +
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
    const trend = !this._live && !this._readyView() ? this._chartBrew()?.trend || [] : [];
    if (this._hover) {
      const [t, w, f] = this._hover;
      const tp = trendAt(trend, t);
      const smooth = tp ? ` (Ø ${this._fmt(tp[1], 1)})` : "";
      const pump = this._pumpAt(t);
      const pumpText = pump != null ? ` · ${this._t("pump")} ${this._fmt(pump, 0)} %` : "";
      readout = `${this._fmt(t, 1)} s · ${this._fmt(f, 1)} g/s${smooth} · ${this._fmt(w, 1)} g${pumpText}`;
    }
    const trendKey = trend.length
      ? `<span class="key trend"><span class="swatch"></span><span class="key-text">${this._t("trend")}</span></span>`
      : "";
    // A picked point replaces the keys, so the row never wraps on a phone
    // (the chart height counts the legend and would jump).
    this._legendPumpKey = this._pumpKey();
    e.legend.innerHTML = readout
      ? `<span class="readout">${readout}</span>`
      : `
      <span class="key flow"><span class="swatch"></span><span class="key-text">${this._t("flow")} (g/s)</span></span>
      ${trendKey}
      <span class="key weight"><span class="swatch"></span><span class="key-text">${this._t("weight")} (g)</span></span>
      ${this._legendPumpKey}
      <span class="readout"></span>`;
  }

  // Legend key of the pump band: "Pump (%)", with the profile's name; its
  // swatch dashed like the line when the power was only observed.
  _pumpKey() {
    const view = this._chartThrottle();
    if (!view || !view.pump) return "";
    const name = view.profile
      ? ` · <span class="profile-name">${escapeHtml(profileName(view.profile, lang(this._hass)))}</span>`
      : "";
    const cls = view.observe ? "key pump observe" : "key pump";
    return `<span class="${cls}"><span class="swatch"></span><span class="key-text">${this._t("pump")} (%)${name}</span></span>`;
  }

  // Drawn pump power step line of the chart's brew (profile time, like the
  // flow points), [] when there is none. ``g``: geometry; ``tLast``: the
  // newest drawn point (the end without a timer stop).
  _pumpSteps(view, brew, g, tLast) {
    if (!view || !view.pump || !g.band || !view.power.length) return [];
    let tEnd = brew.duration != null ? brew.duration : tLast;
    if (tEnd == null || tEnd > g.tMax) tEnd = g.tMax;
    return powerSteps(view.power, tEnd).filter((p) => p[0] <= g.tMax + 1e-6);
  }

  // Pump power (%) at chart time t as drawn; null without a pump band and
  // when the power was only observed (not set: no value to read).
  _pumpAt(t) {
    const view = this._chartThrottle();
    if (!view || !view.pump || view.observe || !view.power.length) return null;
    const brew = this._chartBrew();
    if (!brew) return null;
    const g = this._geometry(brew);
    const pts = g.pts;
    return stepAt(this._pumpSteps(view, brew, g, pts.length ? pts[pts.length - 1][0] : null), t);
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
    if (brew.live) return { ...brew, profile: this._liveVisible(pts) };
    let trend = this._trendCache.get(brew);
    if (!trend) {
      trend = trendFlow(brew.profile || [], brew.duration);
      this._trendCache.set(brew, trend);
    }
    return { ...brew, profile: pts, trend };
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
      for (const p of brew.trend || []) if (p[1] > fPeak) fPeak = p[1];
      // The axis reaches the target shot time marker too (a shot that
      // ended early still shows where it should have ended).
      const view = this._chartThrottle();
      const tTarget = view && view.targetT != null ? view.targetT + 1 : 0;
      tAxis = axis(Math.max(pts.length ? pts[pts.length - 1][0] : 0, tTarget), 5, tCount);
      fAxis = axis(fPeak, 1, 4);
      wAxis = axis(wPeak, 10, 4);
      tMax = tAxis.max;
      fMax = fAxis.max;
      wMax = wAxis.max;
    }
    const th = this._chartThrottle();
    const band = th && th.pump ? BAND_H : 0;
    const plotH = this._chartH - PAD.top - PAD.bottom - band;
    return {
      band,
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
      this._renderThrottle(svg, g, brew, null);
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
    if (brew.trend && brew.trend.length) {
      svg.appendChild(
        svgEl("path", {
          d: monotonePath(brew.trend.map((p) => [g.x(p[0]), g.yF(p[1])])),
          fill: "none",
          stroke: "var(--primary-color)",
          "stroke-width": 1.5,
          "stroke-dasharray": "1 4",
          "stroke-linejoin": "round",
          "stroke-linecap": "round",
        })
      );
    }

    this._renderThrottle(svg, g, brew, last[0]);
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
      const tp = trendAt(brew.trend, t);
      if (tp) {
        svg.appendChild(
          svgEl("circle", { cx: xh, cy: g.yF(tp[1]), r: 3, fill: "var(--primary-color)" })
        );
      }
      const pump = this._pumpAt(t);
      if (pump != null) {
        svg.appendChild(
          svgEl("circle", {
            cx: xh,
            cy: this._bandY(g)(pump),
            r: 3,
            fill: PUMP_COLOR,
            "data-role": "pump-hover",
          })
        );
      }
    }
    box.appendChild(svg);
  }

  // Pump power band (step line, 0-100 %) under the plot and the target
  // shot time marker. Colours from theme variables only.
  _renderThrottle(svg, g, brew, tLast) {
    const view = this._chartThrottle();
    if (!view) return;
    const accent = "var(--accent-color, var(--primary-text-color))";
    if (view.targetT != null && view.targetT > 0 && view.targetT <= g.tMax) {
      const xt = g.x(view.targetT);
      svg.appendChild(
        svgEl("line", {
          x1: xt,
          x2: xt,
          y1: PAD.top,
          y2: PAD.top + g.plotH + (g.band ? g.band - 2 : 0),
          stroke: accent,
          "stroke-width": 1,
          "stroke-dasharray": "6 3",
          opacity: 0.8,
          "data-role": "target-time",
        })
      );
    }
    if (!g.band) return;
    const top = PAD.top + g.plotH + BAND_GAP;
    const h = g.band - BAND_GAP - 2;
    const yP = this._bandY(g);
    svg.appendChild(
      svgEl("line", {
        x1: PAD.left,
        x2: PAD.left + g.plotW,
        y1: top + h,
        y2: top + h,
        stroke: "var(--divider-color)",
        "stroke-width": 1,
      })
    );
    const label = svgEl("text", {
      x: PAD.left + g.plotW + 6,
      y: top + 8,
      "text-anchor": "start",
      "font-size": 9,
      fill: "var(--secondary-text-color)",
    });
    label.textContent = "100%";
    svg.appendChild(label);
    const pts = this._pumpSteps(view, brew, g, tLast);
    if (!pts.length) return;
    const d = pts
      .map((p, i) => `${i ? "L" : "M"}${g.x(Math.max(0, p[0])).toFixed(1)},${yP(p[1]).toFixed(1)}`)
      .join("");
    svg.appendChild(
      svgEl("path", {
        d,
        fill: "none",
        stroke: PUMP_COLOR,
        "stroke-width": 1.5,
        "stroke-dasharray": view.observe ? "3 3" : "none",
        "stroke-linejoin": "round",
        "data-role": "pump-power",
      })
    );
  }

  // y of a pump power (0-100 %) in the band.
  _bandY(g) {
    const top = PAD.top + g.plotH + BAND_GAP;
    const h = g.band - BAND_GAP - 2;
    return (d) => top + h - (Math.max(0, Math.min(100, d)) / 100) * h;
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
        ({
          device_id: s.device,
          chart_height: s.chart_height,
          expand_when_active: s.expand_when_active,
          flow_smoothing: s.flow_smoothing,
        }[schema.name] || s.card_title);
      this._form.computeHelper = (schema) =>
        ({
          chart_height: s.chart_height_helper,
          expand_when_active: s.expand_when_active_helper,
          flow_smoothing: s.flow_smoothing_helper,
        }[schema.name]);
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
      { name: "expand_when_active", selector: { boolean: {} } },
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

// ------------------------------------------------- profile preview feature
//
// Tile feature "custom:difluid-profile-preview" (0.7.0b3) for the pump
// profile select of this integration: the selected profile's pump power
// course as a small graph; for the adaptive throttle profile (x_throttle
// "target_time", the built-in Zielzeit) the target shot time number of the
// same scale as − value + buttons instead. The definition comes from the
// websocket command difluid_scale/profile (built-in and imported alike).

// A last phase whose cap is longer than this runs "until the shot ends"
// (built-ins: 120 s, longer than any espresso; the auto-stop ends it).
const PREVIEW_OPEN_PHASE = 60; // s
const PREVIEW_SAMPLES = 12; // points per eased transition
const PREVIEW_TAIL_MIN = 4; // s, open end drawn after the defined part
const PREVIEW_TAIL_SHARE = 0.25; // of the defined part
// Layout (px): end criteria chips over the graph, the graph, the phase
// names under it; the brew ratio row under everything.
const PREVIEW_CHIP_H = 14;
const PREVIEW_GRAPH_H = 36;
const PREVIEW_NAMES_H = 12;
const PREVIEW_LABEL_PAD = 2; // px between neighbouring names
const PREVIEW_LABEL_MIN = 20; // px: narrower sections get no name (< ~3 characters)
const PREVIEW_CHAR_W = 5.6; // px, estimated mean character width at 10 px
const PREVIEW_CHIP_PAD = 12; // px, a chip's horizontal padding (both sides)
const PREVIEW_CHIP_GAP = 3; // px, at least between two chips
const PREVIEW_RATIO_W = 156; // px, a compact − value + input
const PREVIEW_ROW_H = 32; // px, a labelled input row
const PREVIEW_TOP = 4; // px over the tile's feature gap (12): room under the dropdown
const PREVIEW_BLOCK_GAP = 16; // px between the graph (or the main row) and the ratio row
const PREVIEW_RETRY_MS = 10000; // first retry after a failed fetch
const PREVIEW_RETRY_MAX_MS = 300000; // longest pause between retries

// Transition curve 0..1 -> 0..1 (profiles.ease).
function previewEase(kind, x) {
  x = Math.min(1, Math.max(0, x));
  if (kind === "ease-in") return x * x;
  if (kind === "ease-out") return 1 - (1 - x) ** 2;
  if (kind === "ease-in-out") return 3 * x * x - 2 * x * x * x;
  if (kind === "linear") return x;
  return 1;
}

// Power as the engine sets it: 0 stays 0 (a bloom), anything else in
// [floor, 100] (ProfileRun._clamp/quantize; the pump's own grid is unknown
// here, so 1 % steps).
function previewPower(value, floor) {
  if (value <= 0) return 0;
  return Math.min(100, Math.max(floor, value));
}

// Profile definition (websocket difluid_scale/profile) -> what the feature
// shows. {kind: "target"} for the adaptive throttle profile; null without
// phases; else {kind: "graph", segments, phases, defined, tMax}. Segments:
// {type: "line", pts: [[t, %]], dashed, fade} or {type: "open", t0, t1,
// fade} (a range where the power is not fixed: 0-100 % open). Rules (what
// the engine does, nothing invented):
// - phases in order, each from the power at its start; transitions sampled
//   with their curve, cut at the phase end;
// - a phase with exit targets (weight/flow/ratio) may end before its cap:
//   drawn to its cap, dashed;
// - a flow phase: the controller sets the power live: an open range over
//   the full height; a transition starting after it starts from an unknown
//   power: open as well until it ends;
// - the last phase with a cap over PREVIEW_OPEN_PHASE runs until the shot
//   ends: drawn to the end of its transition, then an open end that fades
//   out (schematic length, not a time); after a shorter last phase the
//   pump goes back to 100 %, also as an open end.
// ``defined``: where the defined part ends (s; the open end follows);
// ``phases``: [{name, t0, t1}] per profile phase, for the labels on the
// x axis. A phase includes its own transition (the engine runs a phase's
// transition from the phase start: ProfileRun._transition); the open last
// phase runs to the end of the drawing; the return to 100 % after a short
// last phase is no phase (not in the list). ``targets`` (exit targets)
// and ``duration`` (s; null for the open last phase): its end criteria;
// ``open``: the last phase runs until the shot ends. Open segments of a
// flow phase carry its target ``flow`` (g/s).
function profilePreview(profile, floor) {
  if (!profile || !Array.isArray(profile.phases) || !profile.phases.length) return null;
  if (profile.throttle === "target_time") return { kind: "target" };
  const fl = Number.isFinite(floor) ? floor : 0;
  const segments = [];
  let t = 0;
  let prev = 100; // the engine's start power; null: unknown (after a flow phase)
  let tail = null; // {type: "line", value} | {type: "open"}
  const phases = profile.phases;
  const labels = [];
  for (let i = 0; i < phases.length; i++) {
    const ph = phases[i];
    const last = i === phases.length - 1;
    const isFlow = ph.flow != null;
    const hasTargets = Array.isArray(ph.targets) && ph.targets.length > 0;
    const open = last && ph.duration > PREVIEW_OPEN_PHASE;
    const span = open ? null : Number(ph.duration);
    labels.push({
      name: typeof ph.name === "string" && ph.name.trim() ? ph.name.trim() : `Phase ${i + 1}`,
      t0: t,
      t1: open ? null : t + span, // null: to the end of the drawing
      // End criteria (ProfileRun._exit): the exit targets, whichever is
      // met first, and the duration, a maximum beside targets. The open
      // last phase's cap is no criterion (it runs until the shot ends).
      targets: hasTargets ? ph.targets.map((tg) => ({ ...tg })) : [],
      duration: open ? null : Number(ph.duration),
      open, // runs until the shot ends (at the target weight)
      flow: isFlow ? Number(ph.flow) : null, // a flow phase's target (g/s)
    });
    if (isFlow) {
      if (open) {
        tail = { type: "open", flow: ph.flow };
      } else {
        segments.push({ type: "open", t0: t, t1: t + span, fade: false, dashed: hasTargets, flow: ph.flow });
        t += span;
        if (last) tail = { type: "line", value: 100 };
      }
      prev = null;
      continue;
    }
    const goal = previewPower(Number(ph.power), fl);
    const td = ph.transition === "instant" || !ph.transition ? 0 : Number(ph.transition_duration) || 0;
    const tdEff = span == null ? td : Math.min(td, span);
    let pts;
    if (prev == null) {
      // From an unknown power: open until the transition's end.
      if (tdEff > 0) {
        segments.push({ type: "open", t0: t, t1: t + tdEff, fade: false, dashed: false });
      }
      const v = tdEff > 0 && tdEff < td ? null : goal; // cut transition: still unknown
      pts = v == null ? null : [[t + tdEff, v]];
    } else {
      pts = [[t, prev]];
      if (tdEff > 0) {
        for (let k = 1; k <= PREVIEW_SAMPLES; k++) {
          const x = (k / PREVIEW_SAMPLES) * tdEff;
          const v = prev + (goal - prev) * previewEase(ph.transition, x / td);
          pts.push([t + x, previewPower(v, fl)]);
        }
      } else {
        pts.push([t, goal]);
      }
    }
    const value = pts ? pts[pts.length - 1][1] : null;
    if (open) {
      t += tdEff;
      tail = value == null ? { type: "open" } : { type: "line", value };
    } else {
      t += span;
      // (Without pts the transition from an unknown power fills the
      // whole phase: already open.)
      if (pts) pts.push([t, value]);
      if (last) tail = { type: "line", value: 100 };
    }
    if (pts && (pts[pts.length - 1][0] > pts[0][0] || pts.some((p) => p[1] !== pts[0][1]))) {
      segments.push({ type: "line", pts, dashed: hasTargets, fade: false });
    }
    prev = value;
  }
  const defined = t;
  const len = Math.max(PREVIEW_TAIL_MIN, defined * PREVIEW_TAIL_SHARE);
  if (tail.type === "open") {
    segments.push({ type: "open", t0: defined, t1: defined + len, fade: true, dashed: false, flow: tail.flow ?? null });
  } else {
    const from = prev == null ? tail.value : prev;
    segments.push({
      type: "line",
      pts: [[defined, from], [defined, tail.value], [defined + len, tail.value]],
      dashed: false,
      fade: true,
    });
  }
  const tMax = defined + len;
  for (const l of labels) if (l.t1 == null) l.t1 = tMax;
  return { kind: "graph", segments, phases: labels, defined, tMax };
}

// Whether a card context is this integration's pump profile select.
function isProfileSelect(hass, context) {
  const id = context && context.entity_id;
  if (!hass || !id || !id.startsWith("select.")) return false;
  const ent = hass.entities && hass.entities[id];
  if (!ent || ent.platform !== DOMAIN) return false;
  if (ent.translation_key != null) return ent.translation_key === "pump_profile";
  // Without the translation key: the select's own attribute (a key, not a
  // display name).
  const st = hass.states && hass.states[id];
  return !!(st && st.attributes && "first_drop_deadline" in st.attributes);
}

// Places the end criteria chips: each centred on its x, kept inside
// [0, width]; overlapping neighbours are moved apart (each by half the
// overlap, inside the edges), but never further than half their width
// from their border. What does not fit that way is left out whole, never
// shortened (a chip without its duration would claim the phase ends only
// at its exit): first the end chip (``end``: the shot's end), then the
// latest chip that is still in conflict. items: [{x, parts, end?}] in x
// order.
function layoutChips(items, width) {
  const chips = items.map((it) => ({
    x: it.x, end: !!it.end, text: it.parts.join(" / "),
    width: it.parts.join(" / ").length * PREVIEW_CHAR_W + PREVIEW_CHIP_PAD,
  }));
  for (;;) {
    if (!chips.length) return [];
    const placed = chips.map((c) => ({ ...c, left: Math.min(Math.max(0, c.x - c.width / 2), width - c.width) }));
    // Overlapping neighbours share the move (half each), inside the edges,
    // until nothing overlaps (or nothing moves any more).
    for (let round = 0; round < 100; round++) {
      let moved = false;
      for (let i = 1; i < placed.length; i++) {
        const a = placed[i - 1];
        const b = placed[i];
        const overlap = a.left + a.width + PREVIEW_CHIP_GAP - b.left;
        if (overlap <= 1e-6) continue;
        const aLeft = Math.max(0, a.left - overlap / 2);
        const bLeft = Math.min(width - b.width, b.left + overlap / 2);
        if (aLeft !== a.left || bLeft !== b.left) moved = true;
        a.left = aLeft;
        b.left = bLeft;
      }
      if (!moved) break;
    }
    const overlaps = (i) => i > 0
      && placed[i - 1].left + placed[i - 1].width + PREVIEW_CHIP_GAP - placed[i].left > 1e-6;
    const bad = placed.map((c, i) => c.left < -1e-6 || c.width > width + 1e-6 || overlaps(i)
      || Math.abs(c.left + c.width / 2 - c.x) > c.width / 2 + 1e-6);
    if (!bad.some(Boolean)) {
      return placed.map((c) => ({ text: c.text, left: Math.max(0, c.left), width: c.width, x: c.x }));
    }
    const end = chips.findIndex((c) => c.end);
    if (end >= 0) chips.splice(end, 1);
    else chips.splice(bad.lastIndexOf(true), 1);
  }
}

const ICON_MINUS = "M19,13H5V11H19V13Z";
const ICON_PLUS = "M19,13H13V19H11V13H5V11H11V5H13V11H19V13Z";

class DifluidProfilePreview extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._context = null;
    this._def = null; // websocket result
    this._key = null; // what the definition was fetched for
    this._token = 0;
    this._width = 0;
    this._pending = {}; // entity id -> {value sent, for: the state object it was sent from}
    this._lastHeight = 0;
    this._floorEntity = null; // from the first definition (refetch key)
    this._loading = false;
    this._failures = 0;
    this._retryAt = null; // a failed fetch: retry from this time (ms)
    this._html = null;
    if (this.shadowRoot && this.shadowRoot.addEventListener) {
      this.shadowRoot.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest ? ev.target.closest("[data-step]") : null;
        if (btn) {
          ev.stopPropagation();
          this._step(Number(btn.dataset.step), btn.dataset.target || "main");
        }
      });
    }
  }

  static getStubConfig() {
    return { type: "custom:difluid-profile-preview" };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  get hass() {
    return this._hass;
  }

  set context(context) {
    this._context = context;
    this._update();
  }

  // Before the context API (older Home Assistant): the entity's state.
  set stateObj(stateObj) {
    if (!this._context && stateObj) this._context = { entity_id: stateObj.entity_id };
    this._update();
  }

  connectedCallback() {
    if (!this._resize && typeof ResizeObserver !== "undefined") {
      this._resize = new ResizeObserver((entries) => {
        const w = Math.round(entries[0].contentRect.width);
        if (w && w !== this._width) {
          this._width = w;
          this._render();
        }
      });
      this._resize.observe(this);
    }
  }

  disconnectedCallback() {
    if (this._resize) this._resize.disconnect();
    this._resize = null;
  }

  _t(key) {
    return STRINGS[lang(this._hass)][key] || STRINGS.en[key] || key;
  }

  _entityId() {
    return this._context && this._context.entity_id;
  }

  // Refetch when the entity, the selected profile, its definition
  // (revision: a profile imported over it), the profile list or the floor
  // change.
  _fetchKey() {
    const id = this._entityId();
    const st = this._hass && id ? this._hass.states[id] : null;
    if (!st) return null;
    const a = st.attributes || {};
    const floorId = this._floorEntity;
    const floor = floorId && this._hass.states[floorId] ? this._hass.states[floorId].state : "";
    const options = a.options || [];
    // The language too: the phase names come translated (0.7.0b5).
    return [id, st.state, a.profile_revision ?? "", options.join("\u0001"), floor, this._language() || ""]
      .join("\u0000");
  }

  // The frontend's language (the user's profile first).
  _language() {
    const h = this._hass;
    return (h && ((h.locale && h.locale.language) || h.language)) || undefined;
  }

  _update() {
    if (!this._hass || !this._config || !this._entityId()) {
      this._render();
      return;
    }
    const key = this._fetchKey();
    if (key !== null && key !== this._key) {
      this._key = key;
      this._failures = 0;
      // Another profile (or entity): its old definition is not shown while
      // the new one loads, a placeholder of the same height instead (no
      // jump). The same profile (floor, list, revision changed) stays until
      // the answer.
      const sel = key.split("\u0000").slice(0, 2).join("\u0000");
      if (sel !== this._defSel) {
        this._def = null;
        this._loading = true;
      }
      this._defSel = sel;
      this._fetch(key);
    } else if (key !== null && this._retryAt != null && Date.now() >= this._retryAt) {
      // A failed fetch is retried on a later update, with a growing pause.
      this._fetch(key);
    }
    this._render();
  }

  async _fetch(key) {
    const token = ++this._token;
    this._retryAt = null;
    const id = this._entityId();
    const st = this._hass.states[id];
    let def = null;
    let failed = false;
    try {
      def = await this._hass.callWS({
        type: `${DOMAIN}/profile`,
        entity_id: id,
        label: st ? st.state : undefined,
        // Built-in profile and phase names in the frontend's language.
        language: this._language(),
      });
    } catch (_err) {
      failed = true; // unknown profile, scale not loaded, connection lost
    }
    // A newer fetch (or a newer selection) wins, in any answer order.
    if (token !== this._token || key !== this._key) return;
    this._loading = false;
    if (failed) {
      this._failures = (this._failures || 0) + 1;
      this._retryAt = Date.now() + Math.min(
        PREVIEW_RETRY_MAX_MS, PREVIEW_RETRY_MS * 2 ** (this._failures - 1));
      this._def = null;
    } else {
      this._failures = 0;
      this._def = def;
      if (def && def.floor_entity !== this._floorEntity) {
        // The floor entity is known now: key it in (no refetch for that).
        this._floorEntity = def.floor_entity;
        this._key = this._fetchKey();
      }
    }
    this._render();
  }

  // What the feature shows: {mode: "graph", preview} | {mode: "target"} |
  // {mode: "standard"} (0.7.0b4: the Standard power instead of a flat
  // line), each with the brew ratio row when its number exists; null:
  // nothing (no definition; Zielzeit/Standard without their number).
  _view() {
    const def = this._def;
    if (!def || !def.profile) return null;
    const preview = profilePreview(def.profile, def.floor);
    if (!preview) return null;
    const ratio = this._numberState(def.ratio_entity);
    let view;
    if (preview.kind === "target") {
      const st = this._numberState(def.target_time_entity);
      view = st ? { mode: "target", entity: def.target_time_entity, state: st } : null;
    } else if (def.profile.standard) {
      const st = this._numberState(def.standard_power_entity);
      view = st ? { mode: "standard", entity: def.standard_power_entity, state: st } : null;
    } else {
      view = { mode: "graph", preview };
    }
    // Zielzeit/Standard without their number: the ratio row alone.
    if (!view) view = ratio ? { mode: "none" } : null;
    if (!view) return null;
    if (ratio) view.ratio = { entity: def.ratio_entity, state: ratio };
    return view;
  }

  _numberState(id) {
    return id && this._hass ? this._hass.states[id] || null : null;
  }

  // A flag of the integration: the select's attribute (live: mode,
  // options, pump state, auto-stop changes), else the definition's.
  // "pump_controllable": the pump power is really controlled;
  // "auto_stop_target": the auto-stop ends the shot at a target.
  _flag(key) {
    const st = this._hass && this._entityId() ? this._hass.states[this._entityId()] : null;
    const attr = st && st.attributes ? st.attributes[key] : undefined;
    if (typeof attr === "boolean") return attr;
    return !!(this._def && this._def[key]);
  }

  _render() {
    const view = this._view();
    let html = "";
    if (view) {
      let main;
      if (view.mode === "graph") main = this._graphHtml(view.preview);
      else if (view.mode === "target") main = this._rowHtml("main", view.state, { kind: "time" });
      else if (view.mode === "standard") {
        main = this._rowHtml("main", view.state, { kind: "power", locked: !this._flag("pump_controllable") });
      } else main = "";
      const ratio = view.ratio ? this._rowHtml("ratio", view.ratio.state, { kind: "ratio" }) : "";
      html = `<div class="mode-${view.mode}">${main}${ratio}</div>`;
    } else if (this._loading && this._html) {
      // Same height as before while the next definition loads.
      html = `<div class="placeholder" data-role="loading" style="height: ${Math.max(0, (this._lastHeight || 46) - PREVIEW_TOP)}px"></div>`;
    }
    // Nothing to show: no height of its own.
    if (this.style) this.style.display = html ? "" : "none";
    if (html === this._html) return;
    this._html = html;
    if (view) this._lastHeight = this._heightOf(view);
    this.shadowRoot.innerHTML = html ? `<style>${DifluidProfilePreview.styles}</style>${html}` : "";
  }

  // Height of a view (px): stable per kind, whatever the profile. The top
  // padding adds to the tile's own feature gap (more room under the
  // dropdown); the block gap separates the graph or the main row from the
  // ratio row.
  _heightOf(view) {
    const main = view.mode === "graph"
      ? PREVIEW_CHIP_H + 4 + PREVIEW_GRAPH_H + PREVIEW_NAMES_H
      : view.mode === "none" ? 0 : PREVIEW_ROW_H;
    return PREVIEW_TOP + main + (view.ratio ? (main ? PREVIEW_BLOCK_GAP : 0) + PREVIEW_ROW_H : 0);
  }

  _fmt(value, digits) {
    return Number(value).toLocaleString(lang(this._hass), {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  // A number with as many decimals as it has (up to 2).
  _num(v) {
    const d = Number.isInteger(v) ? 0 : Number.isInteger(Math.round(v * 1000) / 100) ? 1 : 2;
    return this._fmt(v, d);
  }

  // What ends a phase (ProfileRun._exit), for its chip: the exit targets in
  // their order — weight = the cup weight the scale shows (net, after the
  // tare), flow = the scale flow (after the first drops), ratio = x the
  // brew's dose as a weight; "≥" (gte, the default) or "≤" (lte), each held
  // 1 s; the first met ends the phase — then the duration, which ends it at
  // the latest. The open last phase's cap is not shown (it runs until the
  // shot ends).
  _criteria(ph) {
    const out = [];
    for (const tg of ph.targets || []) {
      const lte = tg.operator === "lte";
      const v = Number(tg.value);
      if (tg.type === "volumetric") out.push(`${lte ? "≤ " : ""}${this._num(v)} g`);
      else if (tg.type === "flow") out.push(`${lte ? "≤" : "≥"} ${this._num(v)} g/s`);
      else if (tg.type === "ratio") out.push(`${lte ? "≤ " : ""}1 : ${this._num(v)}`);
    }
    if (ph.duration != null) out.push(`${this._num(ph.duration)} s`);
    return out;
  }

  // Chips: at every phase end the criteria that end it; at the right end
  // the shot's end at the target weight (with an open last phase's own
  // targets before it) — only when the auto-stop has a target; without one
  // the shot ends by hand and there is no end chip.
  _chipItems(preview, x) {
    const items = [];
    const last = [];
    for (const ph of preview.phases) {
      if (ph.open) last.push(...this._criteria(ph));
      else items.push({ x: x(ph.t1), parts: this._criteria(ph) });
    }
    if (this._flag("auto_stop_target")) {
      last.push(this._t("preview_target_weight"));
      items.push({ x: x(preview.tMax), parts: last, end: true });
    }
    return items.filter((it) => it.parts.length);
  }

  _graphHtml(preview) {
    const W = this._width || this.clientWidth || 300;
    const H = PREVIEW_GRAPH_H;
    const plotW = Math.max(20, W - 8);
    const top = 3;
    const bottom = H - 2;
    const x = (t) => 4 + (t / preview.tMax) * plotW;
    const y = (p) => bottom - (Math.max(0, Math.min(100, p)) / 100) * (bottom - top);
    const last = preview.segments[preview.segments.length - 1];
    const tailT0 = last.type === "open" ? last.t0 : last.pts[0][0];
    const tailT1 = last.type === "open" ? last.t1 : last.pts[last.pts.length - 1][0];
    const marks = preview.segments.map((seg) => {
      if (seg.type === "open") {
        // Power open (the controller's, live): the full height, hatched;
        // a flow phase's target flow in the middle, on a halo of the card
        // colour so the hatching does not cross it; left out (not cut) when
        // the area is too narrow.
        const x0 = x(seg.t0);
        const w = Math.max(0, x(seg.t1) - x0);
        const mask = seg.fade ? ' mask="url(#difluid-fade-mask)"' : "";
        const edge = seg.dashed
          ? `<rect x="${x0.toFixed(1)}" y="${top}" width="${w.toFixed(1)}" height="${bottom - top}" fill="none" stroke="${PUMP_COLOR}" stroke-width="1" stroke-dasharray="4 3"></rect>`
          : "";
        const flowText = seg.flow != null ? `${this._num(seg.flow)} g/s` : "";
        const label = flowText && flowText.length * PREVIEW_CHAR_W + 8 <= w
          ? `<text x="${(x0 + w / 2).toFixed(1)}" y="${((top + bottom) / 2 + 3.5).toFixed(1)}" text-anchor="middle" data-role="flow-target">${escapeHtml(flowText)}</text>`
          : "";
        return `<g data-role="${seg.fade ? "open-end" : "open"}"${mask}>`
          + `<rect x="${x0.toFixed(1)}" y="${top}" width="${w.toFixed(1)}" height="${bottom - top}" fill="${PUMP_COLOR}" fill-opacity="0.15"></rect>`
          + `<rect x="${x0.toFixed(1)}" y="${top}" width="${w.toFixed(1)}" height="${bottom - top}" fill="url(#difluid-hatch)"></rect>`
          + `${edge}</g>${label}`;
      }
      const d = seg.pts
        .map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`)
        .join("");
      const stroke = seg.fade ? "url(#difluid-fade)" : PUMP_COLOR;
      const dash = seg.dashed ? ' stroke-dasharray="4 3"' : "";
      const role = seg.fade ? "end" : seg.dashed ? "uncertain" : "fixed";
      return `<path d="${d}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"${dash} data-role="${role}"></path>`;
    });
    const x0 = x(tailT0).toFixed(1);
    const x1 = x(tailT1).toFixed(1);
    // Phase borders: fine ticks (also where the unnamed return to 100 %
    // starts); not at the drawing's ends.
    const ticks = preview.phases
      .filter((ph) => ph.t1 < preview.tMax - 1e-9)
      .map((ph) => {
        const xt = x(ph.t1).toFixed(1);
        return `<line x1="${xt}" x2="${xt}" y1="${top}" y2="${H}" stroke="var(--divider-color)" stroke-width="1" data-role="phase-tick"></line>`;
      })
      .join("");
    const chips = layoutChips(this._chipItems(preview, x), W)
      .map((c) => `<span class="chip" data-role="chip" style="left: ${c.left.toFixed(1)}px; width: ${c.width.toFixed(1)}px">${escapeHtml(c.text)}</span>`)
      .join("");
    // Names centred under their section, cut with an ellipsis (CSS); left
    // out where the section is too narrow for a few characters.
    const names = preview.phases
      .map((ph) => {
        const left = x(ph.t0) + PREVIEW_LABEL_PAD;
        const width = x(ph.t1) - x(ph.t0) - 2 * PREVIEW_LABEL_PAD;
        if (width < PREVIEW_LABEL_MIN) return "";
        return `<span class="phase" data-role="phase-label" style="left: ${left.toFixed(1)}px; width: ${width.toFixed(1)}px">${escapeHtml(ph.name)}</span>`;
      })
      .join("");
    return `
      <div class="graph">
        <div class="chips">${chips}</div>
        <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
          <defs>
            <linearGradient id="difluid-fade" gradientUnits="userSpaceOnUse" x1="${x0}" y1="0" x2="${x1}" y2="0">
              <stop offset="0" style="stop-color: ${PUMP_COLOR}; stop-opacity: 1"></stop>
              <stop offset="1" style="stop-color: ${PUMP_COLOR}; stop-opacity: 0"></stop>
            </linearGradient>
            <mask id="difluid-fade-mask" maskUnits="userSpaceOnUse" style="mask-type: alpha">
              <rect x="${x0}" y="0" width="${Math.max(0, x1 - x0).toFixed(1)}" height="${H}" fill="url(#difluid-fade)"></rect>
            </mask>
            <pattern id="difluid-hatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="6" stroke="${PUMP_COLOR}" stroke-width="1.5" stroke-opacity="0.6"></line>
            </pattern>
          </defs>
          <line x1="4" x2="${(4 + plotW).toFixed(1)}" y1="${bottom}" y2="${bottom}" stroke="var(--divider-color)" stroke-width="1"></line>
          ${ticks}
          ${marks.join("")}
        </svg>
        <div class="phases">${names}</div>
      </div>`;
  }

  // The value a number shows: a sent one until the entity's state changes
  // (to it, or by anyone else) or the call fails; else its state.
  _shownValue(id, st) {
    const p = this._pending[id];
    if (p && p.for !== st) delete this._pending[id];
    return this._pending[id] ? this._pending[id].value : Number(st.state);
  }

  // − value + (HA's number buttons, their tokens), compact. opts.kind:
  // "time", "power" (locked: the power is not controlled, fixed 100 %, not
  // operable) or "ratio" ("1 : x").
  _buttonsHtml(role, st, opts) {
    const a = st.attributes || {};
    const locked = !!opts.locked;
    const unavailable = st.state === "unavailable" || st.state === "unknown";
    const current = this._shownValue(st.entity_id, st);
    const step = Number(a.step) || 1;
    const digits = step < 1 ? Math.min(3, String(step).split(".")[1]?.length || 1) : 0;
    let value;
    if (locked) value = "100 %";
    else if (unavailable || !Number.isFinite(current)) value = "–";
    else if (opts.kind === "ratio") value = current > 0 ? `1 : ${this._fmt(current, digits)}` : this._t("preview_off");
    else value = `${this._fmt(current, digits)}${a.unit_of_measurement ? ` ${escapeHtml(a.unit_of_measurement)}` : ""}`;
    const off = locked || unavailable;
    const dis = (cond) => (off || cond ? ' disabled aria-disabled="true"' : "");
    const atMin = a.min != null && current <= Number(a.min);
    const atMax = a.max != null && current >= Number(a.max);
    const icon = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"></path></svg>`;
    const cls = ["number", "compact", off ? "unavailable" : "", locked ? "locked" : ""]
      .filter(Boolean).join(" ");
    const what = opts.kind;
    return `
      <div class="${cls}" data-role="${role}">
        <button class="button minus" data-step="-1" data-target="${role}" aria-label="${this._t(`preview_dec_${what}`)}"${dis(atMin)}>${icon(ICON_MINUS)}</button>
        <span class="value" data-role="value">${value}</span>
        <button class="button plus" data-step="1" data-target="${role}" aria-label="${this._t(`preview_inc_${what}`)}"${dis(atMax)}>${icon(ICON_PLUS)}</button>
      </div>`;
  }

  // A labelled row: the label right next to the compact − value +, the
  // pair right-aligned; the label outside the grey area (greyed out with
  // a locked input).
  _rowHtml(role, st, opts) {
    const label = this._t(`preview_label_${opts.kind}`);
    const rowRole = role === "ratio" ? "ratio-row" : "main-row";
    return `
      <div class="row${opts.locked ? " locked" : ""}" data-role="${rowRole}">
        <span class="row-label">${escapeHtml(label)}</span>
        ${this._buttonsHtml(role, st, opts)}
      </div>`;
  }

  // − / + : one step, snapped and clamped like HA's number buttons; sent
  // at once (number.set_value). target: "main" (target shot time or
  // Standard power) or "ratio".
  _step(dir, target = "main") {
    const view = this._view();
    if (!view || !this._hass) return;
    let entity;
    let st;
    if (target === "ratio") {
      if (!view.ratio) return;
      ({ entity, state: st } = view.ratio);
    } else {
      if (view.mode === "graph") return;
      if (view.mode === "none") return;
      if (view.mode === "standard" && !this._flag("pump_controllable")) return; // locked
      ({ entity, state: st } = view);
    }
    if (st.state === "unavailable" || st.state === "unknown") return;
    const a = st.attributes || {};
    const base = this._shownValue(entity, st);
    if (!Number.isFinite(base)) return;
    const step = Number(a.step) || 1;
    let value = Math.round((base + dir * step) / step) * step;
    if (a.min != null) value = Math.max(Number(a.min), value);
    if (a.max != null) value = Math.min(Number(a.max), value);
    value = Number(value.toFixed(6));
    if (value === base) return;
    this._pending[entity] = { value, for: st };
    this._render();
    const done = this._hass.callService("number", "set_value", { entity_id: entity, value });
    if (done && typeof done.catch === "function") {
      done.catch(() => {
        // Failed: back to the entity's state.
        const p = this._pending[entity];
        if (p && p.value === value) {
          delete this._pending[entity];
          this._render();
        }
      });
    }
  }
}

// Theme tokens only (the graph's yellow is the pump colour token); the
// buttons use the tokens of HA's number buttons (numeric-input feature).
DifluidProfilePreview.styles = `
  :host { display: block; width: 100%; padding-top: ${PREVIEW_TOP}px; box-sizing: border-box; }
  .graph { position: relative; display: flex; flex-direction: column; gap: 0; border-radius: var(--feature-border-radius, 12px); }
  .graph svg { display: block; }
  .graph svg text { font-family: var(--ha-font-family-body, inherit); font-size: 10px; fill: var(--primary-text-color); font-variant-numeric: tabular-nums; paint-order: stroke; stroke: var(--card-background-color, var(--ha-card-background, var(--primary-background-color))); stroke-width: 4px; stroke-linejoin: round; }
  .chips { position: relative; height: ${PREVIEW_CHIP_H}px; margin-bottom: 4px; }
  .chip { position: absolute; top: 0; box-sizing: border-box; height: ${PREVIEW_CHIP_H}px; padding: 0 6px; border-radius: 7px; background: color-mix(in srgb, var(--primary-text-color) 9%, transparent); color: var(--secondary-text-color); font-family: var(--ha-font-family-body, inherit); font-size: 10px; line-height: ${PREVIEW_CHIP_H}px; text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
  .phases { position: relative; height: ${PREVIEW_NAMES_H}px; }
  .phase { position: absolute; top: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; font-family: var(--ha-font-family-body, inherit); font-size: var(--ha-font-size-xs, 10px); line-height: ${PREVIEW_NAMES_H}px; color: var(--secondary-text-color); }
  .number { pointer-events: auto; position: relative; display: flex; align-items: center; justify-content: space-between; height: var(--feature-height, 42px); border-radius: var(--feature-border-radius, 12px); overflow: hidden; color: var(--primary-text-color); font-weight: var(--ha-font-weight-medium, 500); }
  .number::before { content: ""; position: absolute; inset: 0; background-color: var(--control-number-buttons-background-color, var(--disabled-color)); opacity: var(--control-number-buttons-background-opacity, 0.2); pointer-events: none; }
  .number.unavailable { color: var(--disabled-text-color, var(--disabled-color)); }
  .number.locked { opacity: 0.6; }
  .number.compact { width: ${PREVIEW_RATIO_W}px; height: ${PREVIEW_ROW_H}px; flex: none; }
  .value { position: relative; flex: 1; text-align: center; font-variant-numeric: tabular-nums; }
  .button { position: relative; width: 35px; height: 100%; padding: 0; border: none; background: none; color: inherit; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .button svg { width: 16px; height: 16px; fill: currentColor; }
  .button[disabled] { opacity: 0.4; cursor: not-allowed; }
  .button:focus-visible { outline: 2px solid var(--control-number-buttons-focus-color, var(--secondary-text-color)); outline-offset: -2px; border-radius: var(--feature-border-radius, 12px); }
  .row { display: flex; align-items: center; justify-content: flex-end; gap: 10px; height: ${PREVIEW_ROW_H}px; min-width: 0; }
  .graph + .row, .row + .row { margin-top: ${PREVIEW_BLOCK_GAP}px; }
  .row-label { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; font-family: var(--ha-font-family-body, inherit); font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
  .row.locked .row-label { color: var(--disabled-text-color, var(--disabled-color)); }
`;

// Pure helpers, for tests (tests/card_flow_test.mjs).
DifluidBrewCard.math = { observedOnly, monotone, smoothedFlow, displayProfile, parseFlowSmoothing, despike, isotonic, trendFlow, throttleView, powerSteps, stepAt, hasPump, escapeHtml, profilePreview, isProfileSelect, layoutChips, profileName, BUILTIN_PROFILES };

if (!customElements.get("difluid-brew-card")) {
  customElements.define("difluid-brew-card", DifluidBrewCard);
}
if (!customElements.get("difluid-brew-card-editor")) {
  customElements.define("difluid-brew-card-editor", DifluidBrewCardEditor);
}
if (!customElements.get("difluid-profile-preview")) {
  customElements.define("difluid-profile-preview", DifluidProfilePreview);
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

// Tile feature: under the profile select's select-options feature.
window.customCardFeatures = window.customCardFeatures || [];
if (!window.customCardFeatures.some((f) => f.type === "difluid-profile-preview")) {
  const l = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
  window.customCardFeatures.push({
    type: "difluid-profile-preview",
    name: STRINGS[l].preview_name,
    configurable: false,
    isSupported: (hass, context) => isProfileSelect(hass, context),
  });
}

console.info(`%c DIFLUID-BREW-CARD %c ${CARD_VERSION} `, "font-weight:600", "");
