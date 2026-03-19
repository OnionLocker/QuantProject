import { useState, useEffect, useRef } from 'react'
import { botApi, dataApi, marketApi, newsSyncApi } from '../api'
import {
  Play, Square, Zap, TrendingUp, TrendingDown,
  Minus, AlertTriangle, RefreshCw, Clock, Activity,
  DollarSign, BarChart2, ArrowUpRight, ArrowDownRight,
  ChevronDown, Eye, Shield,
} from 'lucide-react'

export default function Dashboard({ username }) {
  const [status,     setStatus]     = useState(null)
  const [loading,    setLoading]    = useState(false)
  const [wsData,     setWsData]     = useState(null)
  const [wsOk,       setWsOk]       = useState(false)
  const [strategies, setStrategies] = useState([])
  const [selStrategy, setSelStrategy] = useState('AUTO')
  const [showStratMenu, setShowStratMenu] = useState(false)
  const [sentiment,  setSentiment]  = useState(null)
  const [newsSync,   setNewsSync]   = useState(null)
  const stratMenuRef = useRef(null)
  const wsRef = useRef(null)

  const fetchStatus = async () => {
    try { const r = await botApi.status(); setStatus(r.data) } catch {}
  }

  // 用于检测 token 变更（重新登录后 WS 应使用新 token）
  const tokenRef = useRef(localStorage.getItem('token'))

  useEffect(() => {
    const currentToken = localStorage.getItem('token')
    tokenRef.current = currentToken

    // 拉取策略列表
    dataApi.strategies().then(r => setStrategies(r.data || [])).catch(() => {})

    // V3.0: 拉取市场情绪数据
    const fetchSentiment = () => {
      marketApi.sentiment().then(r => setSentiment(r.data)).catch(() => {})
      newsSyncApi.status().then(r => setNewsSync(r.data)).catch(() => {})
    }
    fetchSentiment()
    const sentimentTimer = setInterval(fetchSentiment, 60000)  // 每分钟更新

    fetchStatus()
    const t = setInterval(fetchStatus, 15000)

    // ── WebSocket 自动重连（指数退避）──────────────────────────────────────
    let ws = null
    let reconnectTimer = null
    let reconnectDelay = 1000   // 初始 1 秒
    const MAX_DELAY = 30000     // 最大 30 秒
    let stopped = false

    const connectWs = () => {
      if (stopped) return
      const token  = localStorage.getItem('token')
      if (!token) return  // 未登录，不连接
      const wsBase = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/^http/, 'ws')
      ws = new WebSocket(`${wsBase}/ws/status?token=${token}`)
      wsRef.current = ws

      ws.onopen = () => {
        setWsOk(true)
        reconnectDelay = 1000  // 连接成功后重置退避
      }
      ws.onclose = () => {
        setWsOk(false)
        if (!stopped) {
          reconnectTimer = setTimeout(() => {
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_DELAY)
            connectWs()
          }, reconnectDelay)
        }
      }
      ws.onerror = () => {
        setWsOk(false)
        ws.close()  // 触发 onclose 的重连逻辑
      }
      ws.onmessage = e => {
        try { setWsData(JSON.parse(e.data)) } catch {}
      }
    }
    connectWs()

    return () => {
      stopped = true
      clearInterval(t)
      clearInterval(sentimentTimer)
      clearTimeout(reconnectTimer)
      if (ws) ws.close()
    }
  }, [username])

  // 点击外部关闭策略菜单
  useEffect(() => {
    const handler = (e) => {
      if (stratMenuRef.current && !stratMenuRef.current.contains(e.target)) {
        setShowStratMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleStart = async () => {
    setLoading(true)
    try {
      // AUTO 传 null（使用用户配置），其他策略传策略名覆盖
      await botApi.start(selStrategy === 'AUTO' ? null : selStrategy)
      await fetchStatus()
    } finally { setLoading(false) }
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

  // V7.0: 冷静期数据
  const cooldown = status?.cooldown || {}
  const cdActive = d.cooldown_active ?? cooldown.active ?? false
  const cdBarsRemaining = d.cooldown_bars_remaining ?? cooldown.bars_remaining ?? 0
  const lastCloseTime = d.last_close_time ?? cooldown.last_close_time ?? ''
  const lastCloseReason = d.last_close_reason ?? cooldown.last_close_reason ?? ''
  const lastClosePnl = d.last_close_pnl ?? cooldown.last_close_pnl ?? 0
  const lastCloseSide = d.last_close_side ?? cooldown.last_close_side ?? ''
  const spikeCooldownUntil = d.spike_cooldown_until ?? cooldown.spike_cooldown_until ?? ''
  const signalQuality = d.signal_quality ?? regimeDetail.signal_quality ?? null

  // V3.0: Regime 数据
  const regimeDetail = d.regime_detail || {}
  const regime       = regimeDetail.regime || null
  const regimeConf   = regimeDetail.confidence || 0
  const inTransition = regimeDetail.in_transition || false

  // V3.0: 资金费率数据
  const fundingData = sentiment?.funding || regimeDetail.funding || null
  const fundingRate = fundingData?.funding_rate
  const fundingSignal = fundingData?.signal

  // V3.0: OI 数据
  const oiData = sentiment?.oi || regimeDetail.oi || null

  const pnlColor = (v) => v == null ? 'var(--muted)' : v >= 0 ? 'var(--green)' : 'var(--red)'
  const fmt      = (v, dec=2) => v == null ? '—' : Number(v).toFixed(dec)

  // Regime 颜色映射
  const regimeColors = {
    bull: 'var(--green)', bear: 'var(--red)', ranging: 'var(--yellow)',
    breakout: '#ff6b35', wait: 'var(--muted)', unknown: 'var(--muted2)',
  }
  const regimeLabels = {
    bull: '🐂 牛市', bear: '🐻 熊市', ranging: '📊 震荡',
    breakout: '🚀 突破', wait: '⏸️ 观望', unknown: '—',
  }

  // 资金费率格式化
  const fmtFR = (rate) => {
    if (rate == null) return '—'
    return `${(rate * 100).toFixed(4)}%`
  }

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
        <div className="flex gap-8" style={{ alignItems: 'center' }}>
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
            : (
              <div style={{ display:'flex', gap:0, alignItems:'center' }}>
                {/* 策略选择下拉 */}
                <div ref={stratMenuRef} style={{ position:'relative' }}>
                  <button
                    className="btn-ghost btn-sm"
                    style={{
                      borderRight: '1px solid var(--border)',
                      borderRadius: '6px 0 0 6px',
                      paddingRight: 8,
                      display: 'flex', alignItems: 'center', gap: 4,
                      fontSize: 12,
                      minWidth: 90,
                    }}
                    onClick={() => setShowStratMenu(v => !v)}
                    disabled={loading}
                  >
                    {selStrategy === 'AUTO'
                      ? <><Zap size={11} style={{color:'var(--yellow)'}}/> AUTO</>
                      : selStrategy}
                    <ChevronDown size={11} />
                  </button>
                  {showStratMenu && (
                    <div style={{
                      position: 'absolute', top: '100%', left: 0, zIndex: 100,
                      background: 'var(--surface2)', border: '1px solid var(--border)',
                      borderRadius: 6, minWidth: 160, marginTop: 4,
                      boxShadow: '0 4px 16px rgba(0,0,0,0.3)',
                      overflow: 'hidden',
                    }}>
                      {/* AUTO 选项 */}
                      <div
                        onClick={() => { setSelStrategy('AUTO'); setShowStratMenu(false) }}
                        style={{
                          padding: '8px 14px', cursor: 'pointer', fontSize: 13,
                          display: 'flex', alignItems: 'center', gap: 8,
                          background: selStrategy === 'AUTO' ? 'var(--surface3)' : 'transparent',
                          borderBottom: '1px solid var(--border)',
                        }}
                        onMouseEnter={e => e.currentTarget.style.background = 'var(--surface3)'}
                        onMouseLeave={e => e.currentTarget.style.background = selStrategy === 'AUTO' ? 'var(--surface3)' : 'transparent'}
                      >
                        <Zap size={12} style={{color:'var(--yellow)'}} />
                        <div>
                          <div style={{ fontWeight: 600 }}>AUTO</div>
                          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 1 }}>AI 智能策略切换</div>
                        </div>
                      </div>
                      {/* 各策略选项 */}
                      {strategies.map(s => (
                        <div
                          key={s.name}
                          onClick={() => { setSelStrategy(s.name); setShowStratMenu(false) }}
                          style={{
                            padding: '8px 14px', cursor: 'pointer', fontSize: 13,
                            display: 'flex', alignItems: 'center', gap: 8,
                            background: selStrategy === s.name ? 'var(--surface3)' : 'transparent',
                          }}
                          onMouseEnter={e => e.currentTarget.style.background = 'var(--surface3)'}
                          onMouseLeave={e => e.currentTarget.style.background = selStrategy === s.name ? 'var(--surface3)' : 'transparent'}
                        >
                          <div>
                            <div style={{ fontWeight: 600 }}>{s.name}</div>
                            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 1 }}>{s.class}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                {/* 启动按钮 */}
                <button
                  className="btn-success btn-sm"
                  style={{ borderRadius: '0 6px 6px 0' }}
                  onClick={handleStart}
                  disabled={loading}
                >
                  {loading ? <span className="spinner" /> : <><Play size={12} style={{marginRight:4}}/>启动 Bot</>}
                </button>
              </div>
            )
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

      {/* ── V3.0: 市场状态面板 ── */}
      {running && (
        <div className="card mb-16">
          <div className="card-header" style={{ display:'flex', alignItems:'center', gap:8 }}>
            <Activity size={14} />
            市场状态 · V2.0
            {regime && (
              <span style={{
                marginLeft:'auto', fontSize:12, fontWeight:700,
                color: regimeColors[regime] || 'var(--muted)',
                display:'flex', alignItems:'center', gap:6,
              }}>
                {regimeLabels[regime] || regime}
                {regimeConf > 0 && (
                  <span style={{ fontSize:10, fontWeight:400, color:'var(--muted)' }}>
                    {(regimeConf * 100).toFixed(0)}%
                  </span>
                )}
                {inTransition && (
                  <span style={{
                    fontSize:10, background:'var(--yellow)', color:'#000',
                    padding:'1px 6px', borderRadius:3, fontWeight:600,
                  }}>
                    过渡期
                  </span>
                )}
              </span>
            )}
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)' }}>
            {/* 资金费率 */}
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>
                <DollarSign size={10} style={{verticalAlign:'middle',marginRight:2}} />
                资金费率
              </div>
              <div style={{
                fontSize:15, fontWeight:700,
                color: fundingRate != null
                  ? fundingRate > 0.0003 ? 'var(--red)'
                    : fundingRate < -0.0003 ? 'var(--green)'
                    : 'var(--text)'
                  : 'var(--muted2)'
              }}>
                {fmtFR(fundingRate)}
              </div>
              {fundingSignal && fundingSignal !== 'neutral' && (
                <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>
                  {fundingSignal === 'bearish' ? '📉 多头拥挤' : '📈 空头拥挤'}
                </div>
              )}
            </div>
            {/* OI */}
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>
                <BarChart2 size={10} style={{verticalAlign:'middle',marginRight:2}} />
                持仓量 (OI)
              </div>
              <div style={{ fontSize:15, fontWeight:700, color:'var(--text)' }}>
                {oiData?.oi ? `${(oiData.oi / 1e6).toFixed(1)}M` : '—'}
              </div>
              {oiData?.signal && oiData.signal !== 'stable' && (
                <div style={{
                  fontSize:10, marginTop:2,
                  color: oiData.signal === 'rising' ? 'var(--green)' : 'var(--red)',
                }}>
                  {oiData.signal === 'rising' ? '📈 上升' : '📉 下降'}
                  {oiData.change_pct ? ` (${(oiData.change_pct * 100).toFixed(1)}%)` : ''}
                </div>
              )}
            </div>
            {/* 新闻情绪 */}
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>
                <Eye size={10} style={{verticalAlign:'middle',marginRight:2}} />
                新闻情绪
              </div>
              {sentiment?.news ? (
                <>
                  <div style={{
                    fontSize:15, fontWeight:700,
                    color: sentiment.news.combined_score > 0.2 ? 'var(--green)'
                      : sentiment.news.combined_score < -0.2 ? 'var(--red)'
                      : 'var(--text)',
                  }}>
                    {sentiment.news.combined_score > 0 ? '+' : ''}{sentiment.news.combined_score.toFixed(2)}
                  </div>
                  <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>
                    {sentiment.news.age_minutes < 60
                      ? `${Math.round(sentiment.news.age_minutes)}分钟前`
                      : `${(sentiment.news.age_minutes / 60).toFixed(1)}小时前`}
                    {sentiment.ai_available && ' · AI'}
                  </div>
                </>
              ) : (
                <div style={{ fontSize:15, fontWeight:700, color:'var(--muted2)' }}>—</div>
              )}
            </div>
            {/* 综合信号 */}
            <div style={{ padding:'12px 16px' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>
                <Shield size={10} style={{verticalAlign:'middle',marginRight:2}} />
                综合信号
              </div>
              {regimeDetail.votes ? (
                <>
                  {/* 置信度条 */}
                  <div style={{
                    width:'100%', height:6, background:'var(--surface3)',
                    borderRadius:3, overflow:'hidden', marginTop:8, marginBottom:4,
                  }}>
                    <div style={{
                      width: `${Math.min(regimeConf * 100, 100)}%`,
                      height:'100%', borderRadius:3,
                      background: regimeConf > 0.7 ? 'var(--green)'
                        : regimeConf > 0.4 ? 'var(--yellow)' : 'var(--red)',
                      transition: 'width 0.5s ease',
                    }} />
                  </div>
                  <div style={{ fontSize:10, color:'var(--muted)' }}>
                    置信度 {(regimeConf * 100).toFixed(0)}%
                  </div>
                </>
              ) : (
                <div style={{ fontSize:15, fontWeight:700, color:'var(--muted2)' }}>—</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── 新闻同步状态卡片 ── */}
      {newsSync?.latest && (
        <div className="card mb-16">
          <div className="card-header" style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <span>新闻同步状态</span>
            <button
              className="btn btn-secondary"
              onClick={async () => {
                try {
                  await newsSyncApi.run()
                  const r = await newsSyncApi.status()
                  setNewsSync(r.data)
                  const s = await marketApi.sentiment()
                  setSentiment(s.data)
                } catch {}
              }}
              style={{ padding:'6px 10px', fontSize:12 }}
            >
              立即同步
            </button>
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)' }}>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>启用状态</div>
              <div style={{ fontSize:15, fontWeight:700, color: newsSync.enabled_by_weight ? 'var(--green)' : 'var(--muted2)' }}>
                {newsSync.enabled_by_weight ? '已启用' : '已关闭'}
              </div>
            </div>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>新闻权重</div>
              <div style={{ fontSize:15, fontWeight:700 }}>{newsSync.news_weight ?? '—'}</div>
            </div>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>最近同步</div>
              <div style={{ fontSize:15, fontWeight:700 }}>{newsSync.age_minutes != null ? `${newsSync.age_minutes} 分钟前` : '—'}</div>
            </div>
            <div style={{ padding:'12px 16px' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>最新判断</div>
              <div style={{ fontSize:15, fontWeight:700,
                color: newsSync.latest.regime_hint === 'bull' ? 'var(--green)'
                  : newsSync.latest.regime_hint === 'bear' ? 'var(--red)'
                  : 'var(--text)' }}>
                {newsSync.latest.regime_hint} ({newsSync.latest.combined_score > 0 ? '+' : ''}{Number(newsSync.latest.combined_score || 0).toFixed(2)})
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── V7.0: 空仓状态解释面板 — "为什么不动" ── */}
      {running && !hasPos && (
        <div className="card mb-16">
          <div className="card-header" style={{ display:'flex', alignItems:'center', gap:8 }}>
            <Shield size={14} />
            {cdActive || spikeCooldownUntil
              ? '🧊 保护模式 · 冷静期中'
              : '📋 空仓状态'}
          </div>

          {/* 冷静期警告 */}
          {cdActive && (
            <div style={{
              padding:'12px 16px', borderBottom:'1px solid var(--border)',
              background:'rgba(100, 181, 246, 0.06)',
              display:'flex', alignItems:'flex-start', gap:10,
            }}>
              <span style={{ fontSize:20, flexShrink:0 }}>🧊</span>
              <div>
                <div style={{ fontWeight:700, fontSize:13, color:'var(--blue-light)', marginBottom:4 }}>
                  平仓冷静期 — 剩余 {cdBarsRemaining} 根 K 线
                </div>
                <div style={{ fontSize:12, color:'var(--muted)', lineHeight:1.6 }}>
                  上次平仓后，系统进入保护模式。即使出现信号也不会立即重新开仓，
                  避免在波动未稳定时贸然追入。
                </div>
              </div>
            </div>
          )}

          {/* 插针冷静期 */}
          {spikeCooldownUntil && !cdActive && (
            <div style={{
              padding:'12px 16px', borderBottom:'1px solid var(--border)',
              background:'rgba(255, 165, 0, 0.06)',
              display:'flex', alignItems:'flex-start', gap:10,
            }}>
              <span style={{ fontSize:20, flexShrink:0 }}>⚡</span>
              <div>
                <div style={{ fontWeight:700, fontSize:13, color:'#ffa500', marginBottom:4 }}>
                  异常波动冷静期
                </div>
                <div style={{ fontSize:12, color:'var(--muted)', lineHeight:1.6 }}>
                  检测到近期 K 线存在插针/异常波动，系统暂停开仓等待波动率回落。
                  <br/>截止时间: {spikeCooldownUntil}
                </div>
              </div>
            </div>
          )}

          {/* 上次平仓信息 + 信号质量 */}
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)' }}>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>上次平仓</div>
              <div style={{ fontSize:13, fontWeight:600,
                color: lastClosePnl > 0 ? 'var(--green)' : lastClosePnl < 0 ? 'var(--red)' : 'var(--muted2)' }}>
                {lastClosePnl ? `${lastClosePnl > 0 ? '+' : ''}${Number(lastClosePnl).toFixed(2)} U` : '—'}
              </div>
              {lastCloseReason && (
                <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>{lastCloseReason}</div>
              )}
            </div>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>上次方向</div>
              <div style={{ fontSize:13, fontWeight:600,
                color: lastCloseSide === 'long' ? 'var(--green)' : lastCloseSide === 'short' ? 'var(--red)' : 'var(--muted2)' }}>
                {lastCloseSide === 'long' ? '做多' : lastCloseSide === 'short' ? '做空' : '—'}
              </div>
              {lastCloseTime && (
                <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>{lastCloseTime.slice(5,16)}</div>
              )}
            </div>
            <div style={{ padding:'12px 16px', borderRight:'1px solid var(--border)' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>信号质量</div>
              <div style={{ fontSize:15, fontWeight:700,
                color: signalQuality == null ? 'var(--muted2)'
                  : signalQuality >= 60 ? 'var(--green)'
                  : signalQuality >= 35 ? 'var(--yellow)'
                  : 'var(--red)' }}>
                {signalQuality != null ? `${Number(signalQuality).toFixed(0)}/100` : '—'}
              </div>
              <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>
                {signalQuality != null
                  ? signalQuality >= 60 ? '信号较强' : signalQuality >= 35 ? '信号一般' : '信号较弱'
                  : ''}
              </div>
            </div>
            <div style={{ padding:'12px 16px' }}>
              <div style={{ fontSize:11, color:'var(--muted)', marginBottom:4 }}>当前状态</div>
              <div style={{ fontSize:13, fontWeight:600, color:'var(--text)' }}>
                {cdActive ? '⏸️ 冷静等待'
                  : spikeCooldownUntil ? '⚡ 波动过滤'
                  : regime === 'wait' ? '👀 观望中'
                  : regime ? '🔍 寻找信号'
                  : '—'}
              </div>
              <div style={{ fontSize:10, color:'var(--muted)', marginTop:2 }}>
                {cdActive ? '不会贸然追入'
                  : spikeCooldownUntil ? '等待波动率回落'
                  : regime === 'wait' ? '市场方向不明'
                  : '等待优质开仓时机'}
              </div>
            </div>
          </div>

          {/* 无冷静期时的说明文字 */}
          {!cdActive && !spikeCooldownUntil && (
            <div style={{
              padding:'10px 16px', borderTop:'1px solid var(--border)',
              fontSize:12, color:'var(--muted)', lineHeight:1.6,
            }}>
              {regime === 'wait'
                ? '📋 当前市场方向不明确（ADX 较低 + 趋势信号模糊），系统选择等待更明确的信号再入场。'
                : regime === 'ranging'
                  ? '📋 当前市场处于震荡区间，系统使用震荡策略评估，等待突破或反转信号。'
                  : `📋 市场状态: ${regimeLabels[regime] || '评估中'} — 策略正在评估信号，满足条件后将自动开仓。`
              }
            </div>
          )}
        </div>
      )}

      {/* ── 持仓详情卡片 ── */}
      {hasPos && (
        <div className="card mb-16">
          <div className="card-header">当前持仓详情</div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)' }}>
            {[
              { label:'止损价 (SL)', value: activeSl ? `$${activeSl.toLocaleString()}` : '—', color:'var(--red)' },
              { label:'止盈价 (TP1)', value: activeTp ? `$${activeTp.toLocaleString()}` : '—', color:'var(--green)' },
              { label:'预期风险',
                value: activeSl && entryPx && posAmt && curPrice
                  ? `~${Math.abs((entryPx - activeSl) * posAmt * (d.contract_size || 0.01)).toFixed(2)} U`
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
