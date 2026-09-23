// Renders candles, Bollinger Bands, volume, RSI and signal markers with Lightweight Charts v5,
// and keeps the newest candle in view unless the user deliberately scrolls back.
import {
  createChart, createSeriesMarkers, CandlestickSeries, LineSeries, HistogramSeries,
  CrosshairMode, LineStyle, TickMarkType,
} from 'lightweight-charts';

const COLORS = {
  up: '#16c784', down: '#ea3943', band: '#5b8def', mid: '#e0a526', rsi: '#b18cff',
  grid: 'rgba(148,163,184,0.10)', text: '#9aa4b2',
};

const fmtTime = (sec, opts) => new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', hour12: false, ...opts }).format(new Date(sec * 1000));

export class ChartView {
  constructor(container, { onFollowChange } = {}) {
    this.chart = createChart(container, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: COLORS.text, fontFamily: 'inherit', attributionLogo: true, panes: { separatorColor: 'rgba(148,163,184,0.2)' } },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 6, shiftVisibleRangeOnNewBar: true, tickMarkFormatter: tickFormatter },
      localization: { locale: 'ja-JP', timeFormatter: (t) => fmtTime(t, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }), priceFormatter: (p) => p.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 }) },
    });
    this.candles = this.chart.addSeries(CandlestickSeries, { upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down });
    const bandOpts = { color: COLORS.band, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    this.upper = this.chart.addSeries(LineSeries, bandOpts);
    this.lower = this.chart.addSeries(LineSeries, bandOpts);
    this.middle = this.chart.addSeries(LineSeries, { ...bandOpts, color: COLORS.mid, lineStyle: LineStyle.Dashed });
    this.volume = this.chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
    this.chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    this.rsi = this.chart.addSeries(LineSeries, { color: COLORS.rsi, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true }, 1);
    this.rsi.createPriceLine({ price: 70, color: COLORS.down, lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false });
    this.rsi.createPriceLine({ price: 30, color: COLORS.up, lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false });
    this.chart.panes()[1]?.setHeight(110);
    this.markers = createSeriesMarkers(this.candles, []);

    this.following = true;
    this.onFollowChange = onFollowChange ?? (() => {});
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      const following = this.chart.timeScale().scrollPosition() > -1; // within ~1 bar of the right edge
      if (following !== this.following) {
        this.following = following;
        this.onFollowChange(following);
      }
    });
  }

  // Full redraw (history load, interval/feed change, gap refill).
  setAll(candles, ind, signals, { jump = true } = {}) {
    this.candles.setData(candles.map(toBar));
    this.volume.setData(candles.map(toVolume));
    this.#setIndicators(candles, ind);
    this.setSignals(signals);
    if (jump || this.following) this.jumpToLatest();
  }

  // Incremental update of the forming candle (called at most once per animation frame).
  updateLast(candles, ind) {
    const i = candles.length - 1;
    const c = candles[i];
    this.candles.update(toBar(c));
    this.volume.update(toVolume(c));
    const b = ind.bands[i];
    if (b) {
      this.upper.update({ time: c.time, value: b.upper });
      this.lower.update({ time: c.time, value: b.lower });
      this.middle.update({ time: c.time, value: b.middle });
    }
    if (ind.rsi[i] != null) this.rsi.update({ time: c.time, value: ind.rsi[i] });
    if (this.following) this.chart.timeScale().scrollToRealTime();
  }

  setSignals(signals) {
    this.markers.setMarkers(signals.map((s) => s.side === 'buy'
      ? { time: s.time, position: 'belowBar', color: COLORS.up, shape: 'arrowUp', text: 'BUY' }
      : { time: s.time, position: 'aboveBar', color: COLORS.down, shape: 'arrowDown', text: 'SELL' }));
  }

  jumpToLatest() {
    this.chart.timeScale().scrollToRealTime();
  }

  #setIndicators(candles, ind) {
    const line = (key) => candles.flatMap((c, i) => (ind.bands[i] ? [{ time: c.time, value: ind.bands[i][key] }] : []));
    this.upper.setData(line('upper'));
    this.lower.setData(line('lower'));
    this.middle.setData(line('middle'));
    this.rsi.setData(candles.flatMap((c, i) => (ind.rsi[i] != null ? [{ time: c.time, value: ind.rsi[i] }] : [])));
  }
}

function toBar(c) {
  return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
}

function toVolume(c) {
  return { time: c.time, value: c.volume, color: c.close >= c.open ? 'rgba(22,199,132,0.35)' : 'rgba(234,57,67,0.35)' };
}

function tickFormatter(time, type) {
  switch (type) {
    case TickMarkType.Year: return fmtTime(time, { year: 'numeric' });
    case TickMarkType.Month: return fmtTime(time, { month: 'short' });
    case TickMarkType.DayOfMonth: return fmtTime(time, { month: 'numeric', day: 'numeric' });
    default: return fmtTime(time, { hour: '2-digit', minute: '2-digit' });
  }
}
