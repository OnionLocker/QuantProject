import { useState, useEffect, useRef } from 'react'
import { botApi } from '../api'
import {
  Play, Square, Zap, TrendingUp, TrendingDown,
  Minus, AlertTriangle, RefreshCw, Clock
} from 'lucide-react'

export default function Dashboard({ username }) {
  const [status,  setStatus]  = useState(null)
  const [loading, setLoading] = useState(false)
  const [wsData,  setWsData]  = useState(null)
  const wsRef = useRef(null)

  const fetchStatus = async () => {
    try { const r = await botApi.status(); setStatus(r.data) } catch {}
  }

  useEffect(() => {
    fetchStatus()
    const t = setInterval(fetchStatus, 10000)

    const token  = localStorage.getItem('token')
    const wsBase = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/^http/, 'ws')
    const ws     = new WebSocket(`${wsBase}/ws/status?token=${token}`)
    wsRef.current = ws
    ws.onmessage = e => setWsData(JSON.parse(e.data))
    ws.onerror   = () => {}

    return () => { clearInterval(t); ws.close() }
  }, [])

  const handleStart = async () => {
    setLoading(true)
    try { await botApi.start(); await fetchStatus() } finally { setLoading(false) }
  }
  const handleStop = async () => {
    setLoading(true)
    try { await botApi.stop(); await fetchStatus() } finally { setLoading(false) }
  }
  const handleResume = async () => {
    setLoading(true)
    try { await botApi.resume(); await fetchStatus() } finally { setLoading(false) }
  }

  const bot = wsData || status?.bot || {}
  const pos = status?.position || {}
  const running = bot.running
  const fused   = bot.fused
  const hasPos  = pos.side && pos.amount > 0

  // 浮动盈亏计算（前端估算，无实时价格时显示 N/A）
  const unrealizedPnl = null   // 需要实时价格才能算，保留占位

  return (
    <div>
      {/* ── 顶部操作栏 ── */}
      <div className="page-header">
        <div>
          <div className="page-title">控制台</div>
          <div className="page-sub">实时监控 · 一键启停</div>
        </div>
        <div className="flex gap-8">
          {fused && (
            <button className="btn-warning btn-sm" onClick={handleResume} disabled={loading}>
              <Zap size={12} style={{ marginRight: 4 }} />
              恢复熔断
            </button>
          )}
          <button
            className="btn-ghost btn-sm"
            onClick={fetchStatus}
            title="刷新状态"
          >
            <RefreshCw size={13} />
          </button>
          {running
            ? <button className="btn-danger btn-sm" onClick={handleStop} disabled={loading}>
                {loading ? <span className="spinner" /> : <><Square size={12} style={{marginRight:4}}/>停止 Bot</>}
              </button>
            : <button className="btn-success btn-sm" onClick={handleStart} disabled={loading}>
                {loading ? <span className="spinner" /> : <><Play size={12} style={{marginRight:4}}/>启动 Bot</>}
              </button>
          }
        </div>
      </div>

      {/* ── 状态卡片行 ── */}
      <div className="stat-grid stat-grid-4 mb-20">
        {/* Bot 状态 */}
        <div className="stat-cell">
          <div className="s-label">Bot 状态</div>
          <div className="s-value" style={{ fontSize: 14, marginTop: 6 }}>
            {running
              ? <span className="badge badge-green"><span className="dot dot-green" />运行中</span>
              : fused
                ? <span className="badge badge-yellow"><AlertTriangle size={10} />熔断</span>
                : <span className="badge badge-gray"><span className="dot dot-gray" />已停止</span>
            }
          </div>
          {bot.started_at && (
            <div className="s-sub" style={{ marginTop: 6 }}>
              <Clock size={10} style={{ marginRight: 3, verticalAlign: 'middle' }} />
              {bot.started_at}
            </div>
          )}
        </div>

        {/* 持仓方向 */}
        <div className="stat-cell">
          <div className="s-label">持仓方向</div>
          <div className="s-value" style={{ marginTop: 6 }}>
            {pos.side === 'long'
              ? <span style={{ color: 'var(--green)', display:'flex', alignItems:'center', gap:6 }}>
                  <TrendingUp size={18} /> 做多
                </span>
              : pos.side === 'short'
                ? <span style={{ color: 'var(--red)', display:'flex', alignItems:'center', gap:6 }}>
                    <TrendingDown size={18} /> 做空
                  </span>
                : <span style={{ color: 'var(--muted)', display:'flex', alignItems:'center', gap:6 }}>
                    <Minus size={18} /> 空仓
                  </span>
            }
          </div>
          {hasPos && (
            <div className="s-sub" style={{ marginTop: 6 }}>{pos.amount} 张</div>
          )}
        </div>

        {/* 入场价 */}
        <div className="stat-cell">
          <div className="s-label">入场价格</div>
          <div className="s-value" style={{ marginTop: 6 }}>
            {hasPos ? `$${pos.entry_price.toLocaleString()}` : <span style={{ color: 'var(--muted2)' }}>—</span>}
          </div>
          {hasPos && pos.entry_time && (
            <div className="s-sub" style={{ marginTop: 6 }}>{pos.entry_time}</div>
          )}
        </div>

        {/* 连续亏损 */}
        <div className="stat-cell">
          <div className="s-label">连续亏损</div>
          <div className="s-value" style={{
            marginTop: 6,
            color: bot.consecutive_losses > 0 ? 'var(--red)' : 'var(--green)'
          }}>
            {bot.consecutive_losses ?? 0} 次
          </div>
          <div className="s-sub" style={{ marginTop: 6 }}>
            熔断上限 {bot.crash_count !== undefined ? `/ 崩溃${bot.crash_count}次` : ''}
          </div>
        </div>
      </div>

      {/* ── 持仓详情 ── */}
      {hasPos && (
        <div className="card mb-20">
          <div className="card-header">当前持仓</div>
          <div className="stat-grid stat-grid-3" style={{ border: 'none', borderRadius: 0, gap: 0 }}>
            {[
              { label: '止损价 (SL)', value: `$${pos.active_sl?.toLocaleString() ?? '—'}`, color: 'var(--red)' },
              { label: '止盈价 (TP1)', value: `$${pos.active_tp1?.toLocaleString() ?? '—'}`, color: 'var(--green)' },
              { label: '策略信号', value: pos.strategy || '—', color: 'var(--blue-light)' },
            ].map(({ label, value, color }) => (
              <div key={label} style={{ padding: '12px 16px', borderRight: '1px solid var(--border)' }}>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>{label}</div>
                <div style={{ fontSize: 15, fontWeight: 700, color }}>{value}</div>
              </div>
            ))}
          </div>
          {pos.reason && (
            <div style={{
              padding: '10px 16px',
              borderTop: '1px solid var(--border)',
              fontSize: 12, color: 'var(--muted)',
            }}>
              信号原因：<span style={{ color: 'var(--text)' }}>{pos.reason}</span>
            </div>
          )}
        </div>
      )}

      {/* ── 熔断告警 ── */}
      {fused && (
        <div className="alert alert-danger mb-16">
          <strong><AlertTriangle size={13} style={{ verticalAlign:'middle', marginRight:6 }} />风控熔断已触发</strong>
          <span style={{ marginLeft: 8 }}>
            连续亏损 {bot.consecutive_losses} 次，Bot 已自动暂停。
            确认风险后点击「恢复熔断」继续运行。
          </span>
        </div>
      )}

      {/* ── 上次错误 ── */}
      {bot.last_error && !running && (
        <div className="alert alert-warning">
          <strong>上次退出原因：</strong> {bot.last_error}
        </div>
      )}
    </div>
  )
}
