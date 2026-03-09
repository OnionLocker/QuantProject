/**
 * BacktestChart.jsx
 * lightweight-charts K 线图，带入场/平仓标记 + SL/TP 水平线
 */
import { useEffect, useRef, useCallback } from 'react'
import { createChart, CrosshairMode, LineStyle } from 'lightweight-charts'

export default function BacktestChart({ candles = [], trades = [] }) {
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const seriesRef    = useRef(null)
  const slLinesRef   = useRef([])  // 当前显示的 SL/TP price lines

  // ── 初始化图表 ────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: '#0f1117' },
        textColor:  '#9ca3af',
      },
      grid: {
        vertLines:  { color: 'rgba(255,255,255,0.04)' },
        horzLines:  { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: 'rgba(255,255,255,0.2)', width: 1, style: LineStyle.Dashed },
        horzLine: { color: 'rgba(255,255,255,0.2)', width: 1, style: LineStyle.Dashed },
      },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.1)',
        scaleMargins: { top: 0.08, bottom: 0.08 },
      },
      timeScale: {
        borderColor:       'rgba(255,255,255,0.1)',
        timeVisible:       true,
        secondsVisible:    false,
        rightOffset:       8,
        barSpacing:        6,
        fixLeftEdge:       true,
        fixRightEdge:      true,
      },
      handleScroll:  true,
      handleScale:   true,
    })

    const series = chart.addCandlestickSeries({
      upColor:          '#26a69a',
      downColor:        '#ef5350',
      borderUpColor:    '#26a69a',
      borderDownColor:  '#ef5350',
      wickUpColor:      '#26a69a',
      wickDownColor:    '#ef5350',
    })

    chartRef.current  = chart
    seriesRef.current = series

    // 响应容器尺寸变化
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect
      chart.applyOptions({ width, height })
    })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current  = null
      seriesRef.current = null
    }
  }, [])

  // ── 载入 K 线数据 ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !candles.length) return
    seriesRef.current.setData(candles)
    chartRef.current?.timeScale().fitContent()
  }, [candles])

  // ── 绘制入场/平仓标记 + SL/TP 价格线 ─────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !chartRef.current) return

    // 清除旧的 SL/TP 价格线
    slLinesRef.current.forEach(l => {
      try { seriesRef.current?.removePriceLine(l) } catch {}
    })
    slLinesRef.current = []

    if (!trades.length) {
      seriesRef.current.setMarkers([])
      return
    }

    const markers = []

    trades.forEach((t, idx) => {
      const isLong = t.side === 'long'
      const pnlStr = t.pnl != null
        ? `  PnL: ${t.pnl >= 0 ? '+' : ''}${t.pnl} U`
        : ''

      // ── 入场标记 ──
      if (t.entry_ts) {
        const entryTime = Math.floor(new Date(t.entry_ts).getTime() / 1000)
        markers.push({
          time:     entryTime,
          position: isLong ? 'belowBar' : 'aboveBar',
          color:    isLong ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowUp' : 'arrowDown',
          text:     `#${idx + 1} ${isLong ? '多' : '空'} @${t.entry_price}`,
          size:     1.5,
        })
      }

      // ── SL 价格线（用 PriceLine，整个持仓期间显示）──
      if (t.sl && t.entry_ts) {
        const slLine = seriesRef.current.createPriceLine({
          price:      t.sl,
          color:      'rgba(239,83,80,0.7)',
          lineWidth:  1,
          lineStyle:  LineStyle.Dashed,
          axisLabelVisible: false,
          title:      `#${idx + 1} SL`,
        })
        slLinesRef.current.push(slLine)
      }

      // ── TP 价格线 ──
      if (t.tp && t.entry_ts) {
        const tpLine = seriesRef.current.createPriceLine({
          price:      t.tp,
          color:      'rgba(38,166,154,0.7)',
          lineWidth:  1,
          lineStyle:  LineStyle.Dashed,
          axisLabelVisible: false,
          title:      `#${idx + 1} TP`,
        })
        slLinesRef.current.push(tpLine)
      }

      // ── 平仓标记 ──
      if (t.exit_ts && t.exit_price) {
        const exitTime = Math.floor(new Date(t.exit_ts).getTime() / 1000)
        const isWin    = t.result === 'win'
        const reasonIcon = t.exit_reason === '止盈' ? '🎉'
                         : t.exit_reason === '止损' ? '🩸'
                         : '↩'
        markers.push({
          time:     exitTime,
          position: isLong ? 'aboveBar' : 'belowBar',
          color:    isWin ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowDown' : 'arrowUp',
          text:     `${reasonIcon} @${t.exit_price}${pnlStr}`,
          size:     1.5,
        })
      }
    })

    // lightweight-charts 要求 markers 按时间升序
    markers.sort((a, b) => a.time - b.time)
    seriesRef.current.setMarkers(markers)
  }, [trades, candles])

  if (!candles.length) return null

  return (
    <div
      ref={containerRef}
      style={{
        width:        '100%',
        height:       480,
        borderRadius: 8,
        overflow:     'hidden',
        background:   '#0f1117',
      }}
    />
  )
}
