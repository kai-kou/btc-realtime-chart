// Coinbase Exchange public feed, used as the failover source.
// https://docs.cdp.coinbase.com/exchange/websocket-feed/overview
import { ResilientSocket } from './resilientSocket.js';
import { aggregate } from '../domain/candles.js';
import { intervalSeconds } from '../domain/intervals.js';

const REST = 'https://api.exchange.coinbase.com/products';
const WS = 'wss://ws-feed.exchange.coinbase.com';
// Supported granularities: 60, 300, 900, 3600, 21600, 86400 (no 4h) -> build 4h from 1h.
const SOURCE_GRANULARITY = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 3600, '1d': 86400 };

export function toCandle(row) {
  // [time, low, high, open, close, volume]
  return { time: row[0], open: row[3], high: row[2], low: row[1], close: row[4], volume: row[5] };
}

export function parseMessage(raw) {
  const m = JSON.parse(raw);
  if (m.type === 'match' || m.type === 'last_match') {
    const ms = Date.parse(m.time);
    return { trade: { timeSec: ms / 1000, price: +m.price, size: +m.size, exchangeTimeMs: ms } };
  }
  return null;
}

export function createCoinbaseFeed({ product = 'BTC-USD', fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
  return {
    id: 'coinbase',
    label: 'Coinbase',
    pair: 'BTC/USD',
    async loadHistory(interval) {
      const g = SOURCE_GRANULARITY[interval];
      const res = await fetchImpl(`${REST}/${product}/candles?granularity=${g}`);
      if (!res.ok) throw new Error(`Coinbase candles HTTP ${res.status}`);
      const rows = (await res.json()).map(toCandle).sort((a, b) => a.time - b.time);
      const target = intervalSeconds(interval);
      return target === g ? rows : aggregate(rows, target);
    },
    subscribe(_interval, { onTrade, onStatus }) {
      const sock = new ResilientSocket(WS, {
        onStatus,
        onOpen: (ws) => ws.send(JSON.stringify({ type: 'subscribe', product_ids: [product], channels: ['matches', 'heartbeat'] })),
        onMessage: (raw) => {
          const m = parseMessage(raw);
          if (m?.trade) onTrade(m.trade);
        },
      });
      sock.start();
      return sock;
    },
  };
}
