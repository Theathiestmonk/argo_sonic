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

  const fillHeight = (displayPercent / 100) * 100

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 16,
        width: '100%'
      }}
    >
      {/* Short & Thick Cylindrical Battery */}
      <div
        style={{
          position: 'relative',
          width: '100px',
          height: '120px',
          background: 'linear-gradient(90deg, rgba(50,50,60,0.8) 0%, rgba(30,30,40,0.8) 50%, rgba(50,50,60,0.8) 100%)',
          border: '2px solid rgba(255,255,255,0.15)',
          borderRadius: '16px',
          overflow: 'hidden',
          boxShadow: `inset 0 2px 8px rgba(0,0,0,0.6), 0 8px 24px rgba(0,0,0,0.4), 0 0 20px ${energyColor}30`
        }}
      >
        {/* Battery Fill */}
        <div
          style={{
            position: 'absolute',
            bottom: 0,
            left: 0,
            right: 0,
            height: `${fillHeight}%`,
            background: `linear-gradient(180deg, ${energyColor}ff 0%, ${energyColor}cc 100%)`,
            borderRadius: '14px',
            transition: 'height 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)',
            boxShadow: `0 0 12px ${energyColor}80, inset 0 1px 3px rgba(255,255,255,0.2)`,
            animation: isCharging ? `pulse-fill 1.2s ease-in-out infinite` : 'none'
          }}
        />

        {/* Battery Terminal */}
        <div
          style={{
            position: 'absolute',
            top: -8,
            left: '50%',
            transform: 'translateX(-50%)',
            width: '30px',
            height: '12px',
            background: 'linear-gradient(180deg, #c0c0c0, #a0a0a0)',
            border: '1px solid rgba(255,255,255,0.4)',
            borderRadius: '0 0 4px 4px',
            boxShadow: '0 2px 4px rgba(0,0,0,0.4), inset 0 1px 1px rgba(255,255,255,0.3)'
          }}
        />
      </div>

      {/* Thunderbolt Indicator Below Battery */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          height: '50px',
          position: 'relative'
        }}
      >
        <svg
          width="40"
          height="50"
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

        @keyframes pulse-fill {
          0%, 100% {
            box-shadow: 0 0 12px ${energyColor}80, inset 0 1px 3px rgba(255,255,255,0.2);
            opacity: 1;
          }
          50% {
            box-shadow: 0 0 20px ${energyColor}a0, inset 0 1px 4px rgba(255,255,255,0.3);
            opacity: 0.9;
          }
        }
      `}</style>
    </div>
  )
}
