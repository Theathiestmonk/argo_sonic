import { useState, useEffect } from 'react'
import MapPreviewThumb from './MapPreviewThumb'
import MapWaypointViewer from './MapWaypointViewer'

export default function SettingsPanel({ launcherUrl, selectedMap, onSelectMap, onClose, showToast }) {
  const [maps, setMaps] = useState(null) // null = loading

  useEffect(() => {
    fetch(`${launcherUrl}/maps`)
      .then(r => r.json())
      .then(d => setMaps(d.maps ?? []))
      .catch(() => setMaps([]))
  }, [launcherUrl])

  return (
    <div style={{ maxWidth: 1180, margin: '0 auto', animation: 'slideUp 0.35s ease' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 28 }}>
        <div>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 28, fontWeight: 800, letterSpacing: -0.5 }}>
            Settings
          </h2>
          <p style={{ color: 'var(--muted)', marginTop: 8, fontSize: 15 }}>
            Pick which saved map's tables and waypoints to work with.
          </p>
        </div>
        <button onClick={onClose} className="btn-ghost" style={{ fontSize: 13 }}>Close</button>
      </div>

      {maps === null && (
        <div style={{ textAlign: 'center', color: 'var(--muted)', padding: '40px 0' }}>Loading maps…</div>
      )}

      {maps !== null && maps.length === 0 && (
        <div style={{ textAlign: 'center', color: 'var(--muted)', padding: '40px 0' }}>
          No saved maps found on the robot yet.
        </div>
      )}

      {maps !== null && maps.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 14 }}>
          {maps.map(name => {
            const sel = selectedMap === name
            return (
              <button
                key={name}
                onClick={() => onSelectMap(name)}
                className="glass-card"
                style={{
                  padding: 18, textAlign: 'left', cursor: 'pointer',
                  borderColor: sel ? 'rgba(226,179,92,0.42)' : 'var(--border-glass)',
                  boxShadow: sel ? '0 0 0 2px rgba(226,179,92,0.42), var(--shadow-fluid)' : 'var(--shadow-fluid)',
                  transform: sel ? 'translateY(-3px)' : undefined,
                  transition: 'all 0.25s cubic-bezier(0.25,0.8,0.25,1)',
                  position: 'relative',
                }}
              >
                <MapPreviewThumb launcherUrl={launcherUrl} mapName={name} width="100%" height={130} />
                <div style={{
                  fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 15,
                  color: sel ? '#fff' : 'rgba(255,255,255,0.85)', marginTop: 14,
                }}>
                  {name}
                </div>

                {sel && (
                  <div style={{
                    position: 'absolute', top: 14, right: 14,
                    width: 24, height: 24, borderRadius: 8,
                    background: 'rgba(226,179,92,0.18)', border: '1px solid rgba(226,179,92,0.42)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--gold-bright)', fontSize: 13, fontWeight: 800,
                  }}>✓</div>
                )}
              </button>
            )
          })}
        </div>
      )}

      {selectedMap && maps?.includes(selectedMap) && (
        <div style={{ marginTop: 32 }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 17, marginBottom: 4 }}>
            {selectedMap} — waypoints
          </div>
          <p style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 16 }}>
            Click a pin (or a row on the right) to select it, then use the pencil to rename it.
          </p>
          <MapWaypointViewer launcherUrl={launcherUrl} mapName={selectedMap} showToast={showToast} />
        </div>
      )}
    </div>
  )
}
