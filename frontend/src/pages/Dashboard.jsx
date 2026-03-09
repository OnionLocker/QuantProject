import { useState, useEffect, useRef } from 'react'
import { botApi } from '../api'
import {
  Play, Square, Zap, TrendingUp, TrendingDown,
  Minus, AlertTriangle, RefreshCw, Clock, Activity,
  DollarSign, BarChart2, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'

export default function Dashboard({ username }) {
  const [status,  setStatus]  = useState(null)
  const [loading, setLoading] = useState(false)
  const [wsData,  setWsData]  = useState(null)
  const [wsOk,    setWsOk]    = useState(false)
  const wsRef = useRef(null)

  const fetchStatus = async () => {
    try { const r = await botApi.status(); setStatus(r.data) } catch {}
  }

  useEffect(() => {
    fetchStatus()
    const t = setInterval(fetchStatus, 15000)

    const token  = localStorage.getItem('token')
    const wsBase = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/^http/, 'ws')
    const ws     = new WebSocket(`${wsBase}/ws/status?token=${token}`)
    wsRef.current = ws
    ws.onopen    = () => setWsOk(true)
    ws.onclose   = () => setWsOk(false)
    ws.onerror   = () => setWsOk(false)
    ws.onmessage = e => {
      try { setWsData(JSON.parse(e.data)) } catch {}
    }

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

  // WS 数据优先，fallback 到轮询
  const d       = wsData || {}
  const bot     = status?.bot   || {}
  const pos     = status?.position || {}

  const running  = d.running  ?? bot.running  ?? false
  const fused    = d.fused    ?? bot.fused    ?? false
  const posAmt   = d.position_amount ?? pos.amount ?? 0
  const posSide  = d.position_side   ?? pos.side   ?? null
  const entryPx  = d.entry_price     ?? pos.entry_price ?? 0
  const activeSl = d.active_sl       ?? pos.active_sl   ?? 0
  const activeTp = d.active_tp1      ?? pos.active_tp1  ?? 0
  const entryTime= d.entry_time      ?? pos.entry_time  ?? ''
  const stratName= d.strategy_name   ?? pos.strategy    ?? ''
  const sigReason= d.signal_reason   ?? pos.reason      ?? ''
  const curPrice = d.current_price   ?? null
  const unrealPnl= d.unrealized_pnl  ?? null
  const todayPnl = d.today_pnl       ?? null
  const todayTrd = d.today_trades    ?? null
  const uptime   = d.uptime          ?? ''
  const consLoss = d.consecutive_losses ?? bot.consecutive_losses ?? 0
  const hasPos   = posAmt > 0 && posSide && posSide !== 'unknown_rollback_failed'

  const pnlColor = (v) => v == null ? 'var(--muted)' : v >= 0 ? 'var(--green)' : 'var(--red)'
  const fmt      = (v, dec=2) => v == null ? '—' : Number(v).toFixed(dec)

  return (
    <div>
      {/* ── 顶部操作栏 ── */}
      <div className="page-header">
        <div>
          <div className="page-title">控制台</div>
          <div className="page-sub" style={{ display:'flex', alignItems:'center', gap:6 }}>
            实时监控 · 一键启停
            <span style={{
              display:'inline-flex', alignItems:'center', gap:4,
              fontSize:10, color: wsOk ? 'var(--green)' : 'var(--muted)',
            }}>
              <span style={{
                width:5, height:5, borderRadius:'50%',
                background: wsOk ? 'var(--green)' : 'var(--muted2)',
                display:'inline-block',
              }} />
              {wsOk ? 'WS 实时' : '轮询'}
            </span>
          </div>
        </div>
        <div className="flex gap-8">
          {fused && (
            <button className="btn-warning btn-sm" onClick={handleResume} disabled={loading}>
              <Zap size={12} style={{ marginRight:4 }} />恢复熔断
            </button>
          )}
          <button className="btn-ghost btn-sm" onClick={fetchStatus} title="刷新">
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

      {/* ── 第一行：Bot 状态 4 格 ── */}
      <div className="stat-grid stat-grid-4 mb-1">
        <div className="stat-cell">
          <div className="s-label">Bot 状态</div>
          <div style={{ marginTop:6 }}>
            {running
              ? <span className="badge badge-green"><span className="dot dot-green" />运行中</span>
              : fused
                ? <span className="badge badge-yellow"><AlertTriangle size={10} />熔断</span>
                : <span className="badge badge-gray"><span className="dot dot-gray" />已停止</span>
            }
          </div>
          {running && uptime && (
            <div className="s-sub" style={{ marginTop:5 }}>
              <Clock size={10} style={{marginRight:3,verticalAlign:'middle'}} />{uptime}
            </div>
          )}
        </div>

        <div className="stat-cell">
          <div className="s-label">连续亏损</div>
          <div className="s-value" style={{
            marginTop:6,
            color: consLoss >= 2 ? 'var(--red)' : consLoss >= 1 ? 'var(--yellow)' : 'var(--green)',
          }}>
            {consLoss} 次
          </div>
          <div className="s-sub" style={{ marginTop:5 }}>
            上限 {bot.crash_count !== undefined ? `· 崩溃${bot.crash_count}次` : ''}
          </div>
        </div>

        <div className="stat-cell">
          <div className="s-label">今日交易</div>
          <div className="s-value" style={{ marginTop:6 }}>
            {todayTrd ?? '—'} 笔
          </div>
          <div className="s-sub" style={{ marginTop:5, color: pnlColor(todayPnl) }}>
            {todayPnl != null ? `${todayPnl >= 0 ? '+' : ''}${fmt(todayPnl)} U` : '—'}
          </div>
        </div>

        <div className="stat-cell">
          <div className="s-label">当前价格</div>
          <div className="s-value" style={{ marginTop:6 }}>
            {curPrice ? `$${curPrice.toLocaleString()}` : <span style={{color:'var(--muted2)'}}>—</span>}
          </div>
          <div className="s-sub" style={{ marginTop:5 }}>
            {wsOk ? '5s 刷新' : ''}
          </div>
        </div>
      </div>

      {/* ── 第二行：持仓信息 4 格 ── */}
      <div className="stat-grid stat-grid-4 mb-16">
        <div className="stat-cell">
          <div className="s-label">持仓方向</div>
          <div style={{ marginTop:6 }}>
            {posSide === 'long'
              ? <span style={{ color:'var(--green)', display:'flex', alignItems:'center', gap:6 }}>
                  <TrendingUp size={16} /> 做多
                </span>
              : posSide === 'short'
                ? <span style={{ color:'var(--red)', display:'flex', alignItems:'center', gap:6 }}>
                    <TrendingDown size={16} /> 做空
                  </span>
                : <span style={{ color:'var(--muted)', display:'flex', alignItems:'center', gap:6 }}>
                    <Minus size={16} /> 空仓
                  </span>
            }
          </div>
          {hasPos && <div className="s-sub" style={{ marginTop:5 }}>{posAmt} 张</div>}
        </div>

        <div className="stat-cell">
          <div className="s-label">入场价格</div>
          <div className="s-value" style={{ marginTop:6 }}>
            {hasPos ? `$${entryPx.toLocaleString()}` : <span style={{color:'var(--muted2)'}}>—</span>}
          </div>
          {hasPos && entryTime && (
            <div className="s-sub" style={{ marginTop:5 }}>{entryTime.slice(0,16)}</div>
          )}
        </div>

        <div className="stat-cell">
          <div className="s-label">浮动盈亏</div>
          <div className="s-value" style={{ marginTop:6, color: pnlColor(unrealPnl), fontWeight:700 }}>
            {unrealPnl != null
              ? <span style={{ display:'flex', alignItems:'center', gap:4 }}>
                  {unrealPnl >= 0
                    ? <ArrowUpRight size={14} color="var(--green)" />
                    : <ArrowDownRight size={14} color="var(--red)" />}
                  {unrealPnl >= 0 ? '+' : ''}{fmt(unrealPnl)} U
                </span>
              : <span style={{color:'var(--muted2)'}}>空仓</span>
            }
          </div>
          {hasPos && curPrice && entryPx > 0 && (
            <div className="s-sub" style={{ marginTop:5 }}>
              {(((curPrice - entryPx) / entryPx) * (posSide === 'long' ? 1 : -1) * 100).toFixed(2)}%
            </div>
          )}
        </div>

        <div className="stat-cell">
          <div className="s-label">策略</div>
          <div className="s-value" style={{ marginTop:6, fontSize:13, color:'var(--blue-light)' }}>
            {stratName || <span style={{color:'var(--muted2)'}}>—</span>}
          </div>
          {sigReason && (
            <div className="s-sub" style={{ marginTop:5, maxWidth:140, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {sigReason}
            </div>
          )}
        </div>
      </div>

      {/* ── 持仓详情卡片 ── */}
      {hasPos && (
        <div className="card mb-16">
          <div className="card-header">当前持仓详情</div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)' }}>
            {[
              { label:'止损价 (SL)', value: activeSl ? `$${activeSl.toLocaleString()}` : '—', color:'var(--red)' },
              { label:'止盈价 (TP1)', value: activeTp ? `$${activeTp.toLocaleString()}` : '—', color:'var(--green)' },
              { label:'预期风险',
                value: activeSl && entryPx && posAmt
                  ? `~${Math.abs((entryPx - activeSl) * posAmt * 0.01).toFixed(2)} U`
                  : '—',
                color:'var(--yellow)' },
            ].map(({ label, value, color }) => (
              <div key={label} style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
                <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>{label}</div>
                <div style={{ fontSize:15, fontWeight:700, color }}>{value}</div>
              </div>
            ))}
          </div>
          {sigReason && (
            <div style={{ padding:'10px 16px', borderTop:'1px solid var(--border)', fontSize:12, color:'var(--muted)' }}>
              信号原因：<span style={{ color:'var(--text)' }}>{sigReason}</span>
            </div>
          )}
        </div>
      )}

      {/* ── 熔断告警 ── */}
      {fused && (
        <div className="alert alert-danger mb-16">
          <strong><AlertTriangle size={13} style={{verticalAlign:'middle',marginRight:6}} />风控熔断已触发</strong>
          <span style={{ marginLeft:8 }}>
            连续亏损 {consLoss} 次，Bot 已自动暂停。确认风险后点击「恢复熔断」继续运行。
          </span>
        </div>
      )}

      {/* ── 上次错误 ── */}
      {(d.last_error || bot.last_error) && !running && (
        <div className="alert alert-warning">
          <strong>上次退出原因：</strong>{d.last_error || bot.last_error}
        </div>
      )}
    </div>
  )
}
