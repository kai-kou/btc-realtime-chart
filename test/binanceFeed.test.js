import test from 'node:test';
import assert from 'node:assert/strict';
import { parseMessage, toCandle } from '../src/infra/binanceFeed.js';

test('parses combined-stream aggTrade and kline messages', () => {
  const trade = parseMessage(JSON.stringify({ stream: 'btcusdt@aggTrade', data: { e: 'aggTrade', p: '87000.5', q: '0.01', T: 1790000000123 } }));
  assert.deepEqual(trade.trade, { timeSec: 1790000000.123, price: 87000.5, size: 0.01, exchangeTimeMs: 1790000000123 });
  const kline = parseMessage(JSON.stringify({ stream: 'btcusdt@kline_1m', data: { e: 'kline', E: 1790000001000, k: { t: 1789999980000, o: '1', h: '3', l: '0.5', c: '2', v: '10', x: false } } }));
  assert.deepEqual(kline.candle, { time: 1789999980, open: 1, high: 3, low: 0.5, close: 2, volume: 10 });
  assert.equal(parseMessage(JSON.stringify({ result: null, id: 1 })), null);
});

test('converts REST kline rows', () => {
  assert.deepEqual(toCandle([1789999980000, '1', '3', '0.5', '2', '10']), { time: 1789999980, open: 1, high: 3, low: 0.5, close: 2, volume: 10 });
});
