import { useState, useEffect } from 'react'

export default function BatteryViewer({ battery = {} }) {
  const isConnected = battery.connected !== false
  const batteryPercent = Math.round(battery.battery_percent || 0)
  const isCharging = battery.charging || false
  const [displayPercent, setDisplayPercent] = useState(batteryPercent)

  useEffect(() => {
    if (displayPercent !== batteryPercent) {
      const animationTime = 400
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

  const getBarColor = (percent) => {
    if (isCharging) return '#4AC925' // Dark green when charging
    if (percent >= 100) return '#4ADE80' // Full
    if (percent <= 20) return '#FF5C5C' // Critical - red
    if (percent <= 40) return '#F5B942' // Low - amber
    return '#F5F5F5' // Normal - off-white
  }

  const barColor = getBarColor(displayPercent)

  const segments = 8
  const filledSegments = Math.round((displayPercent / 100) * segments)

  const formatTime = (hours) => {
    if (!hours) return '—'
    if (hours < 1) return Math.round(hours * 60) + 'm'
    if (hours < 2) return Math.round(hours * 10) / 10 + 'h'
    const h = Math.floor(hours)
    const m = Math.round((hours - h) * 60)
    return m === 0 ? h + 'h' : h + 'h ' + m + 'm'
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 12,
        width: '100%'
      }}
    >
      {/* Premium Battery Container */}
      <div
        style={{
          position: 'relative',
          width: '85px',
          height: '160px'
        }}
      >
        {/* Blinking Indicator - Top-right on battery when NOT charging */}
        {!isCharging && (
          <div
            style={{
              position: 'absolute',
              top: '-2px',
              right: '0px',
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              background: barColor,
              boxShadow: `0 0 8px ${barColor}`,
              animation: 'status-blink 1.5s ease-in-out infinite',
              zIndex: 10
            }}
          />
        )}
        {/* Battery Terminal (Top) - Minimal metal look */}
        <div
          style={{
            position: 'absolute',
            top: -2,
            left: '50%',
            transform: 'translateX(-50%)',
            width: '24px',
            height: '12px',
            background: 'rgba(200,200,200,0.3)',
            border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: '3px 3px 0 0',
            boxShadow: 'inset 0 1px 2px rgba(255,255,255,0.1)'
          }}
        />

        {/* Battery Housing - Premium translucent charcoal */}
        <div
          style={{
            position: 'absolute',
            top: '10px',
            left: 0,
            right: 0,
            height: '145px',
            background: 'rgba(30, 30, 35, 0.6)',
            backdropFilter: 'blur(8px)',
            border: '1px solid rgba(255,255,255,0.08)',
            borderRadius: '8px 8px 12px 12px',
            padding: '8px',
            display: 'flex',
            flexDirection: 'column-reverse',
            gap: '4px',
            overflow: 'hidden',
            boxShadow: 'inset 0 1px 8px rgba(0,0,0,0.4), 0 8px 16px rgba(0,0,0,0.3)',
            justifyContent: 'center',
            alignItems: 'center'
          }}
        >
          {/* Loading Spinner - When no BMS data */}
          {!isConnected && (
            <div style={{
              position: 'absolute',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 15
            }}>
              <svg
                width="40"
                height="40"
                viewBox="0 0 100 100"
                style={{
                  animation: 'spin-battery 2s linear infinite'
                }}
              >
                {Array.from({ length: 12 }).map((_, i) => {
                  const angle = (i * 360) / 12
                  return (
                    <line
                      key={i}
                      x1="50"
                      y1="18"
                      x2="50"
                      y2="32"
                      stroke="rgba(255,255,255,0.5)"
                      strokeWidth="3"
                      strokeLinecap="round"
                      transform={`rotate(${angle} 50 50)`}
                      style={{
                        animation: `pulse-bar 1.2s ease-in-out infinite`,
                        animationDelay: `${i * 0.1}s`,
                        opacity: 0.4 + (i * 0.05)
                      }}
                    />
                  )
                })}
              </svg>
            </div>
          )}

          {/* Thunderbolt Icon - Inside battery when charging (Bright yellow) */}
          {isConnected && isCharging && (
            <svg
              width="20"
              height="28"
              viewBox="0 0 60 80"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              style={{
                position: 'absolute',
                filter: 'drop-shadow(0 0 8px #FFFF3380) drop-shadow(0 0 16px #FFFF3360)',
                animation: 'premium-glow 1.8s ease-in-out infinite',
                opacity: 1,
                zIndex: 5
              }}
            >
              <path
                d="M30 2 L12 38 L28 38 L10 78 L50 22 L34 22 L52 2 Z"
                fill="#FFFF33"
                stroke="#FFFF33"
                strokeWidth="0.5"
              />
            </svg>
          )}
          {/* Segmented Bars - Premium animation (only when connected) */}
          {isConnected && Array.from({ length: segments }).map((_, i) => {
            const isFilled = i < filledSegments
            return (
              <div
                key={i}
                style={{
                  flex: 1,
                  width: '100%',
                  background: isFilled
                    ? barColor
                    : 'rgba(255,255,255,0.03)',
                  borderRadius: '2px',
                  border: `0.5px solid ${isFilled ? `${barColor}40` : 'rgba(255,255,255,0.05)'}`,
                  boxShadow: isFilled
                    ? isCharging
                      ? `0 0 6px ${barColor}70, inset 0 1px 1px rgba(255,255,255,0.2)`
                      : `0 0 3px ${barColor}50, inset 0 1px 1px rgba(255,255,255,0.15)`
                    : 'none',
                  transition: 'all 400ms cubic-bezier(0.25, 0.46, 0.45, 0.94)',
                  transitionDelay: isCharging ? `${i * 80}ms` : '0ms',
                  animationName: isCharging && isFilled
                    ? 'premium-pulse'
                    : displayPercent <= 20 && isFilled
                    ? 'critical-pulse'
                    : displayPercent <= 40 && isFilled
                    ? 'low-pulse'
                    : 'none',
                  animationDuration: isCharging && isFilled
                    ? '1.8s'
                    : displayPercent <= 20 && isFilled
                    ? '1.2s'
                    : displayPercent <= 40 && isFilled
                    ? '2s'
                    : '0s',
                  animationTimingFunction: 'ease-in-out',
                  animationIterationCount: 'infinite',
                  animationDelay: `${i * 60}ms`,
                  opacity: isFilled ? 1 : 0.7
                }}
              />
            )
          })}
        </div>
      </div>


      {/* Battery Info - Format: "Battery: 15% | Runtime: 30m" or "Connecting..." */}
      {isConnected ? (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 6,
            textAlign: 'center',
            width: '100%'
          }}
        >
          {/* Line 1: Battery Percentage */}
          <div
            style={{
              fontSize: '13px',
              fontWeight: '600',
              color: 'rgba(255,255,255,0.7)',
              letterSpacing: '0.03em'
            }}
          >
            Battery: <span style={{ color: barColor, fontWeight: '700' }}>{displayPercent}%</span>
          </div>

          {/* Line 2: Runtime / Charging Time */}
          <div
            style={{
              fontSize: '13px',
              fontWeight: '600',
              color: 'rgba(255,255,255,0.7)',
              letterSpacing: '0.03em'
            }}
          >
            {isCharging ? 'Charging: ' : 'Runtime: '}
            <span style={{ color: barColor, fontWeight: '700' }}>
              {isCharging
                ? battery.estimated_charge_remaining_hours
                  ? formatTime(battery.estimated_charge_remaining_hours)
                  : '—'
                : battery.estimated_remaining_hours
                ? formatTime(battery.estimated_remaining_hours)
                : '—'}
            </span>
          </div>
        </div>
      ) : (
        <div
          style={{
            fontSize: '13px',
            fontWeight: '600',
            color: 'rgba(255,255,255,0.5)',
            letterSpacing: '0.03em',
            animation: 'blink-text 1.5s ease-in-out infinite'
          }}
        >
          Connecting Battery...
        </div>
      )}

      {/* Premium Animation Keyframes */}
      <style>{`
        @keyframes spin-battery {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }

        @keyframes pulse-bar {
          0%, 100% {
            opacity: 0.4;
            stroke-width: 3;
          }
          50% {
            opacity: 1;
            stroke-width: 3.5;
          }
        }

        @keyframes blink-text {
          0%, 100% {
            opacity: 0.5;
          }
          50% {
            opacity: 1;
          }
        }

        @keyframes premium-pulse {
          0%, 100% {
            opacity: 1;
            box-shadow: 0 0 6px #7CFF6B70, inset 0 1px 1px rgba(255,255,255,0.2);
          }
          50% {
            opacity: 0.8;
            box-shadow: 0 0 10px #7CFF6B90, inset 0 1px 2px rgba(255,255,255,0.25);
          }
        }

        @keyframes critical-pulse {
          0%, 100% {
            opacity: 1;
            box-shadow: 0 0 4px #FF5C5C50, inset 0 1px 1px rgba(255,255,255,0.1);
          }
          50% {
            opacity: 0.75;
            box-shadow: 0 0 8px #FF5C5C70, inset 0 1px 1px rgba(255,255,255,0.15);
          }
        }

        @keyframes low-pulse {
          0%, 100% {
            opacity: 1;
            box-shadow: 0 0 4px #F5B94250, inset 0 1px 1px rgba(255,255,255,0.1);
          }
          50% {
            opacity: 0.85;
            box-shadow: 0 0 6px #F5B94270, inset 0 1px 1px rgba(255,255,255,0.15);
          }
        }

        @keyframes premium-glow {
          0%, 100% {
            filter: drop-shadow(0 0 8px #7CFF6B60) drop-shadow(0 0 16px #7CFF6B30);
            opacity: 0.95;
          }
          50% {
            filter: drop-shadow(0 0 12px #7CFF6B80) drop-shadow(0 0 20px #7CFF6B50);
            opacity: 1;
          }
        }

        @keyframes status-blink {
          0%, 100% {
            opacity: 1;
            box-shadow: 0 0 6px currentColor;
            transform: scale(1);
          }
          50% {
            opacity: 0.4;
            box-shadow: 0 0 3px currentColor;
            transform: scale(0.85);
          }
        }
      `}</style>
    </div>
  )
}
