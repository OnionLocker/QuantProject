import { useState, useEffect } from 'react'
import './index.css'
import AuthPage     from './pages/AuthPage'
import Dashboard    from './pages/Dashboard'
import TradesPage   from './pages/TradesPage'
import BalancePage  from './pages/BalancePage'
import BacktestPage from './pages/BacktestPage'
import SettingsPage from './pages/SettingsPage'

const PAGES = [
  { key: 'dashboard', label: '🏠 控制台' },
  { key: 'trades',    label: '📋 交易记录' },
  { key: 'balance',   label: '💰 资产曲线' },
  { key: 'backtest',  label: '🧪 回测' },
  { key: 'settings',  label: '⚙️ 设置' },
]

export default function App() {
  const [username, setUsername] = useState(localStorage.getItem('username') || '')
  const [page, setPage] = useState('dashboard')

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
    <div className="layout">
      <aside className="sidebar">
        <div className="logo">⚡ QuantBot</div>
        {PAGES.map(p => (
          <a key={p.key} href="#"
            className={page === p.key ? 'active' : ''}
            onClick={e => { e.preventDefault(); setPage(p.key) }}>
            {p.label}
          </a>
        ))}
        <div style={{flex:1}} />
        <div style={{borderTop:'1px solid var(--border)', paddingTop:16, marginTop:8}}>
          <div style={{fontSize:12, color:'var(--muted)', marginBottom:8, paddingLeft:8}}>
            {username}
          </div>
          <a href="#" onClick={e => { e.preventDefault(); logout() }}
            style={{color:'var(--red)', fontSize:13, paddingLeft:8}}>
            退出登录
          </a>
        </div>
      </aside>
      <main className="main-content">
        {renderPage()}
      </main>
    </div>
  )
}
