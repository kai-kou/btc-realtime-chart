// Buy/sell signal rules (mean-reversion on Bollinger Bands confirmed by RSI).
//
// BUY : the previous bar closed below the lower band and the current bar closes back
//       inside it, while RSI was oversold on either bar (rebound from an extreme).
// SELL: mirror image on the upper band with overbought RSI.
// Signals are evaluated on closed bars only, so they never repaint.
import { bollinger, rsi } from './indicators.js';

export const DEFAULT_PARAMS = { bbPeriod: 20, bbMult: 2, rsiPeriod: 14, rsiBuy: 35, rsiSell: 65, cooldown: 5 };

export function computeIndicators(candles, params = DEFAULT_PARAMS) {
  const closes = candles.map((c) => c.close);
  return { bands: bollinger(closes, params.bbPeriod, params.bbMult), rsi: rsi(closes, params.rsiPeriod) };
}

// `closedCount` = number of leading candles that are final (usually length - 1).
export function detectSignals(candles, ind, params = DEFAULT_PARAMS, closedCount = candles.length - 1) {
  const signals = [];
  let lastBuy = -Infinity;
  let lastSell = -Infinity;
  for (let i = 1; i < closedCount; i++) {
    const prev = candles[i - 1];
    const cur = candles[i];
    const pb = ind.bands[i - 1];
    const cb = ind.bands[i];
    const r0 = ind.rsi[i - 1];
    const r1 = ind.rsi[i];
    if (!pb || !cb || r0 == null || r1 == null) continue;
    if (prev.close < pb.lower && cur.close >= cb.lower && Math.min(r0, r1) <= params.rsiBuy && i - lastBuy > params.cooldown) {
      signals.push({ time: cur.time, side: 'buy', price: cur.close, rsi: r1 });
      lastBuy = i;
    } else if (prev.close > pb.upper && cur.close <= cb.upper && Math.max(r0, r1) >= params.rsiSell && i - lastSell > params.cooldown) {
      signals.push({ time: cur.time, side: 'sell', price: cur.close, rsi: r1 });
      lastSell = i;
    }
  }
  return signals;
}

// Human-readable state of the forming bar (not a signal; it may still change).
export function currentBias(close, band, rsiValue, params = DEFAULT_PARAMS) {
  if (!band || rsiValue == null) return { level: 'neutral', text: 'データ蓄積中' };
  if (close < band.lower && rsiValue <= params.rsiBuy) return { level: 'buy-watch', text: '下限バンド割れ・売られすぎ（反発待ち）' };
  if (close > band.upper && rsiValue >= params.rsiSell) return { level: 'sell-watch', text: '上限バンド超え・買われすぎ（反落待ち）' };
  if (close <= band.lower) return { level: 'buy-watch', text: '下限バンド付近' };
  if (close >= band.upper) return { level: 'sell-watch', text: '上限バンド付近' };
  return { level: 'neutral', text: 'バンド内（様子見）' };
}
