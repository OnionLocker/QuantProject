import { useState, useEffect } from 'react'
import { dataApi } from '../api'

export default function BacktestPage() {
  const [strategies, setStrategies] = useState([])
  const [selected, setSelected] = useState('PA_V2')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [polling, setPolling] = useState(null)

  useEffect(() => {
    dataApi.strategies().then(r => setStrategies(r.data))
    return () => { if (polling) clearInterval(polling) }
  }, [])

  const startBacktest = async () => {
    setResult(null); setRunning(true)
    try {
      await dataApi.runBacktest(selected)
      // 每 3s 轮询结果
      const t = setInterval(async () => {
        const r = await dataApi.backtestResult()
        if (r.data?.status === 'done' || r.data?.status === 'error') {
          setResult(r.data)
          setRunning(false)
          clearInterval(t)
          setPolling(null)
        }
      }, 3000)
      setPolling(t)
    } catch (err) {
      setResult({ status:'error', error: err.message })
      setRunning(false)
    }
  }

  return (
    <div>
      <h1 className="page-title">🧪 策略回测</h1>

      <div className="card" style={{maxWidth:480, marginBottom:24}}>
        <div style={{fontWeight:600, marginBottom:16}}>运行回测</div>
        <div className="form-group">
          <label>选择策略</label>
          <select value={selected} onChange={e => setSelected(e.target.value)}>
            {strategies.map(s => (
              <option key={s.name} value={s.name}>{s.name} ({s.class})</option>
            ))}
          </select>
        </div>
        <button className="btn-primary" onClick={startBacktest} disabled={running}>
          {running ? '⏳ 回测运行中...' : '▶ 开始回测'}
        </button>
        {running && (
          <p style={{marginTop:12, color:'var(--muted)', fontSize:13}}>
            正在下载 3 年历史数据并计算，预计 30-90 秒...
          </p>
        )}
      </div>

      {result && result.status === 'error' && (
        <div className="card" style={{borderColor:'var(--red)'}}>
          <div style={{color:'var(--red)', fontWeight:600, marginBottom:8}}>回测失败</div>
          <code style={{fontSize:12, color:'var(--muted)'}}>{result.error}</code>
        </div>
      )}

      {result && result.status === 'done' && (
        <div className="card">
          <div style={{fontWeight:600, marginBottom:20}}>
            📊 回测结果 — {result.strategy}
          </div>
          <div className="grid-4" style={{marginBottom:20}}>
            {[
              ['初始本金', `${result.initial_capital} U`],
              ['最终余额', `${result.final_balance} U`],
              ['净收益率', <span style={{color: result.roi_pct >= 0 ? 'var(--green)' : 'var(--red)'}}>
                {result.roi_pct >= 0 ? '+' : ''}{result.roi_pct}%
              </span>],
              ['最大回撤', <span style={{color:'var(--yellow)'}}>{result.max_drawdown_pct}%</span>],
            ].map(([k, v]) => (
              <div className="card stat-card" key={k}>
                <div className="label">{k}</div>
                <div className="value" style={{fontSize:20}}>{v}</div>
              </div>
            ))}
          </div>
          <div className="grid-4">
            {[
              ['总交易次数', result.total_trades],
              ['盈利次数',  <span className="tag-green">{result.winning_trades}</span>],
              ['亏损次数',  <span className="tag-red">{result.losing_trades}</span>],
              ['胜率',      <span>{result.win_rate_pct}%</span>],
            ].map(([k, v]) => (
              <div className="card stat-card" key={k}>
                <div className="label">{k}</div>
                <div className="value" style={{fontSize:20}}>{v}</div>
              </div>
            ))}
          </div>
          <div style={{marginTop:16, color:'var(--muted)', fontSize:13}}>
            总手续费支出: {result.total_fees_paid} U
          </div>
        </div>
      )}
    </div>
  )
}
