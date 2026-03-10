import axios from 'axios'

const BASE = import.meta.env.VITE_API_BASE || ''

const api = axios.create({ baseURL: BASE })

// 自动附带 JWT token
api.interceptors.request.use(cfg => {
  const token = localStorage.getItem('token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

// 401 自动跳登录
api.interceptors.response.use(
  r => r,
  err => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export const authApi = {
  register: (username, password) =>
    api.post('/api/auth/register', { username, password }),
  login: (username, password) => {
    const form = new FormData()
    form.append('username', username)
    form.append('password', password)
    return api.post('/api/auth/login', form)
  },
}

export const keysApi = {
  save:        (data) => api.post('/api/keys/save', data),
  status:      ()     => api.get('/api/keys/status'),
  validate:    ()     => api.get('/api/keys/validate'),
  liveBalance: ()     => api.get('/api/keys/live-balance'),
  reset:       ()     => api.delete('/api/keys/reset'),
}

export const botApi = {
  start:  (strategyName) => api.post('/api/bot/start', strategyName ? { strategy_name: strategyName } : {}),
  stop:   () => api.post('/api/bot/stop'),
  status: () => api.get('/api/bot/status'),
  resume: () => api.post('/api/bot/risk/resume'),
}

export const dataApi = {
  trades:           (limit = 50) => api.get(`/api/data/trades?limit=${limit}`),
  balance:          (limit = 90) => api.get(`/api/data/balance?limit=${limit}`),
  strategies:       () => api.get('/api/data/strategies'),
  backtestOptions:  () => api.get('/api/data/backtest/options'),
  runBacktest:      (params) => api.post('/api/data/backtest/run', params),
  backtestResult:   () => api.get('/api/data/backtest/result'),
  backtestHistory:  () => api.get('/api/data/backtest/history'),
  backtestHistoryDetail: (id) => api.get(`/api/data/backtest/history/${id}`),
}

export const notifyApi = {
  saveTg:   (data) => api.post('/api/notify/telegram/save', data),
  tgStatus: ()     => api.get('/api/notify/telegram/status'),
  testTg:   ()     => api.post('/api/notify/telegram/test'),
  clearTg:  ()     => api.delete('/api/notify/telegram/clear'),
}

// 向后兼容旧名称
export const tgApi = notifyApi

export const userConfigApi = {
  get:   ()     => api.get('/api/user-config'),
  save:  (data) => api.post('/api/user-config/save', data),
  reset: ()     => api.delete('/api/user-config/reset'),
}

export default api
