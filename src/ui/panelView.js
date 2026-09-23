// Price / indicator panel and connection badge. Pure DOM writes, no business logic.
import { percentB } from '../domain/indicators.js';
import { currentBias } from '../domain/signals.js';

const $ = (id) => document.getElementById(id);
const price = (v) => v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const jst = (ms) => new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(ms));

export class PanelView {
  constructor() {
    this.prevClose = null;
    this.status = { state: 'loading' };
  }

  renderPrice(candles, ind, pair) {
    const n = candles.length;
    if (!n) return;
    const last = candles[n - 1];
    const prev = candles[n - 2];
    const band = ind.bands[n - 1];
    const r = ind.rsi[n - 1];
    const priceEl = $('price');
    priceEl.textContent = price(last.close);
    if (this.prevClose != null && last.close !== this.prevClose) {
      priceEl.dataset.tick = last.close > this.prevClose ? 'up' : 'down';
    }
    this.prevClose = last.close;
    if (prev) {
      const diff = last.close - prev.close;
      const pct = (diff / prev.close) * 100;
      const el = $('change');
      el.textContent = `前足比 ${diff >= 0 ? "+" : ""}${price(diff)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
      el.dataset.dir = diff >= 0 ? 'up' : 'down';
    }
    const pb = percentB(last.close, band);
    $('pctb').textContent = pb == null ? '—' : pb.toFixed(2);
    $('rsi').textContent = r == null ? '—' : r.toFixed(1);
    $('bands').textContent = band ? `${price(band.lower)} / ${price(band.upper)}` : '—';
    const bias = currentBias(last.close, band, r);
    const biasEl = $('bias');
    biasEl.textContent = bias.text;
    biasEl.dataset.level = bias.level;
    document.title = `${price(last.close)} ${pair} | BTC Realtime Chart`;
  }

  renderSignals(signals) {
    const last = signals[signals.length - 1];
    const el = $('last-signal');
    if (!last) {
      el.textContent = '表示範囲内になし';
      el.dataset.side = '';
      return;
    }
    el.textContent = `${last.side === 'buy' ? '買い' : '売り'} @ ${price(last.price)}（${jst(last.time * 1000)}）`;
    el.dataset.side = last.side;
  }

  setStatus(s) {
    this.status = { ...this.status, ...s };
    this.#paintStatus();
  }

  // Called every second so the "last update" age keeps moving even without ticks.
  tick(now) {
    this.#paintStatus(now);
  }

  setFeed(feed) {
    $('feed-label').textContent = `${feed.label} ${feed.pair}`;
    $('feed').value = feed.id;
  }

  #paintStatus(now = Date.now()) {
    const s = this.status;
    const badge = $('status');
    let state = s.state;
    const age = s.lastTickAt ? now - s.lastTickAt : null;
    if (state === 'live' && age != null && age > 5000) state = 'stale';
    const labels = { loading: '読み込み中', connecting: '接続中', connected: '接続済み', live: 'LIVE', stale: '更新停滞', reconnecting: '再接続中', error: 'エラー' };
    badge.textContent = labels[state] ?? state;
    badge.dataset.state = state;
    $('age').textContent = age == null ? '' : `最終受信 ${(age / 1000).toFixed(age < 10000 ? 1 : 0)} 秒前`;
    $('latency').textContent = s.latencyMs == null ? '' : `遅延 ${Math.round(s.latencyMs)} ms`;
  }
}
