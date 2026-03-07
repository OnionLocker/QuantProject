import { useState, useEffect } from 'react'
import { keysApi, tgApi } from '../api'

/* ─── OKX API Key 卡片 ─────────────────────────────────────────────── */
function OkxKeyCard() {
  const [form, setForm] = useState({ api_key: '', secret: '', passphrase: '', is_simulate: false })
  const [status, setStatus] = useState(null)   // {configured, is_simulate, updated_at}
  const [msg, setMsg] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    keysApi.status().then(r => setStatus(r.data)).catch(() => {})
  }, [])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setMsg(null); setLoading(true)
    try {
      await keysApi.save(form)
      setMsg({ ok: true, text: '✅ API Key 已加密保存！' })
      setForm({ api_key: '', secret: '', passphrase: '', is_simulate: false })
      keysApi.status().then(r => setStatus(r.data)).catch(() => {})
    } catch (err) {
      setMsg({ ok: false, text: err.response?.data?.detail || '保存失败' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="card" style={{ maxWidth: 540 }}>
      <div style={{ fontWeight: 600, marginBottom: 16 }}>OKX API Key 配置</div>

      {status?.configured && (
        <div className="card" style={{
          background: 'rgba(39,174,96,.08)', borderColor: 'rgba(39,174,96,.3)',
          marginBottom: 16, fontSize: 13
        }}>
          ✅ 已配置 {status.is_simulate ? '（模拟盘）' : '（实盘）'} · 更新于 {status.updated_at}
        </div>
      )}

      <div className="card" style={{
        background: 'rgba(210,153,34,.08)', borderColor: 'rgba(210,153,34,.3)',
        marginBottom: 16, fontSize: 13
      }}>
        💡 API Key 在服务端以 AES 加密存储，明文不会被持久化。<br />
        请确保 Key 开启了「读取」和「交易」权限，并绑定了你的 IP。
      </div>

      <form onSubmit={handleSubmit}>
        {[
          ['api_key', 'API Key'],
          ['secret', 'Secret Key'],
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
        <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <input
            type="checkbox"
            id="sim"
            style={{ width: 'auto' }}
            checked={form.is_simulate}
            onChange={e => set('is_simulate', e.target.checked)}
          />
          <label htmlFor="sim" style={{ margin: 0, cursor: 'pointer' }}>使用模拟盘（Sandbox）</label>
        </div>
        <button type="submit" className="btn-primary" disabled={loading} style={{ marginTop: 4 }}>
          {loading ? '保存中...' : '保存 API Key'}
        </button>
        {msg && (
          <p style={{ marginTop: 12, color: msg.ok ? 'var(--green)' : 'var(--red)', fontSize: 13 }}>
            {msg.text}
          </p>
        )}
      </form>
    </div>
  )
}

/* ─── Telegram 通知配置卡片 ────────────────────────────────────────── */
function TelegramCard() {
  const [form, setForm] = useState({ tg_bot_token: '', tg_chat_id: '' })
  const [configured, setConfigured] = useState(false)
  const [saveMsg, setSaveMsg] = useState(null)
  const [testMsg, setTestMsg] = useState(null)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)

  useEffect(() => {
    tgApi.status().then(r => setConfigured(r.data.configured)).catch(() => {})
  }, [])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSave = async (e) => {
    e.preventDefault()
    setSaveMsg(null); setSaving(true)
    try {
      await tgApi.save(form.tg_bot_token, form.tg_chat_id)
      setSaveMsg({ ok: true, text: '✅ Telegram 配置已保存！' })
      setConfigured(true)
      setForm({ tg_bot_token: '', tg_chat_id: '' })
    } catch (err) {
      setSaveMsg({ ok: false, text: err.response?.data?.detail || '保存失败' })
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTestMsg(null); setTesting(true)
    try {
      await tgApi.test()
      setTestMsg({ ok: true, text: '✅ 测试消息已发送，请查看 Telegram！' })
    } catch (err) {
      setTestMsg({ ok: false, text: err.response?.data?.detail || '发送失败，请检查配置' })
    } finally {
      setTesting(false)
    }
  }

  const handleClear = async () => {
    if (!window.confirm('确认清除 Telegram 配置？')) return
    try {
      await tgApi.clear()
      setConfigured(false)
      setSaveMsg({ ok: true, text: '已清除配置' })
    } catch {
      setSaveMsg({ ok: false, text: '清除失败' })
    }
  }

  return (
    <div className="card" style={{ maxWidth: 540, marginTop: 24 }}>
      <div style={{ fontWeight: 600, marginBottom: 16 }}>Telegram 通知配置</div>

      {/* 配置状态标签 */}
      <div className="card" style={{
        background: configured ? 'rgba(39,174,96,.08)' : 'rgba(231,76,60,.08)',
        borderColor: configured ? 'rgba(39,174,96,.3)' : 'rgba(231,76,60,.3)',
        marginBottom: 16, fontSize: 13,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
      }}>
        <span>
          {configured
            ? '✅ Telegram 通知已启用，开仓/平仓/熔断均会推送到你的 Bot'
            : '⚠️ 尚未配置 Telegram，Bot 运行期间不会有消息推送'}
        </span>
        {configured && (
          <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
            <button
              onClick={handleTest}
              disabled={testing}
              className="btn-primary"
              style={{ padding: '4px 12px', fontSize: 12 }}
            >
              {testing ? '发送中...' : '📨 测试'}
            </button>
            <button
              onClick={handleClear}
              style={{
                padding: '4px 12px', fontSize: 12, cursor: 'pointer',
                background: 'transparent', border: '1px solid var(--red)',
                color: 'var(--red)', borderRadius: 6,
              }}
            >
              清除
            </button>
          </div>
        )}
      </div>

      {/* 测试结果 */}
      {testMsg && (
        <p style={{ marginBottom: 12, color: testMsg.ok ? 'var(--green)' : 'var(--red)', fontSize: 13 }}>
          {testMsg.text}
        </p>
      )}

      {/* 如何获取帮助提示 */}
      <div className="card" style={{
        background: 'rgba(52,152,219,.07)', borderColor: 'rgba(52,152,219,.3)',
        marginBottom: 16, fontSize: 12, lineHeight: 1.7,
      }}>
        <b>如何获取 Bot Token 和 Chat ID：</b><br />
        1. Telegram 搜索 <b>@BotFather</b>，发送 <code>/newbot</code> 创建 Bot，复制 Token<br />
        2. 搜索 <b>@userinfobot</b>，发送任意消息，获取你的 Chat ID（数字）<br />
        3. 先向你的 Bot 发一条消息，再填写下方配置
      </div>

      <form onSubmit={handleSave}>
        <div className="form-group">
          <label>Bot Token</label>
          <input
            type="password"
            value={form.tg_bot_token}
            onChange={e => set('tg_bot_token', e.target.value)}
            placeholder="例：7412345678:AAGxxxxxxxxxxxx"
            required
            autoComplete="off"
          />
        </div>
        <div className="form-group">
          <label>Chat ID</label>
          <input
            type="text"
            value={form.tg_chat_id}
            onChange={e => set('tg_chat_id', e.target.value)}
            placeholder="例：123456789（你的用户 ID 或群组 ID）"
            required
            autoComplete="off"
          />
        </div>
        <button type="submit" className="btn-primary" disabled={saving}>
          {saving ? '保存中...' : configured ? '更新 Telegram 配置' : '保存 Telegram 配置'}
        </button>
        {saveMsg && (
          <p style={{ marginTop: 12, color: saveMsg.ok ? 'var(--green)' : 'var(--red)', fontSize: 13 }}>
            {saveMsg.text}
          </p>
        )}
      </form>
    </div>
  )
}

/* ─── 页面入口 ─────────────────────────────────────────────────────── */
export default function SettingsPage() {
  return (
    <div>
      <h1 className="page-title">⚙️ 设置</h1>
      <OkxKeyCard />
      <TelegramCard />
    </div>
  )
}
