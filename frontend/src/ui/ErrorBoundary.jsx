import { Component } from 'react'

// Keeps one failing widget (e.g. the 3D viewer in a browser without WebGL) from unmounting the whole dashboard.
export default class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(error) { return { error } }
  componentDidCatch(error) { console.error('[ErrorBoundary]', error); this.props.onError?.(error) }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="a-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeContent: 'center' }}>
        <b>{this.props.title || 'Something went wrong'}</b>
        {this.props.message || String(this.state.error?.message || '')}
        {this.props.onReset && <div style={{ marginTop: 12 }}><button className="a-btn gold" onClick={() => { this.setState({ error: null }); this.props.onReset() }}>{this.props.resetLabel || 'Try again'}</button></div>}
      </div>
    )
  }
}
