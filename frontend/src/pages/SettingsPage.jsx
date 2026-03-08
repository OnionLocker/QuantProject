import { useState, useEffect } from 'react'
import { keysApi, notifyApi } from '../api'
import { KeyRound, Bell, Eye, EyeOff, Check, X, Send, Trash2 } from 'lucide-react'

function Section({ icon: Icon, title, children }) {
  return (
    <div className="card mb-16">
      <div className="card-header" style={{ display:'flex', alignItems:'center', gap:6 }}>
        {Icon && <Icon size={12} />}{title}
      </div>
      {children}
    </div>
  )
}

export default function SettingsPage() {
  // OKX Key
  const [keyForm, setKeyForm] = useState({ api_key:'', secret:'', passphrase:'', is_simulate: false })
  const [showSecret, setShowSecret] = useState(false)
  const [keyStatus, setKeyStatus]   = useState(null)
  const [keySaving, setKeySaving]   = useState(false)
  const [keyMsg, setKeyMsg]         = useState('')
  const [keyValidating, setKeyValidating] = useState(false)

  // Telegram
  const [tgForm, setTgForm]   = useState({ tg_bot_token:'', tg_chat_id:'' })
  const [tgStatus, setTgStatus] = useState(null)
  const [tgSaving, setTgSaving] = useState(false)
  const [tgMsg, setTgMsg]       = useState('')
  const [testing, setTesting]   = useState(false)

  useEffect(() => {
    keysApi.status().then(r  => setKeyStatus(r.data))
    notifyApi.tgStatus().then(r => setTgStatus(r.data))
  }, [])

  const saveKey = async e => {
    e.preventDefault()
    setKeySaving(true); setKeyMsg('')
    try {
      await keysApi.save(keyForm)
      const r = await keysApi.status(); setKeyStatus(r.data)
      setKeyMsg('✅ API Key 已保存（AES 加密）。建议点击「验证 API Key」确认可用')
      setKeyForm({ api_key:'', secret:'', passphrase:'', is_simulate: false })
    } catch (err) {
      const d = err.response?.data?.detail
      setKeyMsg('❌ ' + (Array.isArray(d) ? d.map(x => x.msg || x).join(' ') : d || '保存失败'))
    } finally { setKeySaving(false) }
  }

  const validateKey = async () => {
    setKeyValidating(true); setKeyMsg('')
    try {
      const r = await keysApi.validate()
      setKeyMsg('✅ ' + (r.data?.message || 'API Key 有效'))
    } catch (err) {
      setKeyMsg('❌ ' + (err.response?.data?.detail || '验证失败'))
    } finally { setKeyValidating(false) }
  }

  const saveTg = async e => {
    e.preventDefault()
    setTgSaving(true); setTgMsg('')
    try {
      await notifyApi.saveTg(tgForm)
      const r = await notifyApi.tgStatus(); setTgStatus(r.data)
      setTgMsg('✅ Telegram 配置已保存')
      setTgForm({ tg_bot_token:'', tg_chat_id:'' })
    } catch (err) {
      setTgMsg('❌ ' + (err.response?.data?.detail || '保存失败'))
    } finally { setTgSaving(false) }
  }

  const testTg = async () => {
    setTesting(true); setTgMsg('')
    try {
      await notifyApi.testTg()
      setTgMsg('✅ 测试消息已发送，请查看 Telegram')
    } catch (err) {
      setTgMsg('❌ ' + (err.response?.data?.detail || '发送失败'))
    } finally { setTesting(false) }
  }

  const clearTg = async () => {
    try {
      await notifyApi.clearTg()
      const r = await notifyApi.tgStatus(); setTgStatus(r.data)
      setTgMsg('✅ 已清除 Telegram 配置')
    } catch {}
  }

  return (
    <div style={{ maxWidth: 680 }}>
      <div className="page-header">
        <div>
          <div className="page-title">设置</div>
          <div className="page-sub">API Key 与通知配置</div>
        </div>
      </div>

      {/* ── OKX API Key ── */}
      <Section icon={KeyRound} title="OKX API Key">
        {keyStatus?.configured && (
          <div className="alert alert-success mb-12" style={{ display:'flex', alignItems:'center', flexWrap:'wrap', gap:8 }}>
            <span>
              <Check size={12} style={{ marginRight:6, verticalAlign:'middle' }} />
              已配置 API Key
              {keyStatus.is_simulate && <span className="badge badge-yellow" style={{marginLeft:8}}>模拟盘</span>}
              <span className="col-muted" style={{ marginLeft:8, fontSize:11 }}>更新于 {keyStatus.updated_at}</span>
            </span>
            <button type="button" className="btn-ghost btn-sm" onClick={validateKey} disabled={keyValidating} style={{ marginLeft:'auto' }}>
              {keyValidating ? <span className="spinner" /> : '验证 API Key'}
            </button>
          </div>
        )}

        <form onSubmit={saveKey}>
          <div className="form-row">
            <label className="form-label">API Key</label>
            <input
              value={keyForm.api_key}
              onChange={e => setKeyForm(f => ({...f, api_key: e.target.value}))}
              placeholder={keyStatus?.configured ? '（已配置，留空则不修改）' : '输入 OKX API Key'}
            />
          </div>

          <div className="form-row">
            <label className="form-label">Secret Key</label>
            <div style={{ position:'relative' }}>
              <input
                type={showSecret ? 'text' : 'password'}
                value={keyForm.secret}
                onChange={e => setKeyForm(f => ({...f, secret: e.target.value}))}
                placeholder={keyStatus?.configured ? '（已配置）' : '输入 Secret Key'}
                style={{ paddingRight:36 }}
              />
              <button type="button"
                onClick={() => setShowSecret(s => !s)}
                style={{ position:'absolute', right:8, top:'50%', transform:'translateY(-50%)', background:'none', color:'var(--muted)', padding:4 }}>
                {showSecret ? <EyeOff size={13} /> : <Eye size={13} />}
              </button>
            </div>
          </div>

          <div className="form-row">
            <label className="form-label">Passphrase</label>
            <input
              type="password"
              value={keyForm.passphrase}
              onChange={e => setKeyForm(f => ({...f, passphrase: e.target.value}))}
              placeholder={keyStatus?.configured ? '（已配置）' : '输入 Passphrase'}
            />
          </div>

          <div className="form-row" style={{ flexDirection:'row', alignItems:'center', gap:10 }}>
            <input
              type="checkbox"
              id="simulate"
              checked={keyForm.is_simulate}
              onChange={e => setKeyForm(f => ({...f, is_simulate: e.target.checked}))}
              style={{ width:'auto' }}
            />
            <label htmlFor="simulate" style={{ fontSize:13, color:'var(--text)', cursor:'pointer' }}>
              使用模拟盘（OKX 沙盒环境）
            </label>
          </div>

          {keyMsg && (
            <div className={`alert ${keyMsg.startsWith('✅') ? 'alert-success' : 'alert-danger'} mb-12`}>
              {keyMsg}
            </div>
          )}

          <button type="submit" className="btn-primary btn-sm" disabled={keySaving}>
            {keySaving ? <span className="spinner" /> : '保存 API Key'}
          </button>
        </form>

        <div className="alert alert-info" style={{ marginTop:14 }}>
          API Key 使用 Fernet AES 加密存储，明文不落盘，服务端仅在交易时临时解密使用。
        </div>
      </Section>

      {/* ── Telegram ── */}
      <Section icon={Bell} title="Telegram 通知">
        <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:16 }}>
          {tgStatus?.configured
            ? <><span className="dot dot-green" /><span style={{ fontSize:12, color:'var(--green)' }}>已配置</span></>
            : <><span className="dot dot-gray" /><span style={{ fontSize:12, color:'var(--muted)' }}>未配置</span></>
          }
          {tgStatus?.configured && (
            <div style={{ marginLeft:'auto', display:'flex', gap:8 }}>
              <button className="btn-ghost btn-sm" onClick={testTg} disabled={testing}>
                {testing ? <span className="spinner" /> : <><Send size={12} style={{marginRight:4}} />发测试消息</>}
              </button>
              <button className="btn-ghost btn-sm" onClick={clearTg} style={{ color:'var(--red)' }}>
                <Trash2 size={12} style={{marginRight:4}} />清除
              </button>
            </div>
          )}
        </div>

        <form onSubmit={saveTg}>
          <div className="form-row">
            <label className="form-label">Bot Token</label>
            <input
              value={tgForm.tg_bot_token}
              onChange={e => setTgForm(f => ({...f, tg_bot_token: e.target.value}))}
              placeholder={tgStatus?.configured ? '（已配置，留空则不修改）' : '从 @BotFather 获取'}
            />
          </div>
          <div className="form-row">
            <label className="form-label">Chat ID</label>
            <input
              value={tgForm.tg_chat_id}
              onChange={e => setTgForm(f => ({...f, tg_chat_id: e.target.value}))}
              placeholder={tgStatus?.configured ? '（已配置）' : '用 @userinfobot 查询'}
            />
          </div>

          {tgMsg && (
            <div className={`alert ${tgMsg.startsWith('✅') ? 'alert-success' : 'alert-danger'} mb-12`}>
              {tgMsg}
            </div>
          )}

          <button type="submit" className="btn-primary btn-sm" disabled={tgSaving}>
            {tgSaving ? <span className="spinner" /> : '保存 Telegram 配置'}
          </button>
        </form>
      </Section>
    </div>
  )
}
