import { useState } from 'react'
import { authApi } from '../api'

export default function AuthPage({ onLogin }) {
  const [mode, setMode] = useState('login')   // 'login' | 'register'
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setLoading(true)
    try {
      const fn = mode === 'login' ? authApi.login : authApi.register
      const res = await fn(username, password)
      localStorage.setItem('token', res.data.access_token)
      localStorage.setItem('username', res.data.username)
      onLogin(res.data.username)
    } catch (err) {
      setError(err.response?.data?.detail || '请求失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-box">
        <h1>⚡ QuantBot</h1>
        <p className="subtitle">OKX 量化交易系统</p>
        <form onSubmit={submit}>
          <div className="form-group">
            <label>用户名</label>
            <input value={username} onChange={e => setUsername(e.target.value)}
              placeholder="请输入用户名" required />
          </div>
          <div className="form-group">
            <label>密码</label>
            <input type="password" value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="请输入密码" required />
          </div>
          <button type="submit" className="btn-primary" style={{width:'100%'}} disabled={loading}>
            {loading ? '处理中...' : (mode === 'login' ? '登录' : '注册')}
          </button>
          {error && <p className="error-msg">{error}</p>}
        </form>
        <p style={{textAlign:'center', marginTop:16, color:'var(--muted)', fontSize:13}}>
          {mode === 'login' ? '没有账号？' : '已有账号？'}
          <a href="#" onClick={e => { e.preventDefault(); setMode(mode==='login'?'register':'login') }}>
            {mode === 'login' ? ' 立即注册' : ' 返回登录'}
          </a>
        </p>
      </div>
    </div>
  )
}
