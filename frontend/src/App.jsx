import { useState, useEffect } from 'react'
import './index.css'
import AuthPage     from './pages/AuthPage'
import Dashboard    from './pages/Dashboard'
import TradesPage   from './pages/TradesPage'
import BalancePage  from './pages/BalancePage'
import BacktestPage from './pages/BacktestPage'
import SettingsPage from './pages/SettingsPage'
import {
  LayoutDashboard, ListOrdered, TrendingUp,
  FlaskConical, Settings, LogOut, ChevronRight, ChevronLeft,
  Zap
} from 'lucide-react'

const PAGES = [
  { key: 'dashboard', label: '控制台',   Icon: LayoutDashboard },
  { key: 'trades',    label: '交易记录', Icon: ListOrdered },
  { key: 'balance',   label: '资产曲线', Icon: TrendingUp },
  { key: 'backtest',  label: '策略回测', Icon: FlaskConical },
  { key: 'settings',  label: '设置',     Icon: Settings },
]

export default function App() {
  const [username, setUsername] = useState(localStorage.getItem('username') || '')
  const [page, setPage]         = useState('dashboard')
  const [open, setOpen]         = useState(false)

  const logout = () => {
    localStorage.removeItem('token')
    localStorage.removeItem('username')
    setUsername('')
  }

  if (!username || !localStorage.getItem('token')) {
    return <AuthPage onLogin={u => setUsername(u)} />
  }

  const renderPage = () => {
    switch (page) {
      case 'dashboard': return <Dashboard username={username} />
      case 'trades':    return <TradesPage />
      case 'balance':   return <BalancePage />
      case 'backtest':  return <BacktestPage />
      case 'settings':  return <SettingsPage />
      default:          return <Dashboard username={username} />
    }
  }

  return (
    <div className={`layout${open ? ' sidebar-open' : ''}`}>
      {/* ── 顶栏 ── */}
      <header className="topbar">
        <button
          onClick={() => setOpen(o => !o)}
          className="btn-ghost btn-sm"
          style={{ padding:'5px 8px', marginRight: 4 }}
          title={open ? '收起侧边栏' : '展开侧边栏'}
        >
          {open
            ? <ChevronLeft size={15} />
            : <ChevronRight size={15} />
          }
        </button>

        <div className="topbar-item">
          <span className="t-label">系统</span>
          <span className="t-value" style={{ color: 'var(--blue-light)' }}>QuantBot</span>
        </div>

        <div className="topbar-divider" />

        <div className="topbar-item">
          <span className="t-label">OKX 永续</span>
          <span className="t-value">实时交易</span>
        </div>

        <div className="topbar-right">
          <span className="topbar-user">👤 {username}</span>
          <button className="btn-ghost btn-sm" onClick={logout} title="退出登录">
            <LogOut size={13} />
          </button>
        </div>
      </header>

      {/* ── 侧边栏 ── */}
      <aside className="sidebar">
        <div className="sidebar-logo">
          {open ? <><Zap size={15}/> QB</> : 'Q'}
        </div>

        {PAGES.map(({ key, label, Icon }) => (
          <div
            key={key}
            className={`nav-item${page === key ? ' active' : ''}`}
            onClick={() => setPage(key)}
            title={!open ? label : undefined}
          >
            <Icon size={16} />
            <span className="nav-label">{label}</span>
          </div>
        ))}

        <div className="sidebar-bottom">
          <div
            className="nav-item"
            onClick={logout}
            title={!open ? '退出登录' : undefined}
            style={{ color: 'var(--muted)' }}
          >
            <LogOut size={16} />
            <span className="nav-label" style={{ color: 'var(--red)' }}>退出登录</span>
          </div>
        </div>
      </aside>

      {/* ── 主内容 ── */}
      <main className="main-content">
        {renderPage()}
      </main>
    </div>
  )
}
