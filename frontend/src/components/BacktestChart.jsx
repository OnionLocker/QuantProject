/**
 * BacktestChart.jsx — lightweight-charts v5 API
 * K 线图 + 入场/平仓标记 + SL/TP 价格线
 */
import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, LineSeries } from 'lightweight-charts'

export default function BacktestChart({ candles = [], trades = [] }) {
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const seriesRef    = useRef(null)
  const priceLines   = useRef([])

  // ── 初始化 ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: '#0f1117' },
        textColor:  '#9ca3af',
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: { mode: 1 },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.1)',
        scaleMargins: { top: 0.08, bottom: 0.08 },
      },
      timeScale: {
        borderColor:    'rgba(255,255,255,0.1)',
        timeVisible:    true,
        secondsVisible: false,
        rightOffset:    8,
        fixLeftEdge:    true,
        fixRightEdge:   true,
      },
    })

    // v5: addSeries(CandlestickSeries, options)
    const series = chart.addSeries(CandlestickSeries, {
      upColor:         '#26a69a',
      downColor:       '#ef5350',
      borderUpColor:   '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor:     '#26a69a',
      wickDownColor:   '#ef5350',
    })

    chartRef.current  = chart
    seriesRef.current = series

    return () => {
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

  // ── 标记 + SL/TP 价格线 ───────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current) return

    // 清除旧价格线
    priceLines.current.forEach(l => {
      try { seriesRef.current.removePriceLine(l) } catch {}
    })
    priceLines.current = []

    if (!trades.length) {
      seriesRef.current.setMarkers([])
      return
    }

    const markers = []

    trades.forEach((t, idx) => {
      const isLong = t.side === 'long'
      const pnlStr = t.pnl != null
        ? ` | ${t.pnl >= 0 ? '+' : ''}${t.pnl}U`
        : ''

      // 入场标记
      if (t.entry_ts) {
        const ts = Math.floor(new Date(t.entry_ts).getTime() / 1000)
        markers.push({
          time:     ts,
          position: isLong ? 'belowBar' : 'aboveBar',
          color:    isLong ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowUp' : 'arrowDown',
          text:     `#${idx + 1} ${isLong ? '开多' : '开空'} @${t.entry_price}`,
        })
      }

      // SL 价格线（红色虚线）
      if (t.sl) {
        const line = seriesRef.current.createPriceLine({
          price:            t.sl,
          color:            'rgba(239,83,80,0.6)',
          lineWidth:        1,
          lineStyle:        2,  // Dashed
          axisLabelVisible: false,
          title:            `SL#${idx + 1}`,
        })
        priceLines.current.push(line)
      }

      // TP 价格线（绿色虚线）
      if (t.tp) {
        const line = seriesRef.current.createPriceLine({
          price:            t.tp,
          color:            'rgba(38,166,154,0.6)',
          lineWidth:        1,
          lineStyle:        2,  // Dashed
          axisLabelVisible: false,
          title:            `TP#${idx + 1}`,
        })
        priceLines.current.push(line)
      }

      // 平仓标记
      if (t.exit_ts && t.exit_price) {
        const ts = Math.floor(new Date(t.exit_ts).getTime() / 1000)
        const isWin = t.result === 'win'
        const icon  = t.exit_reason === '止盈' ? '🎉'
                    : t.exit_reason === '止损' ? '🩸' : '↩'
        markers.push({
          time:     ts,
          position: isLong ? 'aboveBar' : 'belowBar',
          color:    isWin ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowDown' : 'arrowUp',
          text:     `${icon} @${t.exit_price}${pnlStr}`,
        })
      }
    })

    // v5 要求 markers 按时间升序
    markers.sort((a, b) => a.time - b.time)
    seriesRef.current.setMarkers(markers)
  }, [trades, candles])

  if (!candles.length) return null

  return (
    <div
      ref={containerRef}
      style={{
        width:      '100%',
        height:     480,
        borderRadius: 8,
        overflow:   'hidden',
        background: '#0f1117',
      }}
    />
  )
}
