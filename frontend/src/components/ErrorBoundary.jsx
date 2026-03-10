import { Component } from 'react'
import { AlertTriangle, RotateCcw } from 'lucide-react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    console.error('[ErrorBoundary]', error, info?.componentStack)
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          justifyContent: 'center', minHeight: 320, gap: 16, padding: 40,
        }}>
          <div style={{
            width: 56, height: 56, borderRadius: '50%',
            background: 'var(--red-dim)', display: 'flex',
            alignItems: 'center', justifyContent: 'center',
          }}>
            <AlertTriangle size={24} color="var(--red)" />
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>
              页面渲染出错
            </div>
            <div style={{ fontSize: 12, color: 'var(--muted)', maxWidth: 400 }}>
              {String(this.state.error?.message || this.state.error || '未知错误')}
            </div>
          </div>
          <button className="btn-primary btn-sm" onClick={this.handleReset}
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <RotateCcw size={13} /> 重新加载
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
