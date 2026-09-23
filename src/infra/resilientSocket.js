// WebSocket wrapper that keeps a connection alive: exponential backoff with jitter,
// a no-message watchdog (silent half-open connections are common on mobile networks)
// and an explicit reconnect() for tab-resume / network-online events.
export class ResilientSocket {
  constructor(url, { onOpen, onMessage, onStatus, staleMs = 10000, WebSocketImpl = globalThis.WebSocket, now = () => Date.now() } = {}) {
    this.url = url;
    this.onOpen = onOpen ?? (() => {});
    this.onMessage = onMessage ?? (() => {});
    this.onStatus = onStatus ?? (() => {});
    this.staleMs = staleMs;
    this.WebSocketImpl = WebSocketImpl;
    this.now = now;
    this.attempt = 0;
    this.closed = false;
    this.ws = null;
    this.lastMessageAt = 0;
    this.retryTimer = null;
    this.watchdog = null;
  }

  start() {
    this.closed = false;
    this.#connect();
    // Checks every state, not only OPEN: a socket can hang in CONNECTING or CLOSING
    // (e.g. a close handshake that never completes behind a proxy) without firing onclose.
    this.watchdog = setInterval(() => {
      if (this.ws && !this.retryTimerPending && this.now() - this.lastMessageAt > this.staleMs) this.reconnect('stale');
    }, 1000);
  }

  reconnect(reason = 'manual') {
    if (this.closed) return;
    this.onStatus({ state: 'reconnecting', reason });
    this.#teardown();
    this.attempt = 0;
    this.#connect();
  }

  stop() {
    this.closed = true;
    clearInterval(this.watchdog);
    this.#teardown();
  }

  // Exposed for tests.
  static backoffMs(attempt, random = Math.random) {
    const base = Math.min(30000, 1000 * 2 ** attempt);
    return Math.round(base / 2 + random() * (base / 2));
  }

  #connect() {
    clearTimeout(this.retryTimer);
    this.retryTimerPending = false;
    this.lastMessageAt = this.now(); // connection attempt counts as activity for the watchdog
    this.onStatus({ state: 'connecting' });
    const ws = new this.WebSocketImpl(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.lastMessageAt = this.now();
      this.onStatus({ state: 'open' });
      this.onOpen(ws);
    };
    ws.onmessage = (ev) => {
      this.lastMessageAt = this.now();
      this.onMessage(ev.data);
    };
    ws.onerror = () => {}; // onclose follows and handles the retry
    ws.onclose = () => {
      if (this.ws !== ws || this.closed) return;
      const delay = ResilientSocket.backoffMs(this.attempt++);
      this.onStatus({ state: 'reconnecting', reason: 'closed', retryInMs: delay, attempt: this.attempt });
      this.retryTimerPending = true;
      this.retryTimer = setTimeout(() => this.#connect(), delay);
    };
  }

  #teardown() {
    clearTimeout(this.retryTimer);
    this.retryTimerPending = false;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      try { ws.close(); } catch { /* already closed */ }
    }
  }
}
