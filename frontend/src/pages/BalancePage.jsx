import { useState, useEffect } from 'react'
import { dataApi } from '../api'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer
} from 'recharts'
import { TrendingUp, TrendingDown, DollarSign, BarChart2 } from 'lucide-react'

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="tooltip-box">
      <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: 4 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 14 }}>
        ${parseFloat(payload[0].value).toLocaleString(undefined, { minimumFractionDigits: 2 })}
      </div>
    </div>
  )
}

export default function BalancePage() {
  const [data, setData] = useState([])

  useEffect(() => {
    dataApi.balance(90).then(r => setData(r.data.reverse()))
  }, [])

  const latest  = data[data.length - 1]?.balance ?? 0
  const first   = data[0]?.balance ?? 0
  const change  = first > 0 ? ((latest - first) / first * 100) : 0
  const absChg  = latest - first
  const isUp    = change >= 0
  const maxBal  = data.length ? Math.max(...data.map(d => d.balance)) : 0
  const minBal  = data.length ? Math.min(...data.map(d => d.balance)) : 0

  // 回撤
  let maxDD = 0
  let peak  = 0
  for (const d of data) {
    if (d.balance > peak) peak = d.balance
    const dd = peak > 0 ? (peak - d.balance) / peak * 100 : 0
    if (dd > maxDD) maxDD = dd
  }

  const chartColor = isUp ? '#26a69a' : '#ef5350'

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">资产曲线</div>
          <div className="page-sub">近 90 日账户余额走势</div>
        </div>
      </div>

      {/* 统计卡片 */}
      <div className="stat-grid stat-grid-4 mb-20">
        <div className="stat-cell">
          <div className="s-label" style={{ display:'flex', alignItems:'center', gap:4 }}>
            <DollarSign size={11} /> 当前余额
          </div>
          <div className="s-value">${latest.toLocaleString(undefined,{minimumFractionDigits:2})}</div>
          <div className="s-sub">USDT</div>
        </div>
        <div className="stat-cell">
          <div className="s-label" style={{ display:'flex', alignItems:'center', gap:4 }}>
            {isUp ? <TrendingUp size={11} /> : <TrendingDown size={11} />} 90日涨跌
          </div>
          <div className="s-value" style={{ color: isUp ? 'var(--green)' : 'var(--red)' }}>
            {isUp ? '+' : ''}{change.toFixed(2)}%
          </div>
          <div className="s-sub" style={{ color: isUp ? 'var(--green)' : 'var(--red)' }}>
            {isUp ? '+' : ''}{absChg.toFixed(2)} U
          </div>
        </div>
        <div className="stat-cell">
          <div className="s-label" style={{ display:'flex', alignItems:'center', gap:4 }}>
            <BarChart2 size={11} /> 最大回撤
          </div>
          <div className="s-value" style={{ color: maxDD > 0 ? 'var(--red)' : 'var(--muted)' }}>
            -{maxDD.toFixed(2)}%
          </div>
          <div className="s-sub">区间最大</div>
        </div>
        <div className="stat-cell">
          <div className="s-label">区间高/低</div>
          <div style={{ marginTop: 4 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--green)' }}>
              ${maxBal.toLocaleString(undefined,{minimumFractionDigits:2})}
            </div>
            <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--red)', marginTop: 2 }}>
              ${minBal.toLocaleString(undefined,{minimumFractionDigits:2})}
            </div>
          </div>
        </div>
      </div>

      {/* 面积图 */}
      {data.length > 1 ? (
        <div className="card mb-20">
          <div className="card-header">余额曲线</div>
          <ResponsiveContainer width="100%" height={240}>
            <AreaChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
              <defs>
                <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={chartColor} stopOpacity={0.25} />
                  <stop offset="95%" stopColor={chartColor} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="date"
                tick={{ fill: 'var(--muted)', fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: 'var(--border)' }}
                interval={Math.floor(data.length / 6)}
              />
              <YAxis
                tick={{ fill: 'var(--muted)', fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={v => `$${v.toLocaleString()}`}
                width={72}
              />
              <Tooltip content={<CustomTooltip />} />
              <Area
                type="monotone"
                dataKey="balance"
                stroke={chartColor}
                strokeWidth={2}
                fill="url(#balGrad)"
                dot={false}
                activeDot={{ r: 4, fill: chartColor }}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="card mb-20">
          <div className="empty-state">
            <div className="empty-icon">📊</div>
            <div className="empty-text">暂无数据，Bot 运行后将自动记录每日余额</div>
          </div>
        </div>
      )}

      {/* 明细表格 */}
      <div className="card">
        <div className="card-header">每日明细</div>
        {data.length === 0 ? (
          <div className="empty-state"><div className="empty-text">暂无记录</div></div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>日期</th>
                <th className="text-right">余额 (USDT)</th>
                <th className="text-right">日变化</th>
                <th className="text-right">日涨跌 %</th>
              </tr>
            </thead>
            <tbody>
              {[...data].reverse().map((row, i, arr) => {
                const prev    = arr[i + 1]?.balance
                const diff    = prev != null ? row.balance - prev : null
                const diffPct = prev > 0 ? (row.balance - prev) / prev * 100 : null
                return (
                  <tr key={row.date}>
                    <td className="col-muted">{row.date}</td>
                    <td className="text-right fw-600">
                      ${row.balance.toLocaleString(undefined,{minimumFractionDigits:2})}
                    </td>
                    <td className={`text-right ${diff == null ? '' : diff >= 0 ? 'col-green' : 'col-red'}`}>
                      {diff == null ? '—' : `${diff >= 0 ? '+' : ''}${diff.toFixed(2)}`}
                    </td>
                    <td className={`text-right ${diffPct == null ? '' : diffPct >= 0 ? 'col-green' : 'col-red'}`}>
                      {diffPct == null ? '—' : `${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(2)}%`}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
