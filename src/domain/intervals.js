// Candle interval definitions shared by every layer.
export const INTERVALS = {
  '1m': { label: '1分', seconds: 60 },
  '5m': { label: '5分', seconds: 300 },
  '15m': { label: '15分', seconds: 900 },
  '1h': { label: '1時間', seconds: 3600 },
  '4h': { label: '4時間', seconds: 14400 },
  '1d': { label: '1日', seconds: 86400 },
};

export function intervalSeconds(interval) {
  const def = INTERVALS[interval];
  if (!def) throw new Error(`unknown interval: ${interval}`);
  return def.seconds;
}

// Start (UTC seconds) of the bucket that contains `timeSec`.
export function bucketStart(timeSec, seconds) {
  return Math.floor(timeSec / seconds) * seconds;
}
