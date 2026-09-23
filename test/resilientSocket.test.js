import test from 'node:test';
import assert from 'node:assert/strict';
import { ResilientSocket } from '../src/infra/resilientSocket.js';

class FakeWS {
  static instances = [];
  constructor(url) { this.url = url; this.readyState = 0; this.closeCalls = 0; FakeWS.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  message(data) { this.onmessage?.({ data }); }
  close() { this.closeCalls++; this.readyState = 2; } // never completes: simulates a hung close handshake
  drop() { this.readyState = 3; this.onclose?.(); }
}

function setup(t, opts = {}) {
  FakeWS.instances = [];
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] });
  const statuses = [];
  const sock = new ResilientSocket('wss://example.test', { WebSocketImpl: FakeWS, onStatus: (s) => statuses.push(s.state), staleMs: 5000, ...opts });
  sock.start();
  return { sock, statuses };
}

test('backoff grows exponentially, is capped at 30s and jittered', () => {
  assert.equal(ResilientSocket.backoffMs(0, () => 0), 500);
  assert.equal(ResilientSocket.backoffMs(0, () => 1), 1000);
  assert.equal(ResilientSocket.backoffMs(3, () => 1), 8000);
  assert.equal(ResilientSocket.backoffMs(10, () => 1), 30000);
});

test('reconnects after the server drops the connection', (t) => {
  const { sock } = setup(t);
  FakeWS.instances[0].open();
  FakeWS.instances[0].drop();
  t.mock.timers.tick(1000);
  assert.equal(FakeWS.instances.length, 2);
  sock.stop();
});

test('watchdog replaces a socket stuck in CLOSING without onclose', (t) => {
  const { sock, statuses } = setup(t);
  const first = FakeWS.instances[0];
  first.open();
  first.close(); // hung close handshake: no onclose ever fires
  t.mock.timers.tick(6000);
  assert.equal(FakeWS.instances.length, 2, 'a new socket was opened');
  assert.ok(statuses.includes('reconnecting'));
  assert.equal(first.onmessage, null, 'old socket handlers are detached');
  sock.stop();
});

test('watchdog replaces an open but silent socket', (t) => {
  const { sock } = setup(t);
  FakeWS.instances[0].open();
  t.mock.timers.tick(3000);
  FakeWS.instances[0].message('{}');
  t.mock.timers.tick(4000);
  assert.equal(FakeWS.instances.length, 1, 'recent message keeps the socket');
  t.mock.timers.tick(2000);
  assert.equal(FakeWS.instances.length, 2);
  sock.stop();
});

test('watchdog does not fire while waiting for a scheduled retry', (t) => {
  t.mock.method(Math, 'random', () => 1); // deterministic backoff: 1000ms, 2000ms, ...
  const { sock } = setup(t, { staleMs: 1000 });
  FakeWS.instances[0].open();
  FakeWS.instances[0].drop();
  t.mock.timers.tick(1000);
  assert.equal(FakeWS.instances.length, 2);
  FakeWS.instances[1].drop(); // second failure -> 2000ms backoff, longer than staleMs
  t.mock.timers.tick(1999);
  assert.equal(FakeWS.instances.length, 2, 'watchdog must not open an extra socket during backoff');
  t.mock.timers.tick(1);
  assert.equal(FakeWS.instances.length, 3);
  sock.stop();
});
