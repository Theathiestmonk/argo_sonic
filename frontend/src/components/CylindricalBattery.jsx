import { useState, useEffect } from 'react'

export default function CylindricalBattery({ battery = {} }) {
  const batteryPercent = Math.round(battery.battery_percent || 0)
  const isCharging = battery.charging || false
  const [displayPercent, setDisplayPercent] = useState(batteryPercent)

  useEffect(() => {
    if (displayPercent !== batteryPercent) {
      const animationTime = 600
      const startPercent = displayPercent
      const startTime = Date.now()

      const animate = () => {
        const elapsed = Date.now() - startTime
        const progress = Math.min(elapsed / animationTime, 1)
        const newPercent = startPercent + (batteryPercent - startPercent) * progress

        setDisplayPercent(Math.round(newPercent))

        if (progress < 1) {
          requestAnimationFrame(animate)
        }
      }

      animate()
    }
  }, [batteryPercent, displayPercent])

  // Get color based on battery level
  const getColor = (percent) => {
    if (percent <= 20) return { bg: '#ff3333', glow: '#ff3333' }
    if (percent <= 40) return { bg: '#ff8c00', glow: '#ff8c00' }
    if (percent <= 60) return { bg: '#ffd700', glow: '#ffd700' }
    if (percent <= 80) return { bg: '#90ee90', glow: '#90ee90' }
    return { bg: '#3bf09b', glow: '#3bf09b' }
  }

  const getStatus = (percent) => {
    if (percent <= 20) return 'Critical'
    if (percent <= 40) return 'Low'
    if (percent <= 60) return 'Moderate'
    if (percent <= 80) return 'Good'
    return 'Excellent'
  }

  const colors = getColor(displayPercent)
  const segments = 8
  const filledSegments = Math.round((displayPercent / 100) * segments)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 12,
        height: '100%',
        justifyContent: 'center'
      }}
    >
      {/* Cylindrical Battery Container */}
      <div
        style={{
          position: 'relative',
          width: '80px',
          height: '140px',
          background: 'linear-gradient(90deg, rgba(50,50,60,0.8) 0%, rgba(30,30,40,0.8) 50%, rgba(50,50,60,0.8) 100%)',
          border: '2px solid rgba(255,255,255,0.15)',
          borderRadius: '12px',
          overflow: 'hidden',
          boxShadow: `inset 0 2px 8px rgba(0,0,0,0.6), 0 8px 24px rgba(0,0,0,0.4), 0 0 20px ${colors.glow}30`
        }}
      >
        {/* Cylindrical segments inside */}
        <div
          style={{
            position: 'absolute',
            bottom: 0,
            left: '50%',
            transform: 'translateX(-50%)',
            width: '90%',
            height: '85%',
            display: 'flex',
            flexDirection: 'column-reverse',
            gap: '3px',
            padding: '6px',
            borderRadius: '8px'
          }}
        >
          {Array.from({ length: segments }).map((_, i) => {
            const isFilled = i < filledSegments
            return (
              <div
                key={i}
                style={{
                  flex: 1,
                  width: '100%',
                  background: isFilled
                    ? `linear-gradient(90deg, ${colors.bg}ff 0%, ${colors.bg}dd 100%)`
                    : 'rgba(255,255,255,0.05)',
                  borderRadius: '2px',
                  border: `1px solid ${isFilled ? colors.bg + '60' : 'rgba(255,255,255,0.08)'}`,
                  boxShadow: isFilled
                    ? `0 0 6px ${colors.glow}80, inset 0 1px 2px rgba(255,255,255,0.2)`
                    : 'none',
                  transition: 'all 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)',
                  transitionDelay: `${i * 40}ms`,
                  animation: isCharging && isFilled ? `pulse-cylinder 1.2s ease-in-out infinite` : 'none',
                  animationDelay: `${i * 100}ms`
                }}
              />
            )
          })}
        </div>

        {/* Top Terminal */}
        <div
          style={{
            position: 'absolute',
            top: 0,
            left: '50%',
            transform: 'translateX(-50%)',
            width: '24px',
            height: '10px',
            background: 'linear-gradient(180deg, #c0c0c0, #a0a0a0)',
            border: '1px solid rgba(255,255,255,0.4)',
            borderRadius: '0 0 3px 3px',
            boxShadow: '0 2px 4px rgba(0,0,0,0.4), inset 0 1px 1px rgba(255,255,255,0.3)'
          }}
        />

        {/* Center percentage display */}
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            zIndex: 10,
            textAlign: 'center',
            pointerEvents: 'none',
            background: 'rgba(0, 0, 0, 0.5)',
            borderRadius: '6px',
            padding: '4px 8px',
            backdropFilter: 'blur(4px)'
          }}
        >
          <div
            style={{
              fontSize: '24px',
              fontWeight: 900,
              color: colors.glow,
              textShadow: `0 0 16px ${colors.glow}80, 0 2px 4px rgba(0,0,0,0.8)`,
              lineHeight: 1,
              letterSpacing: '-1px'
            }}
          >
            {displayPercent}%
          </div>
        </div>
      </div>

      {/* Info Section */}
      <div
        style={{
          textAlign: 'center',
          width: '100%'
        }}
      >
        {/* Health Status */}
        <div
          style={{
            fontSize: '9px',
            color: 'rgba(200,200,220,0.5)',
            textTransform: 'uppercase',
            letterSpacing: '0.02em',
            fontWeight: 600
          }}
        >
          {getStatus(displayPercent)}
        </div>

        {/* Connection Status */}
        <div
          style={{
            fontSize: '8px',
            color: 'rgba(200,200,220,0.3)',
            marginTop: 2,
            textTransform: 'uppercase',
            letterSpacing: '0.02em'
          }}
        >
          {battery.connected ? '● Connected' : '○ Offline'}
        </div>
      </div>

      {/* Animation keyframes */}
      <style>{`
        @keyframes pulse-cylinder {
          0%, 100% {
            opacity: 1;
            boxShadow: 0 0 6px ${colors.glow}80, inset 0 1px 2px rgba(255,255,255,0.2);
          }
          50% {
            opacity: 0.6;
            boxShadow: 0 0 12px ${colors.glow}a0, inset 0 1px 3px rgba(255,255,255,0.3);
          }
        }
      `}</style>
    </div>
  )
}
