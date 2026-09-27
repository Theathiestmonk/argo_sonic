import { useState, useEffect } from 'react'
import BatteryViewer from './components/BatteryViewer'

export default function BatteryTest() {
  const [battery, setBattery] = useState({ battery_percent: 15, charging: false, connected: true })

  useEffect(() => {
    const states = [
      { battery_percent: 15, charging: false, connected: true },  // Low - red
      { battery_percent: 35, charging: false, connected: true },  // Medium-low - orange
      { battery_percent: 60, charging: false, connected: true },  // Medium - yellow
      { battery_percent: 80, charging: false, connected: true },  // Good - light green
      { battery_percent: 95, charging: false, connected: true },  // Full - cyan
      { battery_percent: 85, charging: true, connected: true },   // Charging - green pulse
      { battery_percent: 75, charging: true, connected: true },   // Charging
      { battery_percent: 65, charging: true, connected: true },   // Charging
    ]

    let index = 0
    const interval = setInterval(() => {
      setBattery({ ...states[index % states.length], estimated_remaining_hours: 0, estimated_charge_remaining_hours: 0 })
      index++
    }, 2000)

    return () => clearInterval(interval)
  }, [])

  return (
    <div style={{
      background: '#1a1a2e',
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '40px'
    }}>
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '40px'
      }}>
        <div style={{
          fontSize: '24px',
          fontWeight: 'bold',
          color: '#e2b35c',
          textAlign: 'center'
        }}>
          🔋 Battery Visualization Test
        </div>

        <div style={{
          background: 'rgba(0,0,0,0.3)',
          padding: '40px',
          borderRadius: '16px',
          border: '1px solid rgba(255,255,255,0.1)'
        }}>
          <BatteryViewer battery={battery} />
        </div>

        <div style={{
          color: '#888',
          fontSize: '14px',
          textAlign: 'center',
          maxWidth: '400px'
        }}>
          <div>Current: <strong style={{color: '#e2b35c'}}>{battery.battery_percent}%</strong></div>
          <div>{battery.charging ? '🔌 Charging (Green Pulse)' : '🔋 Idle (White Blink)'}</div>
          <div style={{marginTop: '20px', fontSize: '12px', color: '#666'}}>
            Changes state every 2 seconds
          </div>
        </div>
      </div>
    </div>
  )
}
