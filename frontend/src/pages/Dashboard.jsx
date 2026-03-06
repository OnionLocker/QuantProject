import { useState, useEffect } from 'react'
import { botApi } from '../api'

export default function Dashboard({ username }) {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(false)
  const [wsData, setWsData] = useState(null)

  const fetch = async () => {
    try {
      const r = await botApi.status()
      setStatus(r.data)
    } catch {}
  }

  useEffect(() => {
    fetch()
    const t = setInterval(fetch, 8000)

    // WebSocket 实时状态
    const token = localStorage.getItem('token')
    const wsBase = (import.meta.env.VITE_API_BASE || window.location.origin)
      .replace(/^http/, 'ws')
    const ws = new WebSocket(`${wsBase}/ws/status?token=${token}`)
    ws.onmessage = e => setWsData(JSON.parse(e.data))
    return () => { clearInterval(t); ws.close() }
  }, [])

  const handleStart = async () => {
    setLoading(true)
    try { await botApi.start(); await fetch() } finally { setLoading(false) }
  }

  const handleStop = async () => {
    setLoading(true)
    try { await botApi.stop(); await fetch() } finally { setLoading(false) }
  }

  const handleResume = async () => {
    await botApi.resume(); await fetch()
  }

  const data = wsData || status?.bot || {}
  const pos = status?.position || {}
  const running = data.running
  const fused   = data.fused

  return (
    <div>
      <div className="flex items-center justify-between" style={{marginBottom:24}}>
        <h1 className="page-title" style={{margin:0}}>控制台</h1>
        <div className="flex gap-8">
          {fused && (
            <button className="btn-warning" onClick={handleResume}>⚡ 恢复熔断</button>
          )}
          {running
            ? <button className="btn-danger" onClick={handleStop} disabled={loading}>🛑 停止 Bot</button>
            : <button className="btn-success" onClick={handleStart} disabled={loading}>🚀 启动 Bot</button>
          }
        </div>
      </div>

      {/* 状态卡片 */}
      <div className="grid-4">
        <div className="card stat-card">
          <div className="label">Bot 状态</div>
          <div className="value" style={{fontSize:16, marginTop:4}}>
            {running
              ? <span className="badge badge-green">运行中</span>
              : <span className="badge badge-gray">已停止</span>}
            {fused && <span className="badge badge-red" style={{marginLeft:6}}>熔断</span>}
          </div>
        </div>
        <div className="card stat-card">
          <div className="label">持仓方向</div>
          <div className="value">
            {pos.side === 'long'  && <span className="tag-green">多头 ↑</span>}
            {pos.side === 'short' && <span className="tag-red">空头 ↓</span>}
            {!pos.side && <span className="tag-muted">空仓</span>}
          </div>
        </div>
        <div className="card stat-card">
          <div className="label">持仓张数</div>
          <div className="value">{pos.amount || 0}</div>
        </div>
        <div className="card stat-card">
          <div className="label">连续亏损</div>
          <div className="value" style={{color: (data.consecutive_losses||0) > 1 ? 'var(--red)' : 'inherit'}}>
            {data.consecutive_losses || 0} 次
          </div>
        </div>
      </div>

      {/* 当前持仓详情 */}
      {pos.side && (
        <div className="card mt-24">
          <div style={{fontWeight:600, marginBottom:16}}>📊 当前持仓</div>
          <div className="grid-3" style={{gap:12}}>
            {[
              ['策略', pos.strategy],
              ['开仓价', pos.entry_price?.toFixed(2)],
              ['开仓时间', pos.entry_time],
              ['止损价', <span className="tag-red">{pos.active_sl?.toFixed(2)}</span>],
              ['止盈价', <span className="tag-green">{pos.active_tp1?.toFixed(2)}</span>],
              ['信号原因', pos.reason],
            ].map(([k, v]) => (
              <div key={k}>
                <div style={{color:'var(--muted)', fontSize:12, marginBottom:2}}>{k}</div>
                <div style={{fontWeight:500}}>{v || '-'}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Bot 错误信息 */}
      {data.last_error && (
        <div className="card mt-24" style={{borderColor:'var(--red)'}}>
          <div style={{color:'var(--red)', fontWeight:600, marginBottom:8}}>⚠️ 最近一次异常</div>
          <code style={{fontSize:12, color:'var(--muted)'}}>{data.last_error}</code>
        </div>
      )}
    </div>
  )
}
