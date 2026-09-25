import { useState, useEffect } from 'react'

export default function BatteryViewer({ battery = {} }) {
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
    if (percent <= 20) return '#ff3333' // Red
    if (percent <= 40) return '#ff8c00' // Orange
    if (percent <= 60) return '#ffd700' // Gold
    if (percent <= 80) return '#90ee90' // Light green
    return '#3bf09b' // Cyan-green
  }

  const getStatus = (percent) => {
    if (percent <= 20) return 'Critical'
    if (percent <= 40) return 'Low'
    if (percent <= 60) return 'Moderate'
    if (percent <= 80) return 'Good'
    return 'Excellent'
  }

  const energyColor = getColor(displayPercent)
  const segments = 10
  const filledSegments = Math.round((displayPercent / 100) * segments)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        width: '100%',
        padding: '12px',
        background: 'rgba(255,255,255,0.02)',
        borderRadius: 8,
        border: '1px solid rgba(255,255,255,0.06)'
      }}
    >
      {/* Header with percentage and status */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 8
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 4
          }}
        >
          <span
            style={{
              fontSize: '20px',
              fontWeight: 800,
              color: energyColor,
              textShadow: `0 0 8px ${energyColor}40`
            }}
          >
            {displayPercent}%
          </span>
          <span
            style={{
              fontSize: '10px',
              color: 'rgba(200,200,220,0.6)',
              textTransform: 'uppercase',
              fontWeight: 600,
              letterSpacing: '0.05em'
            }}
          >
            {getStatus(displayPercent)}
          </span>
        </div>

        <div
          style={{
            fontSize: '11px',
            fontWeight: 700,
            padding: '4px 8px',
            borderRadius: 4,
            background: isCharging ? `${energyColor}20` : 'rgba(200,200,220,0.1)',
            color: isCharging ? energyColor : 'rgba(200,200,220,0.6)',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
            transition: 'all 0.3s ease-out'
          }}
        >
          {isCharging ? '🔌 Charging' : '🔋 Discharging'}
        </div>
      </div>

      {/* Vertical bar segments */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column-reverse',
          gap: 3,
          alignItems: 'center',
          width: '100%',
          height: '100px'
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
                maxWidth: '60px',
                background: isFilled
                  ? `linear-gradient(90deg, ${energyColor}ff 0%, ${energyColor}cc 100%)`
                  : 'rgba(255,255,255,0.05)',
                borderRadius: 3,
                border: `1px solid ${isFilled ? energyColor + '40' : 'rgba(255,255,255,0.1)'}`,
                boxShadow: isFilled ? `0 0 8px ${energyColor}60, inset 1px 1px 2px rgba(255,255,255,0.15)` : 'none',
                transition: 'all 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)',
                transitionDelay: `${i * 40}ms`,
                animation: isCharging && isFilled ? `pulse-bar-v 1.2s ease-in-out infinite` : 'none',
                animationDelay: `${i * 100}ms`
              }}
            />
          )
        })}
      </div>

      {/* Additional info */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: '9px',
          color: 'rgba(200,200,220,0.5)',
          textTransform: 'uppercase',
          letterSpacing: '0.02em',
          marginTop: 4
        }}
      >
        <span>Battery</span>
        <span>{battery.connected ? 'Connected' : 'Offline'}</span>
      </div>

      {/* Animation keyframes */}
      <style>{`
        @keyframes pulse-bar-v {
          0%, 100% {
            opacity: 1;
            boxShadow: 0 0 8px ${energyColor}60, inset 1px 1px 2px rgba(255,255,255,0.15);
          }
          50% {
            opacity: 0.7;
            boxShadow: 0 0 14px ${energyColor}80, inset 1px 1px 3px rgba(255,255,255,0.25);
          }
        }
      `}</style>
    </div>
  )
}
