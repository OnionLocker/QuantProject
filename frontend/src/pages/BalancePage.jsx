import { useState, useEffect } from 'react'
import { dataApi } from '../api'

export default function BalancePage() {
  const [data, setData] = useState([])

  useEffect(() => {
    dataApi.balance(90).then(r => setData(r.data.reverse()))
  }, [])

  const latest = data[data.length - 1]?.balance || 0
  const first  = data[0]?.balance || 0
  const change = first > 0 ? ((latest - first) / first * 100) : 0

  return (
    <div>
      <h1 className="page-title">💰 资产曲线</h1>

      <div className="grid-2" style={{marginBottom:24}}>
        <div className="card stat-card">
          <div className="label">当前余额 (USDT)</div>
          <div className="value">{latest.toFixed(2)}</div>
        </div>
        <div className="card stat-card">
          <div className="label">90日涨幅</div>
          <div className="value" style={{color: change >= 0 ? 'var(--green)' : 'var(--red)'}}>
            {change >= 0 ? '+' : ''}{change.toFixed(2)}%
          </div>
        </div>
      </div>

      {/* 简易折线图 */}
      {data.length > 1 && (
        <div className="card" style={{marginBottom:24}}>
          <MiniChart data={data} />
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr><th>日期</th><th>余额 (USDT)</th><th>变化</th></tr>
          </thead>
          <tbody>
            {[...data].reverse().map((row, i, arr) => {
              const prev = arr[i + 1]?.balance
              const diff = prev ? row.balance - prev : 0
              return (
                <tr key={row.date}>
                  <td>{row.date}</td>
                  <td>{row.balance.toFixed(2)}</td>
                  <td>
                    {prev
                      ? <span className={diff >= 0 ? 'tag-green' : 'tag-red'}>
                          {diff >= 0 ? '+' : ''}{diff.toFixed(2)}
                        </span>
                      : '-'}
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

function MiniChart({ data }) {
  const W = 700, H = 180, PAD = 20
  const vals = data.map(d => d.balance)
  const min  = Math.min(...vals)
  const max  = Math.max(...vals)
  const range = max - min || 1

  const pts = vals.map((v, i) => {
    const x = PAD + (i / (vals.length - 1)) * (W - PAD * 2)
    const y = PAD + (1 - (v - min) / range) * (H - PAD * 2)
    return [x, y]
  })

  const polyline = pts.map(p => p.join(',')).join(' ')
  const fill = `${pts.map(p => p.join(',')).join(' ')} ${pts[pts.length-1][0]},${H} ${pts[0][0]},${H}`
  const isUp = vals[vals.length - 1] >= vals[0]

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{width:'100%', height:H}}>
      <defs>
        <linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={isUp ? '#3fb950' : '#f85149'} stopOpacity="0.3"/>
          <stop offset="100%" stopColor={isUp ? '#3fb950' : '#f85149'} stopOpacity="0"/>
        </linearGradient>
      </defs>
      <polygon points={fill} fill="url(#grad)" />
      <polyline points={polyline} fill="none"
        stroke={isUp ? '#3fb950' : '#f85149'} strokeWidth="2" />
    </svg>
  )
}
