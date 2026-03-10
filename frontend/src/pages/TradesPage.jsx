import { useState, useEffect } from 'react'
import { dataApi } from '../api'
import { ArrowUpRight, ArrowDownRight, Minus, RefreshCw } from 'lucide-react'

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

  // 统计
  const closedTrades = trades.filter(t => t.pnl !== 0 || t.action === '平仓' || t.action === '被动平仓(SL/TP)')
  const wins   = closedTrades.filter(t => t.pnl > 0).length
  const losses = closedTrades.filter(t => t.pnl < 0).length
  const totalPnl = closedTrades.reduce((s, t) => s + (t.pnl || 0), 0)
  const winRate  = closedTrades.length > 0 ? wins / closedTrades.length * 100 : 0

  // 分页
  const totalPages = Math.ceil(trades.length / PAGE_SIZE)
  const paged      = trades.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

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
              <div className="s-label">总盈亏</div>
              <div className="s-value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} U
              </div>
            </div>
            <div className="stat-cell">
              <div className="s-label">胜率</div>
              <div className="s-value" style={{ color: winRate >= 50 ? 'var(--green)' : 'var(--red)' }}>
                {winRate.toFixed(1)}%
              </div>
              <div className="s-sub">{wins}W / {losses}L</div>
            </div>
            <div className="stat-cell">
              <div className="s-label">总笔数</div>
              <div className="s-value">{closedTrades.length}</div>
              <div className="s-sub">已平仓</div>
            </div>
            <div className="stat-cell">
              <div className="s-label">记录总数</div>
              <div className="s-value">{trades.length}</div>
              <div className="s-sub">含开/平仓</div>
            </div>
          </div>

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
                      <th>原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paged.map(t => {
                      const hasPnl = t.pnl !== 0
                      const isWin  = t.pnl > 0
                      return (
                        <tr key={t.id} style={{
                          background: hasPnl
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
                              t.action === '开仓' ? 'badge-blue' :
                              t.action === '平仓' ? 'badge-gray' :
                              t.action.includes('SL') || t.action.includes('TP') ? 'badge-yellow' : 'badge-gray'
                            }`} style={{ fontSize: 10 }}>
                              {t.action}
                            </span>
                          </td>
                          <td className="text-right fw-600">
                            {parseFloat(t.price).toLocaleString(undefined,{minimumFractionDigits:2})}
                          </td>
                          <td className="text-right col-muted">{t.amount}</td>
                          <td className={`text-right fw-600 ${hasPnl ? (isWin ? 'col-green' : 'col-red') : 'col-muted'}`}>
                            {hasPnl
                              ? `${isWin ? '+' : ''}${parseFloat(t.pnl).toFixed(2)}`
                              : '—'
                            }
                          </td>
                          <td className="col-muted" style={{
                            maxWidth: 200, overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            fontSize: 11,
                          }}>
                            {t.reason || '—'}
                          </td>
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
