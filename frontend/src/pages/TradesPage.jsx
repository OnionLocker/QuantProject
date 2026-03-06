import { useState, useEffect } from 'react'
import { dataApi } from '../api'

export default function TradesPage() {
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    dataApi.trades(100).then(r => { setTrades(r.data); setLoading(false) })
  }, [])

  const totalPnl = trades.filter(t => t.action === '平仓').reduce((s, t) => s + t.pnl, 0)
  const wins     = trades.filter(t => t.action === '平仓' && t.pnl > 0).length
  const closes   = trades.filter(t => t.action === '平仓').length

  return (
    <div>
      <h1 className="page-title">📋 交易记录</h1>

      <div className="grid-3" style={{marginBottom:24}}>
        <div className="card stat-card">
          <div className="label">总盈亏 (U)</div>
          <div className="value" style={{color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)'}}>
            {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)}
          </div>
        </div>
        <div className="card stat-card">
          <div className="label">胜率</div>
          <div className="value">{closes > 0 ? ((wins/closes)*100).toFixed(1) : 0}%</div>
        </div>
        <div className="card stat-card">
          <div className="label">总交易次数</div>
          <div className="value">{closes}</div>
        </div>
      </div>

      <div className="card">
        {loading
          ? <p className="tag-muted">加载中...</p>
          : trades.length === 0
            ? <p className="tag-muted">暂无交易记录</p>
            : (
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>品种</th>
                    <th>方向</th>
                    <th>操作</th>
                    <th>价格</th>
                    <th>数量</th>
                    <th>盈亏</th>
                    <th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map(t => (
                    <tr key={t.id}>
                      <td className="tag-muted">{t.timestamp}</td>
                      <td>{t.symbol}</td>
                      <td>
                        <span className={t.side === 'buy' ? 'tag-green' : 'tag-red'}>
                          {t.side === 'buy' ? '买入' : '卖出'}
                        </span>
                      </td>
                      <td>{t.action}</td>
                      <td>{t.price?.toFixed(2)}</td>
                      <td>{t.amount}</td>
                      <td>
                        {t.action === '平仓'
                          ? <span className={t.pnl >= 0 ? 'tag-green' : 'tag-red'}>
                              {t.pnl >= 0 ? '+' : ''}{t.pnl?.toFixed(2)}
                            </span>
                          : '-'}
                      </td>
                      <td style={{maxWidth:160, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', color:'var(--muted)'}}>
                        {t.reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
        }
      </div>
    </div>
  )
}
