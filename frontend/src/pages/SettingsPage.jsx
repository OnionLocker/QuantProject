import { useState, useEffect } from 'react'
import { keysApi, notifyApi, botApi } from '../api'
import { KeyRound, Bell, Eye, EyeOff, Check, Send, Trash2, RotateCcw } from 'lucide-react'

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
  // Bot 运行状态
  const [botRunning, setBotRunning] = useState(false)

  // OKX Key
  const [keyStatus, setKeyStatus]   = useState(null)
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [keyForm, setKeyForm]       = useState({ api_key:'', secret:'', passphrase:'', is_simulate: false })
  const [showSecret, setShowSecret] = useState(false)
  const [keySaving, setKeySaving]   = useState(false)
  const [keyResetting, setKeyResetting] = useState(false)
  const [keyValidating, setKeyValidating] = useState(false)
  const [keyMsg, setKeyMsg]         = useState('')

  // Telegram
  const [tgStatus, setTgStatus]     = useState(null)
  const [showTgForm, setShowTgForm] = useState(false)
  const [tgForm, setTgForm]         = useState({ tg_bot_token:'', tg_chat_id:'' })
  const [tgSaving, setTgSaving]     = useState(false)
  const [tgResetting, setTgResetting] = useState(false)
  const [tgMsg, setTgMsg]           = useState('')
  const [testing, setTesting]       = useState(false)

  useEffect(() => {
    keysApi.status().then(r => {
      setKeyStatus(r.data)
      setShowKeyForm(!r.data.configured)
    })
    notifyApi.tgStatus().then(r => {
      setTgStatus(r.data)
      setShowTgForm(!r.data.configured)
    })
    botApi.status().then(r => {
      setBotRunning(r.data?.bot?.running === true)
    }).catch(() => {})
  }, [])

  // ── OKX 保存 ──────────────────────────────────────────────────────────────
  const saveKey = async e => {
    e.preventDefault()
    setKeySaving(true); setKeyMsg('')
    try {
      await keysApi.save(keyForm)
      const r = await keysApi.status()
      setKeyStatus(r.data)
      setShowKeyForm(false)
      setKeyMsg('✅ API Key 已保存（AES 加密）。建议点击「验证」确认可用')
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

  const resetKey = async () => {
    if (!window.confirm('确认清除 OKX API Key？清除后 Bot 将无法运行。')) return
    setKeyResetting(true); setKeyMsg('')
    try {
      await keysApi.reset()
      const r = await keysApi.status()
      setKeyStatus(r.data)
      setShowKeyForm(true)
      setKeyMsg('')
    } catch (err) {
      setKeyMsg('❌ ' + (err.response?.data?.detail || '重置失败'))
    } finally { setKeyResetting(false) }
  }

  // ── Telegram 保存 ─────────────────────────────────────────────────────────
  const saveTg = async e => {
    e.preventDefault()
    setTgSaving(true); setTgMsg('')
    try {
      await notifyApi.saveTg(tgForm)
      const r = await notifyApi.tgStatus()
      setTgStatus(r.data)
      setShowTgForm(false)
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

  const resetTg = async () => {
    if (!window.confirm('确认清除 Telegram 配置？')) return
    setTgResetting(true); setTgMsg('')
    try {
      await notifyApi.clearTg()
      const r = await notifyApi.tgStatus()
      setTgStatus(r.data)
      setShowTgForm(true)
      setTgMsg('')
    } catch (err) {
      setTgMsg('❌ ' + (err.response?.data?.detail || '重置失败'))
    } finally { setTgResetting(false) }
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

        {/* 已配置状态栏 */}
        {keyStatus?.configured && !showKeyForm && (
          <div style={{ display:'flex', alignItems:'center', flexWrap:'wrap', gap:10, marginBottom:14 }}>
            <div className="alert alert-success" style={{ flex:1, margin:0, display:'flex', alignItems:'center', gap:8, flexWrap:'wrap' }}>
              <Check size={13} />
              <span>已配置 API Key</span>
              {keyStatus.is_simulate && <span className="badge badge-yellow">模拟盘</span>}
              <span className="col-muted" style={{ fontSize:11 }}>更新于 {keyStatus.updated_at}</span>
            </div>
            <button
              className="btn-ghost btn-sm"
              onClick={validateKey}
              disabled={keyValidating}
              style={{ whiteSpace:'nowrap' }}
            >
              {keyValidating ? <span className="spinner" /> : '验证'}
            </button>
            <button
              className="btn-ghost btn-sm"
              onClick={resetKey}
              disabled={botRunning || keyResetting}
              title={botRunning ? 'Bot 运行中，无法重置' : '清除 API Key 并重新配置'}
              style={{ color:'var(--red)', whiteSpace:'nowrap', display:'flex', alignItems:'center', gap:4 }}
            >
              <RotateCcw size={12} />
              {keyResetting ? <span className="spinner" /> : '重置'}
            </button>
          </div>
        )}
        {botRunning && keyStatus?.configured && !showKeyForm && (
          <div className="alert alert-info" style={{ marginBottom:10, fontSize:12 }}>
            ⚠️ Bot 运行中，需先停止 Bot 才可重置 API Key
          </div>
        )}

        {/* 输入表单（未配置 或 点击重置后显示） */}
        {showKeyForm && (
          <form onSubmit={saveKey}>
            <div className="form-row">
              <label className="form-label">API Key</label>
              <input
                value={keyForm.api_key}
                onChange={e => setKeyForm(f => ({...f, api_key: e.target.value}))}
                placeholder="输入 OKX API Key"
                required
              />
            </div>

            <div className="form-row">
              <label className="form-label">Secret Key</label>
              <div style={{ position:'relative' }}>
                <input
                  type={showSecret ? 'text' : 'password'}
                  value={keyForm.secret}
                  onChange={e => setKeyForm(f => ({...f, secret: e.target.value}))}
                  placeholder="输入 Secret Key"
                  style={{ paddingRight:36 }}
                  required
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
                placeholder="输入 Passphrase"
                required
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

            <div style={{ display:'flex', gap:8, marginTop:4 }}>
              <button type="submit" className="btn-primary btn-sm" disabled={keySaving}>
                {keySaving ? <span className="spinner" /> : '保存 API Key'}
              </button>
              {keyStatus?.configured && (
                <button type="button" className="btn-ghost btn-sm" onClick={() => { setShowKeyForm(false); setKeyMsg('') }}>
                  取消
                </button>
              )}
            </div>
          </form>
        )}

        {keyMsg && (
          <div className={`alert ${keyMsg.startsWith('✅') ? 'alert-success' : 'alert-danger'} mt-12`}>
            {keyMsg}
          </div>
        )}

        {!showKeyForm && (
          <div className="alert alert-info" style={{ marginTop:14 }}>
            API Key 使用 Fernet AES 加密存储，明文不落盘，服务端仅在交易时临时解密使用。
          </div>
        )}
      </Section>

      {/* ── Telegram ── */}
      <Section icon={Bell} title="Telegram 通知">

        {/* 已配置状态栏 */}
        {tgStatus?.configured && !showTgForm && (
          <div style={{ display:'flex', alignItems:'center', flexWrap:'wrap', gap:10, marginBottom:14 }}>
            <div className="alert alert-success" style={{ flex:1, margin:0, display:'flex', alignItems:'center', gap:8 }}>
              <Check size={13} />
              <span>已配置 Telegram 通知</span>
            </div>
            <button className="btn-ghost btn-sm" onClick={testTg} disabled={testing} style={{ whiteSpace:'nowrap', display:'flex', alignItems:'center', gap:4 }}>
              {testing ? <span className="spinner" /> : <><Send size={12} />测试</>}
            </button>
            <button
              className="btn-ghost btn-sm"
              onClick={resetTg}
              disabled={botRunning || tgResetting}
              title={botRunning ? 'Bot 运行中，无法重置' : '清除并重新配置 Telegram'}
              style={{ color:'var(--red)', whiteSpace:'nowrap', display:'flex', alignItems:'center', gap:4 }}
            >
              <RotateCcw size={12} />
              {tgResetting ? <span className="spinner" /> : '重置'}
            </button>
          </div>
        )}
        {botRunning && tgStatus?.configured && !showTgForm && (
          <div className="alert alert-info" style={{ marginBottom:10, fontSize:12 }}>
            ⚠️ Bot 运行中，需先停止 Bot 才可重置 Telegram 配置
          </div>
        )}

        {/* 输入表单 */}
        {showTgForm && (
          <form onSubmit={saveTg}>
            <div className="form-row">
              <label className="form-label">Bot Token</label>
              <input
                value={tgForm.tg_bot_token}
                onChange={e => setTgForm(f => ({...f, tg_bot_token: e.target.value}))}
                placeholder="从 @BotFather 获取"
                required
              />
            </div>
            <div className="form-row">
              <label className="form-label">Chat ID</label>
              <input
                value={tgForm.tg_chat_id}
                onChange={e => setTgForm(f => ({...f, tg_chat_id: e.target.value}))}
                placeholder="用 @userinfobot 查询"
                required
              />
            </div>

            <div style={{ display:'flex', gap:8, marginTop:4 }}>
              <button type="submit" className="btn-primary btn-sm" disabled={tgSaving}>
                {tgSaving ? <span className="spinner" /> : '保存 Telegram 配置'}
              </button>
              {tgStatus?.configured && (
                <button type="button" className="btn-ghost btn-sm" onClick={() => { setShowTgForm(false); setTgMsg('') }}>
                  取消
                </button>
              )}
            </div>
          </form>
        )}

        {tgMsg && (
          <div className={`alert ${tgMsg.startsWith('✅') ? 'alert-success' : 'alert-danger'} mt-12`}>
            {tgMsg}
          </div>
        )}
      </Section>
    </div>
  )
}
