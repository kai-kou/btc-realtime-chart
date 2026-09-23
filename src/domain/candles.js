// Pure candle-series operations. A candle is { time, open, high, low, close, volume }
// where `time` is the bucket start in UTC seconds.
import { bucketStart } from './intervals.js';

// Applies a single trade to the series. Returns { candles, closed } where `closed`
// is the candle that was finalised because the trade opened a new bucket (or null).
export function applyTrade(candles, trade, seconds) {
  const t = bucketStart(trade.timeSec, seconds);
  const last = candles[candles.length - 1];
  if (last && t < last.time) return { candles, closed: null }; // late trade: ignore
  if (last && t === last.time) {
    const updated = {
      ...last,
      high: Math.max(last.high, trade.price),
      low: Math.min(last.low, trade.price),
      close: trade.price,
      volume: last.volume + trade.size,
    };
    return { candles: [...candles.slice(0, -1), updated], closed: null };
  }
  const opened = { time: t, open: trade.price, high: trade.price, low: trade.price, close: trade.price, volume: trade.size };
  return { candles: [...candles, opened], closed: last ?? null };
}

// Replaces or appends an authoritative candle (e.g. an exchange kline update).
export function upsertCandle(candles, candle) {
  const last = candles[candles.length - 1];
  if (!last || candle.time > last.time) return { candles: [...candles, candle], closed: last ?? null };
  if (candle.time === last.time) return { candles: [...candles.slice(0, -1), candle], closed: null };
  const idx = candles.findIndex((c) => c.time === candle.time);
  if (idx === -1) return { candles, closed: null };
  const copy = candles.slice();
  copy[idx] = candle;
  return { candles: copy, closed: null };
}

// Aggregates ascending candles into a coarser bucket size (e.g. 1h -> 4h).
export function aggregate(candles, seconds) {
  const out = [];
  for (const c of candles) {
    const t = bucketStart(c.time, seconds);
    const last = out[out.length - 1];
    if (last && last.time === t) {
      last.high = Math.max(last.high, c.high);
      last.low = Math.min(last.low, c.low);
      last.close = c.close;
      last.volume += c.volume;
    } else {
      out.push({ ...c, time: t });
    }
  }
  return out;
}

export function trimTo(candles, max) {
  return candles.length > max ? candles.slice(candles.length - max) : candles;
}
