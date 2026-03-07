import { useState, useEffect, useRef } from 'react'
import { dataApi } from '../api'

// localStorage key
const LS_KEY = 'quantbot_backtest_state'

// ── 持久化辅助 ────────────────────────────────────────────────────────────────
function saveLocal(data) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(data)) } catch {}
}
function loadLocal() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null') } catch { return null }
}
function clearLocal() {
  try { localStorage.removeItem(LS_KEY) } catch {}
}

// ── 进度条组件 ────────────────────────────────────────────────────────────────
function ProgressBar({ elapsedSec, estimatedSec }) {
  // 根据预估时间算"伪进度"，到 90% 后停住等结果
  const raw = estimatedSec > 0 ? (elapsedSec / estimatedSec) * 100 : 0
  const pct = Math.min(raw, 92)

  return (
    <div style={{ marginTop: 14 }}>
      {/* 轨道 */}
      <div style={{
        height: 6, borderRadius: 3,
        background: 'rgba(255,255,255,0.08)',
        overflow: 'hidden', position: 'relative',
      }}>
        {/* 已完成部分 */}
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: 'var(--blue)',
          borderRadius: 3,
          transition: 'width 1s linear',
        }} />
        {/* 扫光动画（叠加在上方） */}
        <div style={{
          position: 'absolute', top: 0, left: 0,
          height: '100%', width: '100%',
          background: 'linear-gradient(90deg,transparent 0%,rgba(255,255,255,0.18) 50%,transparent 100%)',
          animation: 'shimmer 1.6s infinite',
        }} />
      </div>

      <div style={{
        display: 'flex', justifyContent: 'space-between',
        marginTop: 6, fontSize: 12, color: 'var(--muted)',
      }}>
        <span>⏳ 计算中... 已用时 {elapsedSec}s</span>
        <span>{Math.round(pct)}%</span>
      </div>
    </div>
  )
}

// ── 资金曲线 SVG ──────────────────────────────────────────────────────────────
function EquityCurve({ data, initialCapital }) {
  if (!data || data.length < 2) return null

  const W = 680, H = 180, PAD = { t: 12, r: 16, b: 28, l: 56 }
  const innerW = W - PAD.l - PAD.r
  const innerH = H - PAD.t - PAD.b

  const balances = data.map(d => d.balance)
  const minB = Math.min(...balances, initialCapital)
  const maxB = Math.max(...balances, initialCapital)
  const rangeB = maxB - minB || 1

  const toX = i => PAD.l + (i / (data.length - 1)) * innerW
  const toY = b => PAD.t + innerH - ((b - minB) / rangeB) * innerH

  const polyline = data.map((d, i) => `${toX(i)},${toY(d.balance)}`).join(' ')
  const area = [
    `${PAD.l},${PAD.t + innerH}`,
    ...data.map((d, i) => `${toX(i)},${toY(d.balance)}`),
    `${toX(data.length - 1)},${PAD.t + innerH}`,
  ].join(' ')

  const baseline = toY(initialCapital)
  const isProfit = balances[balances.length - 1] >= initialCapital
  const lineColor = isProfit ? '#27ae60' : '#e74c3c'
  const fillColor = isProfit ? 'rgba(39,174,96,0.12)' : 'rgba(231,76,60,0.12)'

  const yTicks = [minB, (minB + maxB) / 2, maxB].map(v => ({
    y: toY(v),
    label: v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0),
  }))

  const xLabels = []
  const step = Math.max(1, Math.floor(data.length / 5))
  for (let i = 0; i < data.length; i += step)
    xLabels.push({ x: toX(i), label: data[i].date.slice(5) })

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: W, display: 'block' }}>
      <line x1={PAD.l} y1={baseline} x2={W - PAD.r} y2={baseline}
            stroke='rgba(255,255,255,0.15)' strokeDasharray='4 3' strokeWidth='1' />
      <polygon points={area} fill={fillColor} />
      <polyline points={polyline} fill='none' stroke={lineColor} strokeWidth='2'
                strokeLinejoin='round' strokeLinecap='round' />
      {yTicks.map((t, i) => (
        <g key={i}>
          <line x1={PAD.l - 4} y1={t.y} x2={PAD.l} y2={t.y}
                stroke='rgba(255,255,255,0.3)' strokeWidth='1' />
          <text x={PAD.l - 6} y={t.y + 4} textAnchor='end'
                fill='rgba(255,255,255,0.5)' fontSize='10'>{t.label}</text>
        </g>
      ))}
      {xLabels.map((l, i) => (
        <text key={i} x={l.x} y={H - 6} textAnchor='middle'
              fill='rgba(255,255,255,0.4)' fontSize='9'>{l.label}</text>
      ))}
      <line x1={PAD.l} y1={PAD.t} x2={PAD.l} y2={PAD.t + innerH}
            stroke='rgba(255,255,255,0.2)' strokeWidth='1' />
    </svg>
  )
}

// ── 统计卡片 ──────────────────────────────────────────────────────────────────
function StatCard({ label, value, color }) {
  return (
    <div className='card stat-card'>
      <div className='label'>{label}</div>
      <div className='value' style={{ fontSize: 20, color: color || 'inherit' }}>{value}</div>
    </div>
  )
}

// ── 估算秒数 ──────────────────────────────────────────────────────────────────
function estimateSec(form) {
  const days = Math.max(1,
    Math.round((new Date(form.end_date) - new Date(form.start_date)) / 86400000))
  const tf = form.timeframe
  const candles = tf === '15m' ? days * 96 : tf === '1h' ? days * 24 :
                  tf === '4h'  ? days * 6  : days
  return candles > 10000 ? 90 : candles > 3000 ? 45 : 20
}

// ── 主页面 ────────────────────────────────────────────────────────────────────
export default function BacktestPage() {
  const today       = new Date().toISOString().slice(0, 10)
  const oneYearAgo  = new Date(Date.now() - 365 * 86400000).toISOString().slice(0, 10)

  const [options, setOptions] = useState({ symbols: [], timeframes: [], strategies: [] })
  const [form, setForm] = useState({
    strategy_name:   'PA_V2',
    symbol:          'BTC/USDT',
    timeframe:       '1h',
    start_date:      oneYearAgo,
    end_date:        today,
    initial_capital: 5000,
  })

  // 运行状态
  const [running, setRunning]     = useState(false)
  const [elapsedSec, setElapsed]  = useState(0)
  const [estSec, setEstSec]       = useState(45)
  const [result, setResult]       = useState(null)
  const [error, setError]         = useState(null)

  const pollingRef = useRef(null)
  const timerRef   = useRef(null)

  // ── 初始化：加载选项 + 恢复运行状态 ────────────────────────────────────────
  useEffect(() => {
    dataApi.backtestOptions().then(r => setOptions(r.data)).catch(() => {})

    // 从 localStorage 恢复上次状态
    const saved = loadLocal()
    if (saved) {
      if (saved.form)   setForm(saved.form)
      if (saved.result) setResult(saved.result)
      if (saved.error)  setError(saved.error)

      if (saved.running) {
        // 上次离开时还在运行，恢复轮询
        const alreadyElapsed = Math.round((Date.now() - saved.startTs) / 1000)
        setElapsed(alreadyElapsed)
        setEstSec(saved.estSec || 45)
        setRunning(true)
        startPolling(alreadyElapsed)
      }
    }

    return () => { stopAll() }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function stopAll() {
    if (pollingRef.current) clearInterval(pollingRef.current)
    if (timerRef.current)   clearInterval(timerRef.current)
  }

  function startTimer(initSec = 0) {
    if (timerRef.current) clearInterval(timerRef.current)
    let t = initSec
    setElapsed(t)
    timerRef.current = setInterval(() => {
      t++
      setElapsed(t)
    }, 1000)
  }

  function startPolling(initElapsed = 0) {
    startTimer(initElapsed)
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try {
        const r = await dataApi.backtestResult()
        const d = r.data
        if (d?.status === 'done') {
          stopAll()
          setResult(d)
          setRunning(false)
          saveLocal({ form, result: d, running: false })
        } else if (d?.status === 'error') {
          stopAll()
          setError(d.error || '回测失败')
          setRunning(false)
          saveLocal({ form, error: d.error, running: false })
        }
        // status === 'running' 继续等待
      } catch {
        // 网络抖动，继续等
      }
    }, 2500)
  }

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const startBacktest = async () => {
    stopAll()
    setResult(null)
    setError(null)
    setRunning(true)
    setElapsed(0)
    const est = estimateSec(form)
    setEstSec(est)

    // 持久化"正在运行"状态
    saveLocal({ form, running: true, startTs: Date.now(), estSec: est })

    try {
      await dataApi.runBacktest({
        strategy_name:   form.strategy_name,
        symbol:          form.symbol,
        timeframe:       form.timeframe,
        start_date:      form.start_date,
        end_date:        form.end_date,
        initial_capital: Number(form.initial_capital),
      })
      startPolling(0)
    } catch (err) {
      stopAll()
      const msg = err.response?.data?.detail || err.message
      setError(msg)
      setRunning(false)
      saveLocal({ form, error: msg, running: false })
    }
  }

  const resetPage = () => {
    stopAll()
    setRunning(false)
    setResult(null)
    setError(null)
    setElapsed(0)
    clearLocal()
  }

  const roiColor = r => r?.roi_pct >= 0 ? 'var(--green)' : 'var(--red)'

  return (
    <div>
      {/* shimmer keyframe 注入 */}
      <style>{`
        @keyframes shimmer {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(100%); }
        }
      `}</style>

      <h1 className='page-title'>🧪 策略回测</h1>

      {/* ── 参数配置区 ───────────────────────────────────────────────── */}
      <div className='card' style={{ maxWidth: 720, marginBottom: 24 }}>
        <div style={{ fontWeight: 600, marginBottom: 18 }}>回测参数配置</div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
          <div className='form-group'>
            <label>策略</label>
            <select value={form.strategy_name} onChange={e => set('strategy_name', e.target.value)}
                    disabled={running}>
              {options.strategies.map(s => (
                <option key={s.name} value={s.name}>{s.name}</option>
              ))}
            </select>
          </div>

          <div className='form-group'>
            <label>交易品种</label>
            <select value={form.symbol} onChange={e => set('symbol', e.target.value)}
                    disabled={running}>
              {options.symbols.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className='form-group'>
            <label>K 线周期</label>
            <select value={form.timeframe} onChange={e => set('timeframe', e.target.value)}
                    disabled={running}>
              {options.timeframes.map(t => (
                <option key={t} value={t}>
                  {t === '15m' ? '15 分钟' : t === '1h' ? '1 小时' :
                   t === '4h'  ? '4 小时'  : '日线'}
                </option>
              ))}
            </select>
          </div>

          <div className='form-group'>
            <label>初始资金（U）</label>
            <input type='number' min='100' step='100'
                   value={form.initial_capital}
                   onChange={e => set('initial_capital', e.target.value)}
                   disabled={running} />
          </div>

          <div className='form-group'>
            <label>开始日期</label>
            <input type='date' value={form.start_date} max={form.end_date}
                   onChange={e => set('start_date', e.target.value)}
                   disabled={running} />
          </div>

          <div className='form-group'>
            <label>结束日期</label>
            <input type='date' value={form.end_date}
                   min={form.start_date} max={today}
                   onChange={e => set('end_date', e.target.value)}
                   disabled={running} />
          </div>
        </div>

        {/* 快捷日期 */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
          {[
            { label: '近 3 个月', days: 90 },
            { label: '近 6 个月', days: 180 },
            { label: '近 1 年',   days: 365 },
            { label: '近 2 年',   days: 730 },
            { label: '近 3 年',   days: 1095 },
          ].map(({ label, days }) => (
            <button key={label} disabled={running}
              onClick={() => {
                const start = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10)
                setForm(f => ({ ...f, start_date: start, end_date: today }))
              }}
              style={{
                padding: '4px 12px', fontSize: 12,
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.15)',
                color: 'var(--text)', borderRadius: 6,
              }}
            >{label}</button>
          ))}
        </div>

        {/* 按钮行 */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button className='btn-primary' onClick={startBacktest}
                  disabled={running} style={{ minWidth: 140 }}>
            {running ? '⏳ 回测运行中...' : '▶ 开始回测'}
          </button>
          {(running || result || error) && (
            <button onClick={resetPage}
                    style={{
                      padding: '8px 14px', fontSize: 13,
                      background: 'transparent',
                      border: '1px solid var(--border)',
                      color: 'var(--muted)', borderRadius: 6,
                    }}>
              重置
            </button>
          )}
        </div>

        {/* 进度条 */}
        {running && (
          <ProgressBar elapsedSec={elapsedSec} estimatedSec={estSec} />
        )}

        {/* 提示文字（运行中） */}
        {running && (
          <p style={{ marginTop: 8, color: 'var(--muted)', fontSize: 12 }}>
            数据首次下载较慢，后续使用本地缓存（约 {estSec}s）。切换页面不影响进度，回来即可查看结果。
          </p>
        )}

        {/* 错误 */}
        {error && !running && (
          <p style={{ marginTop: 12, color: 'var(--red)', fontSize: 13 }}>❌ {error}</p>
        )}
      </div>

      {/* ── 回测结果区 ──────────────────────────────────────────────── */}
      {result && result.status === 'done' && (() => {
        const r = result
        const roiSign = r.roi_pct >= 0 ? '+' : ''
        return (
          <div className='card' style={{ maxWidth: 720 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              📊 {r.strategy} — {r.symbol} {r.timeframe}
            </div>
            <div style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 20 }}>
              {r.start_date} → {r.end_date} · {r.candle_count} 根 K 线 · 初始资金 {r.initial_capital} U
            </div>

            <div className='grid-4' style={{ marginBottom: 20 }}>
              <StatCard label='最终余额'  value={`${r.final_balance} U`} />
              <StatCard label='净收益率'
                        value={`${roiSign}${r.roi_pct}%`}
                        color={roiColor(r)} />
              <StatCard label='最大回撤'
                        value={`${r.max_drawdown_pct}%`}
                        color='#f39c12' />
              <StatCard label='总手续费'  value={`${r.total_fees_paid} U`} />
            </div>

            <div className='grid-4' style={{ marginBottom: 20 }}>
              <StatCard label='总交易次数' value={r.total_trades} />
              <StatCard label='盈利次数'
                        value={<span className='tag-green'>{r.winning_trades}</span>} />
              <StatCard label='亏损次数'
                        value={<span className='tag-red'>{r.losing_trades}</span>} />
              <StatCard label='胜率'
                        value={`${r.win_rate_pct}%`}
                        color={r.win_rate_pct >= 50 ? 'var(--green)' : 'var(--yellow)'} />
            </div>

            {r.equity_curve?.length > 1 && (
              <>
                <div style={{ fontWeight: 500, marginBottom: 10, fontSize: 14 }}>资金曲线</div>
                <div style={{
                  background: 'rgba(0,0,0,0.2)', borderRadius: 8, padding: '8px 4px',
                  marginBottom: 4,
                }}>
                  <EquityCurve data={r.equity_curve} initialCapital={r.initial_capital} />
                </div>
                <div style={{ color: 'var(--muted)', fontSize: 11, textAlign: 'right' }}>
                  每笔交易后更新
                </div>
              </>
            )}
          </div>
        )
      })()}
    </div>
  )
}
