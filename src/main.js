// Composition root: wires feeds, the controller use case and the views together.
import { createBinanceFeed } from './infra/binanceFeed.js';
import { createCoinbaseFeed } from './infra/coinbaseFeed.js';
import { FeedController } from './app/feedController.js';
import { computeIndicators, detectSignals } from './domain/signals.js';
import { INTERVALS } from './domain/intervals.js';
import { ChartView } from './ui/chartView.js';
import { PanelView } from './ui/panelView.js';

const $ = (id) => document.getElementById(id);
const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const latestBtn = $('latest');
const chart = new ChartView($('chart'), { onFollowChange: (following) => { latestBtn.hidden = following; } });
const panel = new PanelView();
const feeds = [createBinanceFeed(), createCoinbaseFeed()];

let candles = [];
let ind = { bands: [], rsi: [] };
let signals = [];
let pendingFrame = false;
let pendingSignals = false;

function recompute() {
  ind = computeIndicators(candles);
}

function refreshSignals() {
  signals = detectSignals(candles, ind);
  chart.setSignals(signals);
  panel.renderSignals(signals);
}

const controller = new FeedController(feeds, {
  onReset: (cs) => {
    candles = cs;
    recompute();
    signals = detectSignals(candles, ind);
    chart.setAll(candles, ind, signals);
    panel.renderPrice(candles, ind, controller.feed.pair);
    panel.renderSignals(signals);
  },
  onUpdate: (cs, { closed }) => {
    candles = cs;
    if (closed) pendingSignals = true;
    if (pendingFrame) return; // coalesce bursts of trades into one paint per frame
    pendingFrame = true;
    requestAnimationFrame(() => {
      pendingFrame = false;
      recompute();
      chart.updateLast(candles, ind);
      panel.renderPrice(candles, ind, controller.feed.pair);
      if (pendingSignals) {
        pendingSignals = false;
        refreshSignals();
      }
    });
  },
  onStatus: (s) => panel.setStatus(s),
  onFeedChange: (feed) => {
    panel.setFeed(feed);
    store.set('feed', feed.id);
  },
});

// Interval buttons
const intervalBar = $('intervals');
for (const [id, def] of Object.entries(INTERVALS)) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = def.label;
  b.dataset.interval = id;
  b.addEventListener('click', () => selectInterval(id));
  intervalBar.append(b);
}
function selectInterval(id) {
  for (const b of intervalBar.children) b.setAttribute('aria-pressed', String(b.dataset.interval === id));
  store.set('interval', id);
  controller.start(id);
}

$('feed').addEventListener('change', (e) => controller.start(controller.interval, e.target.value));
latestBtn.addEventListener('click', () => chart.jumpToLatest());

// Keep the chart current: resync after the tab was hidden (timers/sockets get throttled or
// dropped in background tabs), after the network returns, and after bfcache restores.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    controller.reconnectIfStale(3000);
    requestWakeLock();
  }
});
window.addEventListener('online', () => controller.resync());
window.addEventListener('pageshow', (e) => { if (e.persisted) controller.resync(); });
setInterval(() => panel.tick(Date.now()), 1000);

// Optional: keep the screen awake while monitoring.
let wakeLock = null;
const wakeToggle = $('wake');
async function requestWakeLock() {
  if (!wakeToggle.checked || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch { wakeToggle.checked = false; }
}
wakeToggle.disabled = !('wakeLock' in navigator);
wakeToggle.addEventListener('change', async () => {
  if (wakeToggle.checked) await requestWakeLock();
  else { await wakeLock?.release(); wakeLock = null; }
});

const initialFeed = store.get('feed', 'binance');
const initialInterval = INTERVALS[store.get('interval', '1m')] ? store.get('interval', '1m') : '1m';
for (const b of intervalBar.children) b.setAttribute('aria-pressed', String(b.dataset.interval === initialInterval));
controller.start(initialInterval, initialFeed);

// Test hook for the E2E check (read-only snapshot).
window.__btcChart = { get state() { return { candles: candles.length, signals: signals.length, feed: controller.feed.id, status: panel.status }; }, controller };
