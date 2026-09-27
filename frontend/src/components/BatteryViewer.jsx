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

  const energyColor = (() => {
    if (displayPercent <= 20) return '#ff3333'
    if (displayPercent <= 40) return '#ff8c00'
    if (displayPercent <= 60) return '#ffd700'
    if (displayPercent <= 80) return '#90ee90'
    return '#3bf09b'
  })()

  const segments = 10
  const filledSegments = Math.round((displayPercent / 100) * segments)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        width: '100%',
        padding: '16px',
        background: 'rgba(255,255,255,0.02)',
        borderRadius: 8,
        border: '1px solid rgba(255,255,255,0.06)'
      }}
    >
      {/* Thunderbolt Indicator */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          height: '80px',
          position: 'relative'
        }}
      >
        <svg
          width="60"
          height="80"
          viewBox="0 0 60 80"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          style={{
            filter: isCharging
              ? `drop-shadow(0 0 12px #00ff88) drop-shadow(0 0 24px #00ff8860)`
              : `drop-shadow(0 0 8px rgba(255,255,255,0.4))`,
            animation: isCharging ? 'pulse-glow 1.5s ease-in-out infinite' : 'blink-white 1s ease-in-out infinite',
            opacity: isCharging ? 1 : 0.8
          }}
        >
          {/* Thunderbolt shape */}
          <path
            d="M30 2 L12 38 L28 38 L10 78 L50 22 L34 22 L52 2 Z"
            fill={isCharging ? '#00ff88' : '#ffffff'}
            stroke={isCharging ? '#00ff88' : '#ffffff'}
            strokeWidth="0.5"
          />
        </svg>
      </div>

      {/* Battery Level Bars */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column-reverse',
          gap: 2,
          alignItems: 'center',
          width: '100%',
          height: '80px'
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
                maxWidth: '50px',
                background: isFilled
                  ? `linear-gradient(90deg, ${energyColor}ff 0%, ${energyColor}cc 100%)`
                  : 'rgba(255,255,255,0.05)',
                borderRadius: 2,
                border: `1px solid ${isFilled ? energyColor + '40' : 'rgba(255,255,255,0.1)'}`,
                boxShadow: isFilled ? `0 0 6px ${energyColor}50, inset 1px 1px 2px rgba(255,255,255,0.1)` : 'none',
                transition: 'all 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)',
                transitionDelay: `${i * 30}ms`,
                animation: isCharging && isFilled ? `pulse-bar 1.2s ease-in-out infinite` : 'none',
                animationDelay: `${i * 80}ms`
              }}
            />
          )
        })}
      </div>

      {/* Animation keyframes */}
      <style>{`
        @keyframes pulse-glow {
          0%, 100% {
            filter: drop-shadow(0 0 12px #00ff88) drop-shadow(0 0 24px #00ff8860);
            opacity: 1;
          }
          50% {
            filter: drop-shadow(0 0 20px #00ff88) drop-shadow(0 0 32px #00ff88aa);
            opacity: 0.9;
          }
        }

        @keyframes blink-white {
          0%, 100% {
            filter: drop-shadow(0 0 8px rgba(255,255,255,0.4));
            opacity: 0.8;
          }
          50% {
            filter: drop-shadow(0 0 4px rgba(255,255,255,0.2));
            opacity: 0.5;
          }
        }

        @keyframes pulse-bar {
          0%, 100% {
            opacity: 1;
            boxShadow: 0 0 6px ${energyColor}50, inset 1px 1px 2px rgba(255,255,255,0.1);
          }
          50% {
            opacity: 0.7;
            boxShadow: 0 0 12px ${energyColor}80, inset 1px 1px 3px rgba(255,255,255,0.2);
          }
        }
      `}</style>
    </div>
  )
}
