/**
 * TradeList.jsx
 * 回测交易明细列表
 */
export default function TradeList({ trades = [] }) {
  if (!trades.length) return null

  const completed = trades.filter(t => t.exit_ts)

  return (
    <div className="card mt-16">
      <div className="card-header" style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
        <span>交易明细</span>
        <span style={{ fontSize:11, color:'var(--muted)', fontWeight:400 }}>
          共 {completed.length} 笔已平仓
          {trades.length > completed.length && `（${trades.length - completed.length} 笔未平仓）`}
        </span>
      </div>

      <div style={{ overflowX:'auto' }}>
        <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
          <thead>
            <tr style={{ borderBottom:'1px solid rgba(255,255,255,0.08)' }}>
              {['#','方向','开仓时间','开仓价','SL','TP','平仓时间','平仓价','原因','PnL'].map(h => (
                <th key={h} style={{
                  padding:'8px 10px', textAlign: h === 'PnL' ? 'right' : 'left',
                  color:'var(--muted)', fontWeight:500, whiteSpace:'nowrap',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => {
              const isWin  = t.result === 'win'
              const isLong = t.side === 'long'
              const pnlColor = t.pnl == null ? 'var(--muted)'
                             : t.pnl >= 0    ? 'var(--green)' : 'var(--red)'
              return (
                <tr key={i} style={{
                  borderBottom: i < trades.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                }}>
                  <td style={{ padding:'7px 10px', color:'var(--muted)' }}>{i + 1}</td>
                  <td style={{ padding:'7px 10px' }}>
                    <span style={{
                      padding:'2px 7px', borderRadius:4, fontSize:11, fontWeight:600,
                      background: isLong ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)',
                      color:      isLong ? 'var(--green)' : 'var(--red)',
                    }}>
                      {isLong ? '多' : '空'}
                    </span>
                  </td>
                  <td style={{ padding:'7px 10px', color:'var(--muted)', whiteSpace:'nowrap', fontSize:11 }}>
                    {t.entry_ts?.slice(0,16).replace('T',' ')}
                  </td>
                  <td style={{ padding:'7px 10px', fontWeight:600 }}>{t.entry_price}</td>
                  <td style={{ padding:'7px 10px', color:'var(--red)', fontSize:11 }}>{t.sl}</td>
                  <td style={{ padding:'7px 10px', color:'var(--green)', fontSize:11 }}>{t.tp}</td>
                  <td style={{ padding:'7px 10px', color:'var(--muted)', whiteSpace:'nowrap', fontSize:11 }}>
                    {t.exit_ts ? t.exit_ts.slice(0,16).replace('T',' ') : '—'}
                  </td>
                  <td style={{ padding:'7px 10px' }}>{t.exit_price ?? '—'}</td>
                  <td style={{ padding:'7px 10px', fontSize:11 }}>
                    {t.exit_reason
                      ? <span style={{ color: t.exit_reason === '止盈' ? 'var(--green)' : t.exit_reason === '止损' ? 'var(--red)' : 'var(--muted)' }}>
                          {t.exit_reason === '止盈' ? '🎉 止盈' : t.exit_reason === '止损' ? '🩸 止损' : `↩ ${t.exit_reason}`}
                        </span>
                      : '—'}
                  </td>
                  <td style={{ padding:'7px 10px', textAlign:'right', fontWeight:700, color:pnlColor, whiteSpace:'nowrap' }}>
                    {t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}${t.pnl} U` : '持仓中'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
