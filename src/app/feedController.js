// Use case: keep an always-fresh candle series for the selected interval.
// Owns feed selection/failover, resync on reconnect/resume, and closed-candle detection.
import { applyTrade, upsertCandle, trimTo } from '../domain/candles.js';
import { intervalSeconds } from '../domain/intervals.js';

const MAX_CANDLES = 1500;
const FAILOVER_AFTER_FAILURES = 3;

export class FeedController {
  constructor(feeds, { onReset, onUpdate, onStatus, onFeedChange, clock = () => Date.now() }) {
    this.feeds = feeds;
    this.onReset = onReset; // (candles) full replace
    this.onUpdate = onUpdate; // (candles, { closed }) incremental
    this.onStatus = onStatus;
    this.onFeedChange = onFeedChange ?? (() => {});
    this.clock = clock;
    this.feedIndex = 0;
    this.interval = '1m';
    this.candles = [];
    this.sub = null;
    this.generation = 0;
    this.failures = 0;
    this.failovers = 0; // consecutive failovers without a single tick: drives the backoff
    this.lastTickAt = 0;
    this.lastLatencyMs = null;
  }

  get feed() {
    return this.feeds[this.feedIndex];
  }

  async start(interval = this.interval, feedId) {
    if (feedId) this.feedIndex = Math.max(0, this.feeds.findIndex((f) => f.id === feedId));
    this.interval = interval;
    const gen = ++this.generation;
    this.sub?.stop();
    this.sub = null;
    this.onFeedChange(this.feed);
    this.onStatus({ state: 'loading' });
    try {
      const history = await this.feed.loadHistory(interval);
      if (gen !== this.generation) return;
      this.candles = trimTo(history, MAX_CANDLES);
      this.onReset(this.candles);
    } catch (err) {
      if (gen !== this.generation) return;
      this.#fail(`history: ${err.message}`);
      return;
    }
    this.sub = this.feed.subscribe(interval, {
      onTrade: (t) => gen === this.generation && this.#onTrade(t),
      onCandle: (c, exchangeTimeMs) => gen === this.generation && this.#onCandle(c, exchangeTimeMs),
      onStatus: (s) => gen === this.generation && this.#onSocketStatus(s),
    });
  }

  // Refetch history (fills gaps after sleep / network loss) and restart the stream.
  resync() {
    return this.start(this.interval);
  }

  reconnectIfStale(maxAgeMs = 5000) {
    if (this.clock() - this.lastTickAt > maxAgeMs) this.resync();
  }

  stop() {
    this.generation++;
    this.sub?.stop();
  }

  #onTrade(trade) {
    this.#touch(trade.exchangeTimeMs);
    const { candles, closed } = applyTrade(this.candles, trade, intervalSeconds(this.interval));
    this.#commit(candles, closed);
  }

  #onCandle(candle, exchangeTimeMs) {
    this.#touch(exchangeTimeMs);
    const { candles, closed } = upsertCandle(this.candles, candle);
    this.#commit(candles, closed);
  }

  #commit(candles, closed) {
    if (candles === this.candles) return;
    this.candles = trimTo(candles, MAX_CANDLES);
    this.onUpdate(this.candles, { closed });
  }

  #touch(exchangeTimeMs) {
    this.failures = 0;
    this.failovers = 0;
    this.lastTickAt = this.clock();
    if (exchangeTimeMs) this.lastLatencyMs = Math.max(0, this.lastTickAt - exchangeTimeMs);
    this.onStatus({ state: 'live', lastTickAt: this.lastTickAt, latencyMs: this.lastLatencyMs });
  }

  #onSocketStatus(s) {
    if (s.state === 'open') {
      // A fresh connection may have missed trades: refetch once the stream is up again.
      if (this.hadConnection) this.#refreshHistory();
      this.hadConnection = true;
      this.onStatus({ state: 'connected' });
      return;
    }
    if (s.state === 'reconnecting' && s.reason === 'closed') {
      if (++this.failures >= FAILOVER_AFTER_FAILURES && this.feeds.length > 1) {
        this.#fail('websocket closed repeatedly');
        return;
      }
    }
    this.onStatus(s);
  }

  async #refreshHistory() {
    const gen = this.generation;
    try {
      const history = await this.feed.loadHistory(this.interval);
      if (gen !== this.generation) return;
      // Keep the forming candle from the stream if it is newer than the REST snapshot.
      const last = this.candles[this.candles.length - 1];
      const merged = last && history.length && last.time > history[history.length - 1].time ? [...history, last] : history;
      this.candles = trimTo(merged, MAX_CANDLES);
      this.onReset(this.candles);
    } catch {
      /* stream keeps running; next reconnect retries */
    }
  }

  #fail(reason) {
    this.onStatus({ state: 'error', reason, feed: this.feed.id });
    if (this.feeds.length < 2) return;
    this.failures = 0;
    this.hadConnection = false;
    this.feedIndex = (this.feedIndex + 1) % this.feeds.length;
    // Back off exponentially when every feed keeps failing, so an outage is not turned
    // into an IP ban by hammering the exchanges (Binance bans on repeated 429/418).
    const delay = Math.min(30000, 500 * 2 ** this.failovers++);
    this.onStatus({ state: 'error', reason, feed: this.feed.id, retryInMs: delay });
    setTimeout(() => this.start(this.interval), delay);
  }
}
