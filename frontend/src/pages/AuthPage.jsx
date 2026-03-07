import { useState } from 'react'
import { authApi } from '../api'
import { Zap, Eye, EyeOff } from 'lucide-react'

export default function AuthPage({ onLogin }) {
  const [mode, setMode]       = useState('login')
  const [username, setUser]   = useState('')
  const [password, setPass]   = useState('')
  const [showPw, setShowPw]   = useState(false)
  const [loading, setLoading] = useState(false)
  const [err, setErr]         = useState('')

  const submit = async e => {
    e.preventDefault()
    setErr('')
    if (!username.trim() || !password.trim()) { setErr('请填写用户名和密码'); return }
    setLoading(true)
    try {
      if (mode === 'login') {
        const r = await authApi.login(username, password)
        localStorage.setItem('token', r.data.access_token)
        localStorage.setItem('username', username)
        onLogin(username)
      } else {
        await authApi.register(username, password)
        const r = await authApi.login(username, password)
        localStorage.setItem('token', r.data.access_token)
        localStorage.setItem('username', username)
        onLogin(username)
      }
    } catch (e) {
      setErr(e.response?.data?.detail || '操作失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--bg)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    }}>
      {/* 背景网格 */}
      <div style={{
        position: 'fixed', inset: 0, pointerEvents: 'none',
        backgroundImage: 'linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)',
        backgroundSize: '40px 40px',
        opacity: .3,
      }} />

      <div style={{
        position: 'relative',
        width: 360,
        background: 'var(--surface)',
        border: '1px solid var(--border2)',
        borderRadius: 12,
        padding: '36px 32px',
      }}>
        {/* Logo */}
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div style={{
            width: 48, height: 48,
            background: 'var(--blue)',
            borderRadius: 10,
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: 12,
          }}>
            <Zap size={24} color="#fff" />
          </div>
          <div style={{ fontSize: 20, fontWeight: 800, letterSpacing: '-.3px' }}>QuantBot</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4 }}>
            OKX 量化交易平台
          </div>
        </div>

        {/* Tab */}
        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 1fr',
          background: 'var(--bg)',
          borderRadius: 6, padding: 3,
          marginBottom: 24,
        }}>
          {['login','register'].map(m => (
            <button
              key={m}
              onClick={() => { setMode(m); setErr('') }}
              style={{
                background: mode === m ? 'var(--surface2)' : 'transparent',
                color: mode === m ? 'var(--text)' : 'var(--muted)',
                borderRadius: 4,
                padding: '6px 0',
                fontWeight: mode === m ? 700 : 500,
                fontSize: 13,
                transition: 'all .15s',
              }}
            >
              {m === 'login' ? '登录' : '注册'}
            </button>
          ))}
        </div>

        <form onSubmit={submit}>
          <div className="form-row">
            <label className="form-label">用户名</label>
            <input
              value={username}
              onChange={e => setUser(e.target.value)}
              placeholder="输入用户名"
              autoComplete="username"
            />
          </div>

          <div className="form-row">
            <label className="form-label">密码</label>
            <div style={{ position: 'relative' }}>
              <input
                type={showPw ? 'text' : 'password'}
                value={password}
                onChange={e => setPass(e.target.value)}
                placeholder="输入密码"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                style={{ paddingRight: 36 }}
              />
              <button
                type="button"
                onClick={() => setShowPw(s => !s)}
                style={{
                  position: 'absolute', right: 8, top: '50%',
                  transform: 'translateY(-50%)',
                  background: 'none', color: 'var(--muted)',
                  padding: 4,
                }}
              >
                {showPw ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </div>

          {err && (
            <div className="alert alert-danger" style={{ marginBottom: 14 }}>{err}</div>
          )}

          <button
            type="submit"
            className="btn-primary"
            disabled={loading}
            style={{ width: '100%', padding: '9px 0', fontSize: 14, marginTop: 4 }}
          >
            {loading
              ? <span className="spinner" style={{ width: 14, height: 14 }} />
              : mode === 'login' ? '登录' : '注册并登录'
            }
          </button>
        </form>

        <div style={{ marginTop: 20, textAlign: 'center', fontSize: 11, color: 'var(--muted2)' }}>
          数据加密存储 · API Key 本地持有
        </div>
      </div>
    </div>
  )
}
