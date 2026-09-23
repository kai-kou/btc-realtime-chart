import test from 'node:test';
import assert from 'node:assert/strict';
import { FeedController } from '../src/app/feedController.js';

const candle = (time, close) => ({ time, open: close, high: close, low: close, close, volume: 1 });

function fakeFeed(id, { history = [candle(0, 1), candle(60, 2)], fail = false } = {}) {
  const feed = {
    id, label: id, pair: 'BTC/X', subs: [],
    async loadHistory() { if (fail) throw new Error('down'); return history; },
    subscribe(_interval, handlers) { const sub = { handlers, stopped: false, stop() { this.stopped = true; } }; feed.subs.push(sub); return sub; },
  };
  return feed;
}

function harness(feeds) {
  const events = { resets: [], updates: [], statuses: [] };
  const ctl = new FeedController(feeds, {
    onReset: (cs) => events.resets.push(cs),
    onUpdate: (cs, meta) => events.updates.push({ cs, meta }),
    onStatus: (s) => events.statuses.push(s),
  });
  return { ctl, events };
}

test('loads history, then applies streamed trades and reports closed candles', async () => {
  const a = fakeFeed('a');
  const { ctl, events } = harness([a]);
  await ctl.start('1m');
  assert.equal(events.resets.length, 1);
  a.subs[0].handlers.onTrade({ timeSec: 70, price: 3, size: 1, exchangeTimeMs: Date.now() });
  assert.equal(ctl.candles.at(-1).close, 3);
  a.subs[0].handlers.onTrade({ timeSec: 125, price: 4, size: 1 });
  assert.equal(events.updates.at(-1).meta.closed.time, 60);
  assert.equal(events.statuses.at(-1).state, 'live');
});

test('fails over to the next feed when history cannot be loaded', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const a = fakeFeed('a', { fail: true });
  const b = fakeFeed('b');
  const { ctl, events } = harness([a, b]);
  await ctl.start('1m');
  assert.equal(events.statuses.at(-1).state, 'error');
  t.mock.timers.tick(500);
  await new Promise((r) => setImmediate(r));
  assert.equal(ctl.feed.id, 'b');
  assert.equal(b.subs.length, 1);
});

test('fails over after repeated websocket closes', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const a = fakeFeed('a');
  const b = fakeFeed('b');
  const { ctl } = harness([a, b]);
  await ctl.start('1m');
  for (let i = 0; i < 3; i++) a.subs[0].handlers.onStatus({ state: 'reconnecting', reason: 'closed' });
  t.mock.timers.tick(500);
  await new Promise((r) => setImmediate(r));
  assert.equal(ctl.feed.id, 'b');
  assert.ok(a.subs[0].stopped, 'old subscription is stopped');
});

test('ignores messages from a subscription that was replaced', async () => {
  const a = fakeFeed('a');
  const { ctl } = harness([a]);
  await ctl.start('1m');
  const old = a.subs[0];
  await ctl.start('5m');
  old.handlers.onTrade({ timeSec: 70, price: 999, size: 1 });
  assert.notEqual(ctl.candles.at(-1).close, 999);
});
