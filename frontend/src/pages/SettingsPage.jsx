import { useState } from 'react'
import { keysApi } from '../api'

export default function SettingsPage() {
  const [form, setForm] = useState({ api_key:'', secret:'', passphrase:'', is_simulate: false })
  const [msg, setMsg] = useState(null)
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setMsg(null); setLoading(true)
    try {
      await keysApi.save(form)
      setMsg({ type:'ok', text:'✅ API Key 已加密保存！' })
      setForm({ api_key:'', secret:'', passphrase:'', is_simulate: false })
    } catch (err) {
      setMsg({ type:'err', text: err.response?.data?.detail || '保存失败' })
    } finally {
      setLoading(false)
    }
  }

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div>
      <h1 className="page-title">⚙️ 设置</h1>

      <div className="card" style={{maxWidth: 540}}>
        <div style={{fontWeight:600, marginBottom:20}}>OKX API Key 配置</div>
        <div className="card" style={{background:'rgba(210,153,34,.08)', borderColor:'rgba(210,153,34,.3)', marginBottom:20, fontSize:13}}>
          💡 API Key 在服务端以 AES 加密存储，明文不会被持久化。<br/>
          请确保 Key 开启了「读取」和「交易」权限，并绑定了你的 IP。
        </div>
        <form onSubmit={handleSubmit}>
          {[
            ['api_key',    'API Key'],
            ['secret',     'Secret Key'],
            ['passphrase', 'Passphrase'],
          ].map(([k, label]) => (
            <div className="form-group" key={k}>
              <label>{label}</label>
              <input
                type="password"
                value={form[k]}
                onChange={e => set(k, e.target.value)}
                placeholder={`请输入 ${label}`}
                required
                autoComplete="off"
              />
            </div>
          ))}
          <div className="form-group" style={{display:'flex', alignItems:'center', gap:10}}>
            <input
              type="checkbox"
              id="sim"
              style={{width:'auto'}}
              checked={form.is_simulate}
              onChange={e => set('is_simulate', e.target.checked)}
            />
            <label htmlFor="sim" style={{margin:0, cursor:'pointer'}}>使用模拟盘（Sandbox）</label>
          </div>
          <button type="submit" className="btn-primary" disabled={loading} style={{marginTop:4}}>
            {loading ? '保存中...' : '保存 API Key'}
          </button>
          {msg && (
            <p style={{marginTop:12, color: msg.type==='ok'?'var(--green)':'var(--red)', fontSize:13}}>
              {msg.text}
            </p>
          )}
        </form>
      </div>
    </div>
  )
}
