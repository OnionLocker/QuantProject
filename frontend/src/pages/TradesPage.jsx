import { useState, useEffect } from 'react'
import { dataApi } from '../api'
import { ArrowUpRight, ArrowDownRight, Minus, RefreshCw, AlertTriangle, CheckCircle, Clock } from 'lucide-react'

const PAGE_SIZE = 20

export default function TradesPage() {
  const [trades,  setTrades]  = useState([])
  const [page,    setPage]    = useState(1)
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try { const r = await dataApi.trades(200); setTrades(r.data) }
    catch (err) { setError(err.response?.data?.detail || '加载失败，请检查网络') }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  // 统计（只统计已对账的真实盈亏）
  const closedTrades = trades.filter(t => t.pnl !== 0 || t.action === 'closed' || t.action === '平仓' || t.action === '被动平仓(SL/TP)')
  const reconciledTrades = closedTrades.filter(t => !t.is_estimated)
  const estimatedTrades = closedTrades.filter(t => t.is_estimated)
  const wins   = reconciledTrades.filter(t => t.pnl > 0).length
  const losses = reconciledTrades.filter(t => t.pnl < 0).length
  const totalPnl = reconciledTrades.reduce((s, t) => s + (t.pnl || 0), 0)
  const winRate  = reconciledTrades.length > 0 ? wins / reconciledTrades.length * 100 : 0

  // 分页
  const totalPages = Math.ceil(trades.length / PAGE_SIZE)
  const paged      = trades.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  // 退出原因颜色映射
  const exitReasonStyle = (reason) => {
    if (!reason) return { color: 'var(--muted)', text: '—' }
    if (reason.includes('止盈')) return { color: 'var(--green)', text: `🎉 ${reason}` }
    if (reason.includes('止损')) return { color: 'var(--red)', text: `🩸 ${reason}` }
    if (reason.includes('追踪')) return { color: 'var(--yellow)', text: `📈 ${reason}` }
    if (reason.includes('保本')) return { color: 'var(--blue-light)', text: `🔒 ${reason}` }
    if (reason.includes('策略反转')) return { color: 'var(--purple)', text: `↩ ${reason}` }
    if (reason.includes('时间')) return { color: 'var(--muted)', text: `⏱️ ${reason}` }
    if (reason.includes('Regime')) return { color: 'var(--yellow)', text: `🔄 ${reason}` }
    if (reason.includes('待确认')) return { color: 'var(--muted)', text: `❓ ${reason}` }
    return { color: 'var(--muted)', text: reason }
  }

  // 对账状态标记
  const reconcileStatus = (t) => {
    if (t.action === 'open' || t.action === '开仓') return null // 开仓记录无需对账
    if (t.is_estimated && !t.reconciled) {
      return (
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 3,
          fontSize: 10, padding: '1px 6px', borderRadius: 3,
          background: 'rgba(255, 165, 0, 0.15)', color: '#ffa500',
          fontWeight: 600, whiteSpace: 'nowrap',
        }}>
          <AlertTriangle size={9} />估算·待对账
        </span>
      )
    }
    if (t.reconciled && t.estimated_pnl != null && t.estimated_pnl !== t.pnl) {
      return (
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 3,
          fontSize: 10, padding: '1px 6px', borderRadius: 3,
          background: 'rgba(38, 166, 154, 0.15)', color: 'var(--green)',
          fontWeight: 600, whiteSpace: 'nowrap',
        }}>
          <CheckCircle size={9} />已对账
        </span>
      )
    }
    return null
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">交易记录</div>
          <div className="page-sub">共 {trades.length} 条记录</div>
        </div>
        <button className="btn-ghost btn-sm" onClick={load} disabled={loading}>
          {loading ? <span className="spinner" /> : <RefreshCw size={13} />}
        </button>
      </div>

      {/* 首次加载骨架屏 */}
      {loading && trades.length === 0 && !error && (
        <div className="page-skeleton">
          <div className="skeleton-row">
            {[1,2,3,4].map(i => <div key={i} className="skeleton skeleton-cell" />)}
          </div>
          <div className="card" style={{ padding: 0 }}>
            {[1,2,3,4,5].map(i => (
              <div key={i} style={{ display: 'flex', gap: 12, padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
                <div className="skeleton skeleton-text" style={{ width: '15%' }} />
                <div className="skeleton skeleton-text" style={{ width: '8%' }} />
                <div className="skeleton skeleton-text" style={{ width: '10%' }} />
                <div className="skeleton skeleton-text" style={{ width: '12%' }} />
                <div className="skeleton skeleton-text" style={{ width: '10%' }} />
                <div className="skeleton skeleton-text" style={{ width: '10%' }} />
                <div className="skeleton skeleton-text" style={{ width: '20%' }} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 加载错误 */}
      {error && (
        <div className="alert alert-danger mb-16">
          {error}
          <button className="btn-ghost btn-sm" onClick={load} style={{ marginLeft: 12 }}>重试</button>
        </div>
      )}

      {/* 数据就绪：统计 + 表格 */}
      {!loading && !error && (
        <>
          {/* 统计行 */}
          <div className="stat-grid stat-grid-4 mb-20">
            <div className="stat-cell">
              <div className="s-label">真实总盈亏</div>
              <div className="s-value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} U
              </div>
              <div className="s-sub">仅统计已对账交易</div>
            </div>
            <div className="stat-cell">
              <div className="s-label">胜率</div>
              <div className="s-value" style={{ color: winRate >= 50 ? 'var(--green)' : 'var(--red)' }}>
                {winRate.toFixed(1)}%
              </div>
              <div className="s-sub">{wins}W / {losses}L</div>
            </div>
            <div className="stat-cell">
              <div className="s-label">已对账 / 总平仓</div>
              <div className="s-value">{reconciledTrades.length} / {closedTrades.length}</div>
              <div className="s-sub">已平仓</div>
            </div>
            <div className="stat-cell">
              <div className="s-label">待对账</div>
              <div className="s-value" style={{ color: estimatedTrades.length > 0 ? '#ffa500' : 'var(--green)' }}>
                {estimatedTrades.length}
              </div>
              <div className="s-sub" style={{ color: estimatedTrades.length > 0 ? '#ffa500' : 'var(--muted)' }}>
                {estimatedTrades.length > 0 ? '⚠️ 盈亏为估算值' : '✅ 全部已对账'}
              </div>
            </div>
          </div>

          {/* 待对账提醒 */}
          {estimatedTrades.length > 0 && (
            <div className="alert alert-warning mb-16" style={{
              background: 'rgba(255, 165, 0, 0.08)',
              border: '1px solid rgba(255, 165, 0, 0.2)',
              borderRadius: 8, padding: '12px 16px',
              display: 'flex', alignItems: 'flex-start', gap: 10,
            }}>
              <AlertTriangle size={16} style={{ color: '#ffa500', flexShrink: 0, marginTop: 2 }} />
              <div style={{ fontSize: 12 }}>
                <strong style={{ color: '#ffa500' }}>
                  {estimatedTrades.length} 笔交易待对账
                </strong>
                <div style={{ color: 'var(--muted)', marginTop: 4, lineHeight: 1.5 }}>
                  标有「估算·待对账」的交易暂未拿到交易所真实成交结果，
                  显示的盈亏为估算值，仅供参考。系统会自动重查并回填真实结果。
                  <br/>
                  <strong>总盈亏仅统计已对账交易</strong>，不含估算值。
                </div>
              </div>
            </div>
          )}

          {/* 表格 */}
          <div className="card">
            {trades.length === 0 ? (
              <div className="empty-state">
                <div className="empty-icon">📋</div>
                <div className="empty-text">暂无交易记录，启动 Bot 后将自动记录</div>
              </div>
            ) : (
              <>
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>方向</th>
                      <th>操作</th>
                      <th className="text-right">价格</th>
                      <th className="text-right">数量</th>
                      <th className="text-right">盈亏 (U)</th>
                      <th>退出原因</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paged.map(t => {
                      const hasPnl = t.pnl !== 0
                      const isWin  = t.pnl > 0
                      const isEstimated = t.is_estimated
                      const exitStyle = exitReasonStyle(t.exit_reason)
                      const statusBadge = reconcileStatus(t)
                      return (
                        <tr key={t.id} style={{
                          background: isEstimated
                            ? 'rgba(255, 165, 0, 0.03)'
                            : hasPnl
                              ? isWin ? 'rgba(38,166,154,.04)' : 'rgba(239,83,80,.04)'
                              : undefined
                        }}>
                          <td className="col-muted" style={{ whiteSpace: 'nowrap' }}>{t.timestamp}</td>
                          <td>
                            {t.side === 'buy'
                              ? <span style={{ color:'var(--green)', display:'flex', alignItems:'center', gap:3, fontSize:11, fontWeight:700 }}>
                                  <ArrowUpRight size={12} />BUY
                                </span>
                              : <span style={{ color:'var(--red)', display:'flex', alignItems:'center', gap:3, fontSize:11, fontWeight:700 }}>
                                  <ArrowDownRight size={12} />SELL
                                </span>
                            }
                          </td>
                          <td>
                            <span className={`badge ${
                              t.action === 'open' || t.action === '开仓' ? 'badge-blue' :
                              t.action === 'closed' || t.action === '平仓' ? 'badge-gray' :
                              (t.action || '').includes('SL') || (t.action || '').includes('TP') ? 'badge-yellow' : 'badge-gray'
                            }`} style={{ fontSize: 10 }}>
                              {t.action === 'open' ? '开仓' :
                               t.action === 'closed' ? '平仓' :
                               t.action}
                            </span>
                          </td>
                          <td className="text-right fw-600" style={{
                            opacity: isEstimated ? 0.7 : 1,
                          }}>
                            {isEstimated && hasPnl ? '~' : ''}
                            {parseFloat(t.price).toLocaleString(undefined,{minimumFractionDigits:2})}
                          </td>
                          <td className="text-right col-muted">{t.amount}</td>
                          <td className={`text-right fw-600`} style={{
                            color: isEstimated
                              ? '#ffa500'
                              : hasPnl
                                ? (isWin ? 'var(--green)' : 'var(--red)')
                                : 'var(--muted)',
                            fontStyle: isEstimated ? 'italic' : 'normal',
                          }}>
                            {hasPnl
                              ? <>
                                  {isEstimated ? '~' : ''}
                                  {isWin ? '+' : ''}{parseFloat(t.pnl).toFixed(2)}
                                  {/* 如果已对账且有估算值差异，显示 */}
                                  {t.reconciled && t.estimated_pnl != null && t.estimated_pnl !== t.pnl && (
                                    <span style={{
                                      display: 'block', fontSize: 9, color: 'var(--muted)',
                                      fontStyle: 'normal', fontWeight: 400,
                                    }}>
                                      原估算: {t.estimated_pnl > 0 ? '+' : ''}{parseFloat(t.estimated_pnl).toFixed(2)}
                                    </span>
                                  )}
                                </>
                              : '—'
                            }
                          </td>
                          <td style={{
                            maxWidth: 160, overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            fontSize: 11, color: exitStyle.color,
                          }}>
                            {exitStyle.text}
                          </td>
                          <td>{statusBadge}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>

                {/* 分页 */}
                {totalPages > 1 && (
                  <div style={{ display:'flex', justifyContent:'flex-end', alignItems:'center', gap:8, marginTop:14 }}>
                    <span className="fs-12 text-muted">{page} / {totalPages}</span>
                    <div className="pagination">
                      <div
                        className={`page-btn${page <= 1 ? ' active' : ''}`}
                        onClick={() => setPage(p => Math.max(1, p - 1))}
                        style={{ cursor: page <= 1 ? 'default' : 'pointer', opacity: page <= 1 ? .4 : 1 }}
                      >‹</div>
                      {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                        const n = i + 1
                        return (
                          <div
                            key={n}
                            className={`page-btn${page === n ? ' active' : ''}`}
                            onClick={() => setPage(n)}
                          >{n}</div>
                        )
                      })}
                      <div
                        className="page-btn"
                        onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                        style={{ cursor: page >= totalPages ? 'default' : 'pointer', opacity: page >= totalPages ? .4 : 1 }}
                      >›</div>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}
