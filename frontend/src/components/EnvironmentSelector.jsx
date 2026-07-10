import { useState } from 'react'

const ENVIRONMENTS = [
  {
    id: 'hotel',
    name: 'Hotel',
    desc: 'Lobby, corridors, rooms & service areas',
    accent: '#e2b35c',
    accentBg: 'rgba(226,179,92,0.08)',
    accentBorder: 'rgba(226,179,92,0.25)',
    svg: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect x="4" y="8" width="28" height="22" rx="3" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <rect x="8" y="14" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <rect x="15" y="14" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <rect x="22" y="14" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <rect x="13" y="22" width="10" height="8" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <path d="M4 8 L18 3 L32 8" stroke="currentColor" strokeWidth="1.5" fill="none"/>
      </svg>
    ),
  },
  {
    id: 'restaurant',
    name: 'Restaurant',
    desc: 'Dining area, kitchen & service counters',
    accent: '#ff8f6b',
    accentBg: 'rgba(255,143,107,0.08)',
    accentBorder: 'rgba(255,143,107,0.25)',
    svg: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect x="5" y="16" width="26" height="14" rx="3" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <line x1="12" y1="16" x2="12" y2="30" stroke="currentColor" strokeWidth="1.5"/>
        <line x1="18" y1="16" x2="18" y2="30" stroke="currentColor" strokeWidth="1.5"/>
        <line x1="24" y1="16" x2="24" y2="30" stroke="currentColor" strokeWidth="1.5"/>
        <path d="M10 7 C10 7 10 13 10 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
        <path d="M18 5 L18 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
        <path d="M26 7 C26 7 26 13 26 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
        <path d="M8 7 L8 10 Q10 12 12 10 L12 7" stroke="currentColor" strokeWidth="1.5" fill="none"/>
      </svg>
    ),
  },
  {
    id: 'indoor',
    name: 'Indoor',
    desc: 'Office, warehouse or general indoor space',
    accent: '#7fa8e8',
    accentBg: 'rgba(127,168,232,0.08)',
    accentBorder: 'rgba(127,168,232,0.25)',
    svg: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect x="4" y="6" width="28" height="24" rx="3" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <line x1="4" y1="14" x2="32" y2="14" stroke="currentColor" strokeWidth="1.4" strokeDasharray="3 2"/>
        <rect x="8" y="17" width="8" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <rect x="20" y="17" width="8" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <path d="M16 30 L16 24" stroke="currentColor" strokeWidth="1.5"/>
        <path d="M4 6 L18 2 L32 6" stroke="currentColor" strokeWidth="1.5"/>
      </svg>
    ),
  },
  {
    id: 'cafe',
    name: 'Cafe',
    desc: 'Small space, counter, seating & garden',
    accent: '#a87cf3',
    accentBg: 'rgba(168,124,243,0.08)',
    accentBorder: 'rgba(168,124,243,0.25)',
    svg: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <path d="M8 15 L8 28 Q8 30 10 30 L22 30 Q24 30 24 28 L24 15 Z" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <path d="M24 17 L27 17 Q31 17 31 21 Q31 25 27 25 L24 25" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <path d="M8 15 L24 15" stroke="currentColor" strokeWidth="1.5"/>
        <path d="M12 8 Q12 5 14 5 Q14 8 16 8 Q16 5 18 5 Q18 8 20 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/>
      </svg>
    ),
  },
  {
    id: 'hospital',
    name: 'Hospital',
    desc: 'Wards, reception, corridors & pharmacy',
    accent: '#3bf09b',
    accentBg: 'rgba(59,240,155,0.08)',
    accentBorder: 'rgba(59,240,155,0.25)',
    svg: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect x="4" y="8" width="28" height="22" rx="3" stroke="currentColor" strokeWidth="1.8" fill="none"/>
        <rect x="14" y="14" width="8" height="8" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/>
        <line x1="18" y1="16" x2="18" y2="20" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
        <line x1="16" y1="18" x2="20" y2="18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
        <rect x="8" y="22" width="5" height="8" rx="1" stroke="currentColor" strokeWidth="1.4" fill="none"/>
        <rect x="23" y="22" width="5" height="8" rx="1" stroke="currentColor" strokeWidth="1.4" fill="none"/>
        <path d="M4 8 L18 3 L32 8" stroke="currentColor" strokeWidth="1.5"/>
      </svg>
    ),
  },
]

export default function EnvironmentSelector({ onSelect, onNewMap }) {
  const [selected, setSelected] = useState(null)

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', animation: 'slideUp 0.35s ease' }}>
      <div style={{ marginBottom: 36 }}>
        <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 28, fontWeight: 800, letterSpacing: -0.5 }}>
          Where will Argo be working?
        </h2>
        <p style={{ color: 'var(--muted)', marginTop: 8, fontSize: 15 }}>
          Select the type of space so Argo can map it correctly.
        </p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 14, marginBottom: 32 }}>
        {ENVIRONMENTS.map(env => {
          const sel = selected?.id === env.id
          return (
            <button
              key={env.id}
              onClick={() => setSelected(env)}
              className="glass-card"
              style={{
                padding: '26px 22px',
                textAlign: 'left',
                cursor: 'pointer',
                borderColor: sel ? env.accentBorder : 'var(--border-glass)',
                boxShadow: sel
                  ? `0 0 0 2px ${env.accentBorder}, var(--shadow-fluid)`
                  : 'var(--shadow-fluid)',
                transform: sel ? 'translateY(-3px)' : undefined,
                transition: 'all 0.25s cubic-bezier(0.25,0.8,0.25,1)',
              }}
            >
              {/* Icon */}
              <div style={{
                width: 56, height: 56, borderRadius: 16, marginBottom: 18,
                background: sel ? env.accentBg : 'rgba(255,255,255,0.04)',
                border: `1px solid ${sel ? env.accentBorder : 'var(--border-glass)'}`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: sel ? env.accent : 'var(--muted)',
                transition: 'all 0.25s',
              }}>
                {env.svg}
              </div>

              <div style={{
                fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 17,
                color: sel ? '#fff' : 'rgba(255,255,255,0.85)',
                marginBottom: 6,
              }}>
                {env.name}
              </div>
              <div style={{ color: 'var(--muted)', fontSize: 12.5, lineHeight: 1.55 }}>
                {env.desc}
              </div>

              {sel && (
                <div style={{
                  position: 'absolute', top: 14, right: 14,
                  width: 24, height: 24, borderRadius: 8,
                  background: env.accentBg,
                  border: `1px solid ${env.accentBorder}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: env.accent, fontSize: 13, fontWeight: 800,
                }}>✓</div>
              )}
            </button>
          )
        })}
      </div>

      <div style={{ display: 'flex', gap: 12, justifyContent: 'space-between', alignItems: 'center' }}>
        <button
          onClick={onNewMap}
          className="btn-ghost"
          style={{ fontSize: 13 }}
        >
          Start fresh without a preset
        </button>

        <button
          disabled={!selected}
          onClick={() => selected && onSelect(selected)}
          className="btn-primary"
          style={{
            opacity: selected ? 1 : 0.4,
            cursor: selected ? 'pointer' : 'not-allowed',
            minWidth: 200,
          }}
        >
          {selected ? `Continue with ${selected.name}` : 'Select a space first'}
        </button>
      </div>
    </div>
  )
}
