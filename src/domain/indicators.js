// Technical indicators over ascending close prices. Missing values are null.

export function bollinger(closes, period = 20, mult = 2) {
  const out = closes.map(() => null);
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    sumSq += closes[i] * closes[i];
    if (i >= period) {
      sum -= closes[i - period];
      sumSq -= closes[i - period] * closes[i - period];
    }
    if (i >= period - 1) {
      const mean = sum / period;
      const sd = Math.sqrt(Math.max(0, sumSq / period - mean * mean)); // population sd (Bollinger's definition)
      out[i] = { middle: mean, upper: mean + mult * sd, lower: mean - mult * sd };
    }
  }
  return out;
}

// Wilder's RSI.
export function rsi(closes, period = 14) {
  const out = closes.map(() => null);
  if (closes.length <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  out[period] = toRsi(avgGain, avgLoss);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = toRsi(avgGain, avgLoss);
  }
  return out;
}

function toRsi(avgGain, avgLoss) {
  if (avgLoss === 0) return avgGain === 0 ? 50 : 100;
  return 100 - 100 / (1 + avgGain / avgLoss);
}

// %B: position of price inside the band (0 = lower, 1 = upper).
export function percentB(close, band) {
  if (!band || band.upper === band.lower) return null;
  return (close - band.lower) / (band.upper - band.lower);
}
