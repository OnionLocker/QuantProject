/**
 * BacktestChart.jsx — lightweight-charts v5
 * 优化：去掉密集 SL/TP 价格线，精简标记文字
 */
import { useEffect, useRef, useState } from 'react'
import {
  createChart,
  CandlestickSeries,
  createSeriesMarkers,
  LineStyle,
} from 'lightweight-charts'

export default function BacktestChart({ candles = [], trades = [], highlightIdx = null }) {
  const containerRef  = useRef(null)
  const chartRef      = useRef(null)
  const seriesRef     = useRef(null)
  const markersApiRef = useRef(null)
  const slLinesRef    = useRef([])   // 当前高亮交易的 SL/TP 线

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
        borderColor:  'rgba(255,255,255,0.1)',
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

    const series = chart.addSeries(CandlestickSeries, {
      upColor:         '#26a69a',
      downColor:       '#ef5350',
      borderUpColor:   '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor:     '#26a69a',
      wickDownColor:   '#ef5350',
    })

    const markersApi = createSeriesMarkers(series, [])

    chartRef.current      = chart
    seriesRef.current     = series
    markersApiRef.current = markersApi

    return () => {
      chart.remove()
      chartRef.current      = null
      seriesRef.current     = null
      markersApiRef.current = null
    }
  }, [])

  // ── 载入 K 线 ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !candles.length) return
    seriesRef.current.setData(candles)
    chartRef.current?.timeScale().fitContent()
  }, [candles])

  // ── 更新标记（精简文字，去掉 SL/TP 线）────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !markersApiRef.current) return

    if (!trades.length) {
      markersApiRef.current.setMarkers([])
      return
    }

    const markers = []

    trades.forEach((t, idx) => {
      const isLong    = t.side === 'long'
      const num       = `#${idx + 1}`

      // 入场：只显示编号，不显示价格（价格通过十字线读取）
      if (t.entry_ts) {
        markers.push({
          time:     Math.floor(new Date(t.entry_ts).getTime() / 1000),
          position: isLong ? 'belowBar' : 'aboveBar',
          color:    isLong ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowUp' : 'arrowDown',
          text:     num,
        })
      }

      // 平仓：显示编号 + PnL，不显示价格
      if (t.exit_ts && t.exit_price) {
        const icon   = t.exit_reason === '止盈' ? '🎉'
                     : t.exit_reason === '止损' ? '🩸' : '↩'
        const pnlStr = t.pnl != null
          ? ` ${t.pnl >= 0 ? '+' : ''}${t.pnl}U`
          : ''
        markers.push({
          time:     Math.floor(new Date(t.exit_ts).getTime() / 1000),
          position: isLong ? 'aboveBar' : 'belowBar',
          color:    t.result === 'win' ? '#26a69a' : '#ef5350',
          shape:    isLong ? 'arrowDown' : 'arrowUp',
          text:     `${icon}${num}${pnlStr}`,
        })
      }
    })

    markers.sort((a, b) => a.time - b.time)
    markersApiRef.current.setMarkers(markers)
  }, [trades, candles])

  // ── 高亮某笔交易时显示 SL/TP 线 ──────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current) return

    // 清除旧 SL/TP 线
    slLinesRef.current.forEach(l => {
      try { seriesRef.current.removePriceLine(l) } catch {}
    })
    slLinesRef.current = []

    if (highlightIdx == null || !trades[highlightIdx]) return

    const t = trades[highlightIdx]

    if (t.sl) {
      slLinesRef.current.push(
        seriesRef.current.createPriceLine({
          price:            t.sl,
          color:            'rgba(239,83,80,0.8)',
          lineWidth:        1,
          lineStyle:        LineStyle.Dashed,
          axisLabelVisible: true,
          title:            `SL #${highlightIdx + 1}`,
        })
      )
    }
    if (t.tp) {
      slLinesRef.current.push(
        seriesRef.current.createPriceLine({
          price:            t.tp,
          color:            'rgba(38,166,154,0.8)',
          lineWidth:        1,
          lineStyle:        LineStyle.Dashed,
          axisLabelVisible: true,
          title:            `TP #${highlightIdx + 1}`,
        })
      )
    }

    // 跳转到该笔交易的入场时间
    if (t.entry_ts && chartRef.current) {
      const ts = Math.floor(new Date(t.entry_ts).getTime() / 1000)
      chartRef.current.timeScale().scrollToPosition(
        chartRef.current.timeScale().coordinateToLogical(
          chartRef.current.timeScale().timeToCoordinate(ts)
        ) ?? 0,
        true
      )
    }
  }, [highlightIdx, trades])

  if (!candles.length) return null

  return (
    <div
      ref={containerRef}
      style={{
        width:        '100%',
        height:       460,
        borderRadius: 8,
        overflow:     'hidden',
        background:   '#0f1117',
      }}
    />
  )
}
