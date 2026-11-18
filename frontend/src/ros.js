import ROSLIB from 'roslib'

// Singleton ROS connection shared across components.
class RosConnection {
  constructor() {
    this.ros = null
    this._cbs = { connect: [], close: [], error: [] }
    this._pubs = new Map()
  }

  connect(url = 'ws://localhost:9090') {
    this._pubs.clear()   // advertisements belong to the old socket
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

  actionClient(serverName, actionName) {
    if (!this.ros) return null
    return new ROSLIB.ActionClient({ ros: this.ros, serverName, actionName })
  }

  goal(actionClient, goalMessage) {
    return new ROSLIB.Goal({ actionClient, goalMessage })
  }

  // Publishers are kept (and advertised once) per topic. A throwaway Topic
  // per call publishes before DDS has matched it with the subscriber, so
  // one-shot messages like /goal_pose were silently dropped by Nav2.
  publish(name, messageType, msg) {
    const key = `${name}|${messageType}`
    let entry = this._pubs.get(key)
    if (!entry) {
      const topic = this.topic(name, messageType)
      if (!topic) return
      topic.advertise()
      entry = { topic, readyAt: Date.now() + 500 }
      this._pubs.set(key, entry)
    }
    const wait = entry.readyAt - Date.now()
    if (wait > 0) setTimeout(() => entry.topic.publish(msg), wait)
    else entry.topic.publish(msg)
  }
}

export const ros = new RosConnection()
