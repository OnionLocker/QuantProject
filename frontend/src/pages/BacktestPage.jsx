import { useState, useEffect, useRef } from 'react'
import { dataApi } from '../api'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts'
import { Play, RotateCcw, TrendingUp, TrendingDown, Percent, BarChart2, Award, AlertTriangle } from 'lucide-react'

const SYMBOLS    = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT"]
const TIMEFRAMES = ["15m","1h","4h","1d"]

const EqTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="tooltip-box">
      <div style={{ color:'var(--muted)', fontSize:11, marginBottom:3 }}>{label}</div>
      <div style={{ fontWeight:700 }}>${parseFloat(payload[0].value).toFixed(2)}</div>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, sub, color }) {
  return (
    <div className="stat-cell">
      <div className="s-label" style={{ display:'flex', alignItems:'center', gap:4 }}>
        {Icon && <Icon size={11} />}{label}
      </div>
      <div className="s-value" style={{ color: color || 'var(--text)', marginTop:4 }}>{value}</div>
      {sub && <div className="s-sub" style={{ marginTop:3 }}>{sub}</div>}
    </div>
  )
}

export default function BacktestPage() {
  const [strategies, setStrategies] = useState([])
  const [stratParams, setStratParams] = useState([])

  const [form, setForm] = useState({
    strategy_name:   '',
    symbol:          'BTC/USDT',
    timeframe:       '1h',
    start_date:      '2023-01-01',
    end_date:        new Date().toISOString().slice(0,10),
    initial_capital: 5000,
    leverage:        3,
    risk_pct:        0.01,
    fee_rate:        0.0005,
    slippage:        0.0002,
    strategy_params: {},
  })

  const [running,  setRunning]  = useState(false)
  const [result,   setResult]   = useState(null)
  const [errMsg,   setErrMsg]   = useState('')
  const pollRef = useRef(null)

  // 加载策略列表
  useEffect(() => {
    dataApi.strategies().then(r => {
      const list = r.data
      setStrategies(list)
      if (list.length > 0) {
        const first = list[0].name
        setForm(f => ({ ...f, strategy_name: first }))
        setStratParams(list[0].params || [])
      }
    })
  }, [])

  const onStrategyChange = e => {
    const name = e.target.value
    setForm(f => ({ ...f, strategy_name: name, strategy_params: {} }))
    const found = strategies.find(s => s.name === name)
    setStratParams(found?.params || [])
  }

  const onParamChange = (key, val, type) => {
    const parsed = type === 'int' ? parseInt(val) : parseFloat(val)
    setForm(f => ({ ...f, strategy_params: { ...f.strategy_params, [key]: isNaN(parsed) ? val : parsed } }))
  }

  const startBacktest = async () => {
    setRunning(true)
    setResult(null)
    setErrMsg('')
    try {
      await dataApi.runBacktest(form)
      // 轮询结果
      pollRef.current = setInterval(async () => {
        const r = await dataApi.backtestResult()
        const d = r.data
        if (d.status !== 'running') {
          clearInterval(pollRef.current)
          setRunning(false)
          if (d.status === 'error') {
            setErrMsg(d.error || '回测失败')
          } else {
            setResult(d)
          }
        }
      }, 1500)
    } catch (e) {
      setRunning(false)
      setErrMsg(e.response?.data?.detail || e.message || '启动失败')
    }
  }

  const reset = () => { setResult(null); setErrMsg('') }

  // 图表数据
  const eqData = result?.equity_curve?.map(p => ({
    date:    p.date?.slice(0,10) || p.ts?.slice(0,10) || '',
    balance: parseFloat(p.balance?.toFixed(2) || 0),
  })) || []

  const isProfit  = result && result.roi_pct >= 0
  const chartColor = isProfit ? '#26a69a' : '#ef5350'

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">策略回测</div>
          <div className="page-sub">历史数据模拟验证，参数自由调整</div>
        </div>
        {result && (
          <button className="btn-ghost btn-sm" onClick={reset}>
            <RotateCcw size={13} style={{ marginRight:4 }} />重新配置
          </button>
        )}
      </div>

      {/* ══ 配置面板（无结果时显示）══ */}
      {!result && (
        <div style={{ display:'grid', gridTemplateColumns:'360px 1fr', gap:16 }}>
          {/* 左：基础参数 */}
          <div>
            <div className="card mb-12">
              <div className="card-header">基础参数</div>

              <div className="form-row">
                <label className="form-label">策略</label>
                <select value={form.strategy_name} onChange={onStrategyChange}>
                  {strategies.map(s => (
                    <option key={s.name} value={s.name}>{s.name} · {s.class}</option>
                  ))}
                </select>
              </div>

              <div className="form-grid-2">
                <div className="form-row">
                  <label className="form-label">交易对</label>
                  <select value={form.symbol} onChange={e => setForm(f => ({...f, symbol: e.target.value}))}>
                    {SYMBOLS.map(s => <option key={s}>{s}</option>)}
                  </select>
                </div>
                <div className="form-row">
                  <label className="form-label">K线周期</label>
                  <select value={form.timeframe} onChange={e => setForm(f => ({...f, timeframe: e.target.value}))}>
                    {TIMEFRAMES.map(t => <option key={t}>{t}</option>)}
                  </select>
                </div>
              </div>

              <div className="form-grid-2">
                <div className="form-row">
                  <label className="form-label">开始日期</label>
                  <input type="date" value={form.start_date}
                    onChange={e => setForm(f => ({...f, start_date: e.target.value}))} />
                </div>
                <div className="form-row">
                  <label className="form-label">结束日期</label>
                  <input type="date" value={form.end_date}
                    onChange={e => setForm(f => ({...f, end_date: e.target.value}))} />
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-header">执行参数</div>
              <div className="form-grid-2">
                <div className="form-row">
                  <label className="form-label">初始资金 (U)</label>
                  <input type="number" value={form.initial_capital} min={100}
                    onChange={e => setForm(f => ({...f, initial_capital: +e.target.value}))} />
                </div>
                <div className="form-row">
                  <label className="form-label">杠杆</label>
                  <input type="number" value={form.leverage} min={1} max={125}
                    onChange={e => setForm(f => ({...f, leverage: +e.target.value}))} />
                </div>
                <div className="form-row">
                  <label className="form-label">单笔风险</label>
                  <input type="number" value={form.risk_pct} step={0.001} min={0.001} max={0.1}
                    onChange={e => setForm(f => ({...f, risk_pct: +e.target.value}))} />
                  <span className="form-hint">{(form.risk_pct * 100).toFixed(1)}% 每笔</span>
                </div>
                <div className="form-row">
                  <label className="form-label">手续费率</label>
                  <input type="number" value={form.fee_rate} step={0.0001}
                    onChange={e => setForm(f => ({...f, fee_rate: +e.target.value}))} />
                  <span className="form-hint">{(form.fee_rate * 100).toFixed(3)}%</span>
                </div>
              </div>
            </div>
          </div>

          {/* 右：策略参数 */}
          <div>
            {stratParams.length > 0 && (
              <div className="card mb-12">
                <div className="card-header">策略参数</div>
                <div className="form-grid-2">
                  {stratParams.map(p => (
                    <div className="form-row" key={p.key}>
                      <label className="form-label">{p.label}</label>
                      {p.type === 'str' ? (
                        <select
                          defaultValue={p.default}
                          onChange={e => onParamChange(p.key, e.target.value, 'str')}
                        >
                          {p.key === 'direction' && <>
                            <option value="both">双向 (both)</option>
                            <option value="long">仅做多 (long)</option>
                            <option value="short">仅做空 (short)</option>
                          </>}
                        </select>
                      ) : (
                        <input
                          type="number"
                          defaultValue={p.default}
                          min={p.min} max={p.max} step={p.step}
                          onChange={e => onParamChange(p.key, e.target.value, p.type)}
                        />
                      )}
                      {p.tip && <span className="form-hint">{p.tip}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 参数预览 */}
            <div className="card mb-12" style={{ background:'var(--bg)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:8, fontWeight:600, textTransform:'uppercase', letterSpacing:'.5px' }}>
                参数预览
              </div>
              <div style={{ display:'flex', flexWrap:'wrap', gap:'6px 16px' }}>
                {[
                  ['策略', form.strategy_name],
                  ['品种', form.symbol],
                  ['周期', form.timeframe],
                  ['资金', `${form.initial_capital} U`],
                  ['杠杆', `${form.leverage}x`],
                  ['风险', `${(form.risk_pct*100).toFixed(1)}%`],
                  [form.start_date, '→ '+form.end_date],
                ].map(([k,v]) => (
                  <div key={k} style={{ fontSize:12 }}>
                    <span style={{ color:'var(--muted)' }}>{k}：</span>
                    <span style={{ fontWeight:600 }}>{v}</span>
                  </div>
                ))}
              </div>
            </div>

            {errMsg && (
              <div className="alert alert-danger mb-12">{errMsg}</div>
            )}

            <button
              className="btn-primary"
              onClick={startBacktest}
              disabled={running || !form.strategy_name}
              style={{ width:'100%', padding:'10px 0', fontSize:14 }}
            >
              {running
                ? <><span className="spinner" style={{marginRight:8}} />回测运行中...</>
                : <><Play size={14} style={{marginRight:6}} />开始回测</>
              }
            </button>
          </div>
        </div>
      )}

      {/* ══ 结果面板 ══ */}
      {result && (
        <>
          {/* 核心指标 */}
          <div className="stat-grid stat-grid-4 mb-16">
            <StatCard
              icon={TrendingUp}
              label="最终余额"
              value={`$${result.final_balance?.toFixed(2)}`}
              sub={`初始 $${result.initial_capital}`}
            />
            <StatCard
              icon={isProfit ? TrendingUp : TrendingDown}
              label="总收益率"
              value={`${result.roi_pct >= 0 ? '+' : ''}${result.roi_pct?.toFixed(2)}%`}
              sub={`$${(result.final_balance - result.initial_capital).toFixed(2)}`}
              color={isProfit ? 'var(--green)' : 'var(--red)'}
            />
            <StatCard
              icon={Percent}
              label="胜率"
              value={`${result.win_rate_pct?.toFixed(1)}%`}
              sub={`${result.winning_trades}胜 / ${result.losing_trades}负`}
              color={result.win_rate_pct >= 50 ? 'var(--green)' : 'var(--red)'}
            />
            <StatCard
              icon={AlertTriangle}
              label="最大回撤"
              value={`-${result.max_drawdown_pct?.toFixed(2)}%`}
              color={result.max_drawdown_pct > 20 ? 'var(--red)' : result.max_drawdown_pct > 10 ? 'var(--yellow)' : 'var(--green)'}
            />
          </div>

          <div className="stat-grid" style={{ gridTemplateColumns:'repeat(4,1fr)' }} >
            <StatCard icon={BarChart2} label="总交易次数" value={result.total_trades} sub="笔" />
            <StatCard label="手续费" value={`$${result.total_fees_paid?.toFixed(2)}`} color="var(--red)" />
            <StatCard label="K线数量" value={result.candle_count?.toLocaleString()} sub={`${result.timeframe}`} />
            <StatCard label="回测区间" value={`${result.start_date} → ${result.end_date}`} />
          </div>

          {/* 权益曲线 */}
          {eqData.length > 1 && (
            <div className="card mt-16 mb-16">
              <div className="card-header">权益曲线</div>
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={eqData} margin={{ top:8, right:8, left:8, bottom:0 }}>
                  <defs>
                    <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor={chartColor} stopOpacity={.25} />
                      <stop offset="95%" stopColor={chartColor} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="date" tick={{ fill:'var(--muted)', fontSize:11 }}
                    tickLine={false} axisLine={{ stroke:'var(--border)' }}
                    interval={Math.floor(eqData.length / 6)} />
                  <YAxis tick={{ fill:'var(--muted)', fontSize:11 }} tickLine={false}
                    axisLine={false} tickFormatter={v => `$${v}`} width={64} />
                  <Tooltip content={<EqTooltip />} />
                  <ReferenceLine y={result.initial_capital}
                    stroke="var(--muted2)" strokeDasharray="4 4"
                    label={{ value:`初始$${result.initial_capital}`, fill:'var(--muted)', fontSize:10, position:'right' }}
                  />
                  <Area type="monotone" dataKey="balance"
                    stroke={chartColor} strokeWidth={2}
                    fill="url(#eqGrad)" dot={false}
                    activeDot={{ r:4, fill:chartColor }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* 备注 */}
          {result.note && (
            <div className="alert alert-info mt-12">{result.note}</div>
          )}

          {/* 参数回显 */}
          <div className="card mt-12" style={{ background:'var(--bg)' }}>
            <div style={{ fontSize:11, color:'var(--muted)', marginBottom:8, fontWeight:600, textTransform:'uppercase', letterSpacing:'.5px' }}>
              回测参数
            </div>
            <div style={{ display:'flex', flexWrap:'wrap', gap:'4px 20px', fontSize:12 }}>
              {[
                ['策略', result.strategy],
                ['品种', result.symbol],
                ['周期', result.timeframe],
                ['区间', `${result.start_date} ~ ${result.end_date}`],
                ['初始资金', `$${result.initial_capital}`],
              ].map(([k,v]) => (
                <div key={k}>
                  <span style={{ color:'var(--muted)' }}>{k}：</span>
                  <span style={{ fontWeight:600 }}>{v}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
