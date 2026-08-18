// Bottom-center live telemetry — drive speed/wheels from serial_bridge.py
// (/odom, /wheel_speeds), obstacle distances from safety_shield.py
// (/safety_shield/lidar_distance, /safety_shield/depth_distance) and
// serial_bridge.py's own per-corner ultrasonic (/us/front_left etc.,
// published directly, not routed through the shield). Always visible
// (unlike the teleop pad/transcript toggles) — this is passive readout,
// not an on-demand control.
const fmtSpeed = (v) => (v == null || !Number.isFinite(v)) ? '—' : `${v.toFixed(2)} m/s`
const fmtAngular = (v) => (v == null || !Number.isFinite(v)) ? '—' : `${v.toFixed(2)} rad/s`

// A sensor publishing +Infinity means "nothing in range", a real and
// common reading — distinct from null/undefined ("no message received
// yet"), so these get different display text rather than collapsing both
// to the same dash.
const fmtDist = (v) => {
  if (v == null) return '—'
  if (!Number.isFinite(v)) return 'Clear'
  return `${v.toFixed(2)}m`
}

// Approximate visual bands for a glance-color only — NOT the actual
// tuned safety_shield.py/nav2.yaml stop/slow thresholds (those differ per
// sensor and aren't worth duplicating here just for display coloring).
const distColor = (v) => {
  if (v == null || !Number.isFinite(v)) return 'var(--ok)'
  if (v < 0.4) return 'var(--danger)'
  if (v < 0.8) return 'var(--gold)'
  return 'var(--ok)'
}

function Tile({ label, value, color = '#fff' }) {
  return (
    <div style={{ minWidth: 74 }}>
      <div style={{ fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700, whiteSpace: 'nowrap' }}>
        {label}
      </div>
      <div style={{ fontSize: 15, fontWeight: 700, marginTop: 3, color, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
        {value}
      </div>
    </div>
  )
}

export default function TelemetryCard({ driveTelemetry = {}, sensorDistances = {} }) {
  return (
    <div
      className="glass-card"
      style={{
        position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)',
        zIndex: 100, padding: '14px 22px', maxWidth: '92vw', overflowX: 'auto',
      }}
    >
      <div style={{ display: 'flex', gap: 22, alignItems: 'stretch' }}>
        <div style={{ display: 'flex', gap: 16 }}>
          <Tile label="Speed" value={fmtSpeed(driveTelemetry.speed)} />
          <Tile label="Angular Vel" value={fmtAngular(driveTelemetry.angularVel)} />
          <Tile label="Left Wheel" value={fmtSpeed(driveTelemetry.wheelLeft)} />
          <Tile label="Right Wheel" value={fmtSpeed(driveTelemetry.wheelRight)} />
        </div>

        <div style={{ width: 1, background: 'var(--border-glass)' }} />

        <div style={{ display: 'flex', gap: 16 }}>
          <Tile label="Lidar" value={fmtDist(sensorDistances.lidar)} color={distColor(sensorDistances.lidar)} />
          <Tile label="Depth Cam" value={fmtDist(sensorDistances.depth)} color={distColor(sensorDistances.depth)} />
          <Tile label="US Front L" value={fmtDist(sensorDistances.usFrontLeft)} color={distColor(sensorDistances.usFrontLeft)} />
          <Tile label="US Front R" value={fmtDist(sensorDistances.usFrontRight)} color={distColor(sensorDistances.usFrontRight)} />
          <Tile label="US Back L" value={fmtDist(sensorDistances.usBackLeft)} color={distColor(sensorDistances.usBackLeft)} />
          <Tile label="US Back R" value={fmtDist(sensorDistances.usBackRight)} color={distColor(sensorDistances.usBackRight)} />
        </div>
      </div>
    </div>
  )
}
