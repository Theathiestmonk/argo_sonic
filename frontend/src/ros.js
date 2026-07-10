import ROSLIB from 'roslib'

// Singleton ROS connection shared across components.
class RosConnection {
  constructor() {
    this.ros = null
    this._cbs = { connect: [], close: [], error: [] }
  }

  connect(url = 'ws://localhost:9090') {
    if (this.ros) {
      try { this.ros.close() } catch {}
    }
    this.ros = new ROSLIB.Ros({ url })
    this.ros.on('connection', () => this._emit('connect'))
    this.ros.on('close',      () => this._emit('close'))
    this.ros.on('error',      () => this._emit('error'))
    return this.ros
  }

  on(event, cb)  { this._cbs[event]?.push(cb) }
  off(event, cb) {
    if (this._cbs[event])
      this._cbs[event] = this._cbs[event].filter(x => x !== cb)
  }
  _emit(event) { this._cbs[event]?.forEach(cb => cb()) }

  isConnected() { return this.ros?.isConnected === true }

  topic(name, messageType, opts = {}) {
    if (!this.ros) return null
    return new ROSLIB.Topic({ ros: this.ros, name, messageType, ...opts })
  }

  service(name, serviceType) {
    if (!this.ros) return null
    return new ROSLIB.Service({ ros: this.ros, name, serviceType })
  }

  publish(name, messageType, msg) {
    const t = this.topic(name, messageType)
    t?.publish(msg)
  }
}

export const ros = new RosConnection()
