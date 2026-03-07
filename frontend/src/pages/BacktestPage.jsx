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
function ProgressBar({ elapsedSec, estimatedSec, timedOut }) {
  // 根据预估时间算"伪进度"，到 90% 后停住等结果；超时后变红显示 100%
  const raw = estimatedSec > 0 ? (elapsedSec / estimatedSec) * 100 : 0
  const pct = timedOut ? 100 : Math.min(raw, 92)
  const barColor = timedOut ? 'var(--red)' : 'var(--blue)'

  return (
    <div style={{ marginTop: 14 }}>
      <div style={{
        height: 6, borderRadius: 3,
        background: 'rgba(255,255,255,0.08)',
        overflow: 'hidden', position: 'relative',
      }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: barColor,
          borderRadius: 3,
          transition: 'width 1s linear',
        }} />
        {!timedOut && (
          <div style={{
            position: 'absolute', top: 0, left: 0,
            height: '100%', width: '100%',
            background: 'linear-gradient(90deg,transparent 0%,rgba(255,255,255,0.18) 50%,transparent 100%)',
            animation: 'shimmer 1.6s infinite',
          }} />
        )}
      </div>

      <div style={{
        display: 'flex', justifyContent: 'space-between',
        marginTop: 6, fontSize: 12,
        color: timedOut ? 'var(--red)' : 'var(--muted)',
      }}>
        <span>{timedOut ? '⚠️ 超时' : `⏳ 计算中... 已用时 ${elapsedSec}s`}</span>
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
  for (let i = 0; i < data.length; i += step) {
    const d = data[i].date          // "2025-03-11"
    const label = d.slice(2, 4) + '/' + d.slice(5, 7)  // "25/03"
    xLabels.push({ x: toX(i), label })
  }

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

// ── 折叠面板组件 ───────────────────────────────────────────────────────────────
function Collapse({ title, badge, children }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ marginBottom: 12 }}>
      <button onClick={() => setOpen(o => !o)} style={{
        width: '100%', display: 'flex', alignItems: 'center', gap: 8,
        background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)',
        borderRadius: 8, padding: '8px 14px', cursor: 'pointer',
        color: 'var(--text)', fontSize: 13, fontWeight: 500,
      }}>
        <span style={{ flex: 1, textAlign: 'left' }}>{title}</span>
        {badge && <span style={{
          fontSize: 11, padding: '2px 8px', borderRadius: 10,
          background: 'rgba(59,130,246,0.2)', color: 'var(--blue)',
        }}>{badge}</span>}
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{open ? '▲ 收起' : '▼ 展开'}</span>
      </button>
      {open && (
        <div style={{
          border: '1px solid rgba(255,255,255,0.08)', borderTop: 'none',
          borderRadius: '0 0 8px 8px', padding: '16px 14px',
          background: 'rgba(255,255,255,0.02)',
        }}>
          {children}
        </div>
      )}
    </div>
  )
}

// ── 参数输入项 ─────────────────────────────────────────────────────────────────
function ParamInput({ p, value, onChange, disabled }) {
  return (
    <div className='form-group' title={p.tip || ''}>
      <label style={{ fontSize: 12 }}>
        {p.label}
        {p.tip && <span style={{ color: 'var(--muted)', marginLeft: 4, fontWeight: 400 }}>ⓘ</span>}
      </label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type='range'
          min={p.min} max={p.max} step={p.step}
          value={value}
          onChange={e => onChange(p.type === 'int' ? parseInt(e.target.value) : parseFloat(e.target.value))}
          disabled={disabled}
          style={{ flex: 1, accentColor: 'var(--blue)' }}
        />
        <span style={{
          minWidth: 42, textAlign: 'right', fontSize: 13,
          fontWeight: 600, color: 'var(--blue)',
        }}>{value}</span>
      </div>
    </div>
  )
}

// ── 主页面 ────────────────────────────────────────────────────────────────────
export default function BacktestPage() {
  const today       = new Date().toISOString().slice(0, 10)
  const oneYearAgo  = new Date(Date.now() - 365 * 86400000).toISOString().slice(0, 10)

  const [options, setOptions] = useState({ symbols: [], timeframes: [], strategies: [] })

  // 基础表单（strategy_name 在 options 加载后动态修正为第一个可用策略）
  const [form, setForm] = useState({
    strategy_name:   'PA_5S',
    symbol:          'BTC/USDT',
    timeframe:       '1h',
    start_date:      oneYearAgo,
    end_date:        today,
    initial_capital: 5000,
  })
  // 执行层参数
  const [execParams, setExecParams] = useState({
    leverage:  3,
    risk_pct:  0.01,
    fee_rate:  0.0005,
    slippage:  0.0002,
  })
  // 策略层参数（动态，跟随所选策略的 PARAMS 元数据）
  const [strategyParams, setStrategyParams] = useState({})

  const [activeDays, setActiveDays] = useState(365)
  const [formDirty, setFormDirty] = useState(false)

  // 运行状态
  const [running, setRunning]     = useState(false)
  const [elapsedSec, setElapsed]  = useState(0)
  const [estSec, setEstSec]       = useState(45)
  const [result, setResult]       = useState(null)
  const [error, setError]         = useState(null)
  const [timedOut, setTimedOut]   = useState(false)   // 超时标志

  const pollingRef  = useRef(null)
  const timerRef    = useRef(null)
  const elapsedRef  = useRef(0)    // 用 ref 在 interval 回调里读取最新值

  const MAX_WAIT_SEC = 300   // 前端最长等待 5 分钟

  // 当策略切换时，从 options 里取出该策略的 PARAMS 元数据，初始化策略参数默认值
  const currentStrategyMeta = options.strategies.find(s => s.name === form.strategy_name)
  const currentStrategyPARAMS = currentStrategyMeta?.params || []

  useEffect(() => {
    if (currentStrategyPARAMS.length > 0) {
      setStrategyParams(prev => {
        const defaults = {}
        currentStrategyPARAMS.forEach(p => { defaults[p.key] = p.default })
        // 只填充还没有值的 key，避免覆盖用户已调整的值
        return { ...defaults, ...prev }
      })
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.strategy_name, options.strategies.length])

  // ── 初始化：加载选项 + 恢复运行状态 ────────────────────────────────────────
  useEffect(() => {
    dataApi.backtestOptions().then(r => {
      setOptions(r.data)
      // 如果当前 form 里的策略名在新注册表里不存在，自动修正为第一个可用策略
      const validNames = (r.data.strategies || []).map(s => s.name)
      if (validNames.length > 0) {
        setForm(f => ({
          ...f,
          strategy_name: validNames.includes(f.strategy_name) ? f.strategy_name : validNames[0],
        }))
      }
    }).catch(() => {})

    // 从 localStorage 恢复上次状态
    const saved = loadLocal()
    if (saved) {
      if (saved.form)       setForm(saved.form)
      if (saved.result)     setResult(saved.result)
      if (saved.error)      setError(saved.error)
      if ('activeDays' in saved) setActiveDays(saved.activeDays)

      if (saved.running) {
        // 上次离开时还在运行，恢复轮询
        const alreadyElapsed = Math.round((Date.now() - saved.startTs) / 1000)
        elapsedRef.current = alreadyElapsed
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
    elapsedRef.current = t
    setElapsed(t)
    timerRef.current = setInterval(() => {
      t++
      elapsedRef.current = t
      setElapsed(t)
    }, 1000)
  }

  function startPolling(initElapsed = 0) {
    startTimer(initElapsed)
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      // 前端超时保护：超过 MAX_WAIT_SEC 秒仍未结束，主动报错
      if (elapsedRef.current >= MAX_WAIT_SEC) {
        stopAll()
        const msg = `回测超时（等待超过 ${MAX_WAIT_SEC}s），服务端可能已报错。请查看服务日志或重试。`
        setError(msg)
        setRunning(false)
        setTimedOut(true)
        saveLocal({ form, error: msg, running: false })
        return
      }
      try {
        const r = await dataApi.backtestResult()
        const d = r.data
        if (d?.status === 'done') {
          stopAll()
          setResult(d)
          setRunning(false)
          setTimedOut(false)
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

  // 修改任意参数时：标记 dirty（需要重新回测），日期类型额外处理快捷按钮高亮
  const set = (k, v) => {
    if (k === 'start_date' || k === 'end_date') {
      if (k === 'end_date' && v > today) v = today
      setActiveDays(null)
    }
    setFormDirty(true)
    setForm(f => ({ ...f, [k]: v }))
  }

  const startBacktest = async () => {
    stopAll()
    setResult(null)
    setError(null)
    setRunning(true)
    setElapsed(0)
    setTimedOut(false)
    setFormDirty(false)
    elapsedRef.current = 0
    const est = estimateSec(form)
    setEstSec(est)

    // 持久化"正在运行"状态
    saveLocal({ form, activeDays, running: true, startTs: Date.now(), estSec: est })

    try {
      await dataApi.runBacktest({
        strategy_name:   form.strategy_name,
        symbol:          form.symbol,
        timeframe:       form.timeframe,
        start_date:      form.start_date,
        end_date:        form.end_date,
        initial_capital: Number(form.initial_capital),
        leverage:        Number(execParams.leverage),
        risk_pct:        Number(execParams.risk_pct),
        fee_rate:        Number(execParams.fee_rate),
        slippage:        Number(execParams.slippage),
        strategy_params: strategyParams,
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
    setTimedOut(false)
    elapsedRef.current = 0
    setActiveDays(365)
    setFormDirty(false)
    setExecParams({ leverage: 3, risk_pct: 0.01, fee_rate: 0.0005, slippage: 0.0002 })
    setStrategyParams({})
    setForm({
      strategy_name:   options.strategies[0]?.name || 'PA_5S',
      symbol:          'BTC/USDT',
      timeframe:       '1h',
      start_date:      oneYearAgo,
      end_date:        today,
      initial_capital: 5000,
    })
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
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          {[
            { label: '近 3 个月', days: 90 },
            { label: '近 6 个月', days: 180 },
            { label: '近 1 年',   days: 365 },
            { label: '近 2 年',   days: 730 },
            { label: '近 3 年',   days: 1095 },
          ].map(({ label, days }) => {
            const isActive = activeDays === days
            return (
              <button key={label} disabled={running}
                onClick={() => {
                  const start = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10)
                  setForm(f => ({ ...f, start_date: start, end_date: today }))
                  setActiveDays(days)
                  setFormDirty(true)
                }}
                style={{
                  padding: '4px 12px', fontSize: 12,
                  background: isActive ? 'var(--blue)' : 'rgba(255,255,255,0.06)',
                  border: `1px solid ${isActive ? 'var(--blue)' : 'rgba(255,255,255,0.15)'}`,
                  color: isActive ? '#fff' : 'var(--text)',
                  borderRadius: 6,
                  fontWeight: isActive ? 600 : 400,
                  cursor: running ? 'not-allowed' : 'pointer',
                  transition: 'all 0.15s',
                }}
              >{label}</button>
            )
          })}
          {activeDays === null && (
            <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 2 }}>自定义区间</span>
          )}
        </div>

        {/* ── 执行层参数 ─────────────────────────────────────────────── */}
        <Collapse title="⚙️ 执行参数" badge="杠杆 / 风险 / 费率">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
            {[
              { key: 'leverage',  label: '杠杆倍数',         type: 'int',   min: 1,      max: 20,    step: 1,      tip: '放大收益与风险，建议≤5x' },
              { key: 'risk_pct',  label: '单笔风险占比',      type: 'float', min: 0.002,  max: 0.05,  step: 0.002,  tip: '每笔交易最多亏掉本金的百分比' },
              { key: 'fee_rate',  label: '手续费率',          type: 'float', min: 0.0001, max: 0.002, step: 0.0001, tip: 'OKX 合约 Taker 费率约 0.05%' },
              { key: 'slippage',  label: '滑点',              type: 'float', min: 0,      max: 0.005, step: 0.0001, tip: '成交价与信号价的偏差假设' },
            ].map(p => {
              const fmt = v => p.key === 'risk_pct' ? `${(v*100).toFixed(1)}%`
                             : p.key === 'fee_rate'  ? `${(v*100).toFixed(3)}%`
                             : p.key === 'slippage'  ? `${(v*100).toFixed(3)}%`
                             : `${v}x`
              return (
                <div className='form-group' key={p.key} title={p.tip}>
                  <label style={{ fontSize: 12 }}>
                    {p.label}
                    <span style={{ color: 'var(--muted)', marginLeft: 6, fontWeight: 400, fontSize: 11 }}>
                      ({p.tip})
                    </span>
                  </label>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <input type='range' min={p.min} max={p.max} step={p.step}
                      value={execParams[p.key]}
                      onChange={e => {
                        const v = p.type === 'int' ? parseInt(e.target.value) : parseFloat(e.target.value)
                        setExecParams(prev => ({ ...prev, [p.key]: v }))
                        setFormDirty(true)
                      }}
                      disabled={running}
                      style={{ flex: 1, accentColor: 'var(--blue)' }}
                    />
                    <span style={{ minWidth: 52, textAlign: 'right', fontSize: 13, fontWeight: 600, color: 'var(--blue)' }}>
                      {fmt(execParams[p.key])}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        </Collapse>

        {/* ── 策略层参数（动态） ──────────────────────────────────────── */}
        {currentStrategyPARAMS.length > 0 && (
          <Collapse title={`🎯 策略参数（${form.strategy_name}）`} badge={`${currentStrategyPARAMS.length} 项`}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
              {currentStrategyPARAMS.map(p => (
                <ParamInput key={p.key} p={p}
                  value={strategyParams[p.key] ?? p.default}
                  onChange={v => {
                    setStrategyParams(prev => ({ ...prev, [p.key]: v }))
                    setFormDirty(true)
                  }}
                  disabled={running}
                />
              ))}
            </div>
          </Collapse>
        )}

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
        {(running || timedOut) && (
          <ProgressBar elapsedSec={elapsedSec} estimatedSec={estSec} timedOut={timedOut} />
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

      {/* ── 参数已修改提示 ──────────────────────────────────────────── */}
      {result && formDirty && !running && (
        <div style={{
          maxWidth: 720, marginBottom: 12,
          padding: '10px 16px', borderRadius: 8,
          background: 'rgba(255,193,7,0.08)',
          border: '1px solid rgba(255,193,7,0.3)',
          color: '#f39c12', fontSize: 13,
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          ⚠️ 参数已修改，以下为<strong>上一次</strong>的回测结果，点击「开始回测」查看新结果
        </div>
      )}

      {/* ── 回测结果区 ──────────────────────────────────────────────── */}
      {result && result.status === 'done' && (() => {
        const r = result
        const roiSign = r.roi_pct >= 0 ? '+' : ''
        return (
          <div className='card' style={{ maxWidth: 720 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              📊 {r.strategy} — {r.symbol} {r.timeframe}
            </div>
            <div style={{ color: 'var(--muted)', fontSize: 13, marginBottom: r.note ? 12 : 20 }}>
              {r.start_date} → {r.end_date} · {r.candle_count} 根 K 线 · 初始资金 {r.initial_capital} U
              {r.leverage && (
                <span style={{ marginLeft: 8 }}>
                  · {r.leverage}x 杠杆 · 风险 {(r.risk_pct*100).toFixed(1)}%/笔
                </span>
              )}
            </div>

            {/* 无交易提示 */}
            {r.note && (
              <div style={{
                marginBottom: 20, padding: '10px 14px', borderRadius: 8,
                background: 'rgba(255,193,7,0.08)',
                border: '1px solid rgba(255,193,7,0.25)',
                color: '#f39c12', fontSize: 13,
              }}>
                ⚠️ {r.note}
              </div>
            )}

            {r.total_trades === 0 ? (
              <div style={{
                padding: '28px 0', textAlign: 'center',
                color: 'var(--muted)', fontSize: 14,
              }}>
                📭 本区间内策略未触发任何交易
              </div>
            ) : (
              <>
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
              </>
            )}

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
