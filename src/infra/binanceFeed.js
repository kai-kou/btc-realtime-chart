// Binance market-data-only endpoints (no API key, not geo-restricted like api.binance.com).
// https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
import { ResilientSocket } from './resilientSocket.js';

const REST = 'https://data-api.binance.vision/api/v3/klines';
const WS = 'wss://data-stream.binance.vision/stream';

export function toCandle(row) {
  return { time: Math.floor(row[0] / 1000), open: +row[1], high: +row[2], low: +row[3], close: +row[4], volume: +row[5] };
}

// Parses a combined-stream message into { trade } or { candle } (or null).
export function parseMessage(raw) {
  const msg = JSON.parse(raw);
  const d = msg.data ?? msg;
  if (d.e === 'aggTrade') return { trade: { timeSec: d.T / 1000, price: +d.p, size: +d.q, exchangeTimeMs: d.T } };
  if (d.e === 'kline') {
    const k = d.k;
    return { candle: { time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v }, final: k.x, exchangeTimeMs: d.E };
  }
  return null;
}

export function createBinanceFeed({ symbol = 'BTCUSDT', fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
  const lower = symbol.toLowerCase();
  return {
    id: 'binance',
    label: 'Binance',
    pair: 'BTC/USDT',
    async loadHistory(interval, limit = 1000) {
      const res = await fetchImpl(`${REST}?symbol=${symbol}&interval=${interval}&limit=${limit}`);
      if (!res.ok) throw new Error(`Binance klines HTTP ${res.status}`);
      return (await res.json()).map(toCandle);
    },
    subscribe(interval, { onTrade, onCandle, onStatus }) {
      const url = `${WS}?streams=${lower}@aggTrade/${lower}@kline_${interval}`;
      const sock = new ResilientSocket(url, {
        onStatus,
        onMessage: (raw) => {
          const m = parseMessage(raw);
          // Volume comes only from the kline snapshot: adding aggTrade sizes on top of it would
          // double-count trades already included in the latest kline. Trades update price only.
          if (m?.trade) onTrade({ ...m.trade, size: 0 });
          else if (m?.candle) onCandle(m.candle, m.exchangeTimeMs);
        },
      });
      sock.start();
      return sock;
    },
  };
}
