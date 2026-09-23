import test from 'node:test';
import assert from 'node:assert/strict';
import { bucketStart, intervalSeconds } from '../src/domain/intervals.js';
import { applyTrade, upsertCandle, aggregate, trimTo } from '../src/domain/candles.js';
import { bollinger, rsi, percentB } from '../src/domain/indicators.js';
import { detectSignals, computeIndicators, currentBias, DEFAULT_PARAMS } from '../src/domain/signals.js';

const c = (time, close, extra = {}) => ({ time, open: close, high: close, low: close, close, volume: 1, ...extra });

test('bucketStart floors to the interval boundary', () => {
  assert.equal(bucketStart(125, 60), 120);
  assert.equal(bucketStart(120, 60), 120);
  assert.equal(intervalSeconds('4h'), 14400);
  assert.throws(() => intervalSeconds('2m'));
});

test('applyTrade updates the forming candle and opens a new one on the next bucket', () => {
  let r = applyTrade([], { timeSec: 61, price: 100, size: 1 }, 60);
  assert.deepEqual(r.candles, [{ time: 60, open: 100, high: 100, low: 100, close: 100, volume: 1 }]);
  r = applyTrade(r.candles, { timeSec: 90, price: 105, size: 2 }, 60);
  r = applyTrade(r.candles, { timeSec: 100, price: 98, size: 1 }, 60);
  assert.deepEqual(r.candles[0], { time: 60, open: 100, high: 105, low: 98, close: 98, volume: 4 });
  assert.equal(r.closed, null);
  r = applyTrade(r.candles, { timeSec: 121, price: 99, size: 1 }, 60);
  assert.equal(r.candles.length, 2);
  assert.equal(r.closed.time, 60);
  const late = applyTrade(r.candles, { timeSec: 70, price: 1, size: 1 }, 60);
  assert.equal(late.candles, r.candles, 'late trades are ignored');
});

test('upsertCandle replaces the same bucket and appends newer ones', () => {
  const base = [c(60, 1), c(120, 2)];
  assert.equal(upsertCandle(base, c(120, 3)).candles[1].close, 3);
  const appended = upsertCandle(base, c(180, 4));
  assert.equal(appended.candles.length, 3);
  assert.equal(appended.closed.time, 120);
  assert.equal(upsertCandle(base, c(60, 9)).candles[0].close, 9);
});

test('aggregate merges 1h candles into 4h candles', () => {
  const hourly = [0, 1, 2, 3, 4].map((h) => ({ time: h * 3600, open: h, high: h + 10, low: h - 1, close: h + 1, volume: 1 }));
  const four = aggregate(hourly, 14400);
  assert.equal(four.length, 2);
  assert.deepEqual(four[0], { time: 0, open: 0, high: 13, low: -1, close: 4, volume: 4 });
  assert.equal(trimTo([1, 2, 3], 2).join(), '2,3');
});

test('bollinger matches a hand-computed population standard deviation', () => {
  const b = bollinger([2, 4, 4, 4, 5, 5, 7, 9], 8, 2);
  assert.equal(b[6], null);
  assert.equal(b[7].middle, 5);
  assert.equal(b[7].upper, 9); // sd = 2
  assert.equal(b[7].lower, 1);
  assert.equal(percentB(5, b[7]), 0.5);
});

test('rsi is 100 for a monotonic rise and 0 for a monotonic fall', () => {
  const up = Array.from({ length: 20 }, (_, i) => i);
  assert.equal(rsi(up, 14)[13], null);
  assert.equal(rsi(up, 14)[19], 100);
  assert.equal(rsi(up.slice().reverse(), 14)[19], 0);
  assert.equal(rsi(Array(20).fill(5), 14)[19], 50);
});

// Flat market, then a sharp drop below the lower band followed by a rebound back inside it.
function reboundSeries() {
  const closes = [...Array(25).fill(100), 99, 98, 97, 90, 95];
  return closes.map((v, i) => c(i * 60, v));
}

test('detectSignals emits BUY when price re-enters the lower band while oversold', () => {
  const candles = [...reboundSeries(), c(30 * 60, 95)]; // last one is the forming candle
  const ind = computeIndicators(candles);
  const signals = detectSignals(candles, ind);
  assert.equal(signals.length, 1);
  assert.equal(signals[0].side, 'buy');
  assert.equal(signals[0].time, 29 * 60);
});

test('detectSignals ignores the forming candle so signals never repaint', () => {
  const candles = reboundSeries(); // the rebound bar is now the forming (last) candle
  const signals = detectSignals(candles, computeIndicators(candles));
  assert.equal(signals.length, 0);
});

test('detectSignals emits SELL on the mirrored pattern', () => {
  const closes = [...Array(25).fill(100), 101, 102, 103, 110, 105, 105];
  const candles = closes.map((v, i) => c(i * 60, v));
  const signals = detectSignals(candles, computeIndicators(candles));
  assert.deepEqual(signals.map((s) => s.side), ['sell']);
});

test('detectSignals respects the RSI filter', () => {
  const candles = [...reboundSeries(), c(30 * 60, 95)];
  const strict = { ...DEFAULT_PARAMS, rsiBuy: -1 }; // RSI reaches 0 in this series, so only a negative threshold rejects it
  assert.equal(detectSignals(candles, computeIndicators(candles, strict), strict).length, 0);
});

test('currentBias describes the forming candle', () => {
  const band = { lower: 90, middle: 100, upper: 110 };
  assert.equal(currentBias(85, band, 20).level, 'buy-watch');
  assert.equal(currentBias(115, band, 80).level, 'sell-watch');
  assert.equal(currentBias(100, band, 50).level, 'neutral');
  assert.equal(currentBias(100, null, null).text, 'データ蓄積中');
});

test('countNewer counts bars the chart has not drawn yet', async () => {
  const { countNewer } = await import('../src/domain/candles.js');
  const cs = [c(60, 1), c(120, 1), c(180, 1)];
  assert.equal(countNewer(cs, 180), 0);
  assert.equal(countNewer(cs, 120), 1);
  assert.equal(countNewer(cs, 60), 2);
  assert.equal(countNewer(cs, -Infinity), 3);
});
