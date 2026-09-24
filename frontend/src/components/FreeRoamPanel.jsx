import { useState, useEffect, useRef } from 'react'
import { ros } from '../ros'

// Generate random goal coordinates in free space within the map
function generateRandomGoal(mapData, robotPose, minDistFromRobot = 0.5) {
  if (!mapData || !robotPose) return null

  const { width, height, resolution, origin, data } = mapData
  const { x: rx, y: ry } = robotPose

  // Try up to 100 times to find a valid goal location
  for (let attempt = 0; attempt < 100; attempt++) {
    // Random position in map range
    const col = Math.floor(Math.random() * width)
    const row = Math.floor(Math.random() * height)

    // Convert to world coordinates
    const wx = origin.x + col * resolution
    const wy = origin.y + (height - 1 - row) * resolution

    // Check if this position is in free space (value < 25) and not too close to current pose
    const mapIdx = row * width + col
    const isFree = data[mapIdx] < 25

    // Check distance from robot
    const dist = Math.sqrt((wx - rx) ** 2 + (wy - ry) ** 2)
    const distValid = dist >= minDistFromRobot

    if (isFree && distValid) {
      return { x: wx, y: wy }
    }
  }

  return null
}

// Frontier-based exploration: find edges between explored and unexplored areas
function findFrontierGoals(mapData, robotPose, count = 3) {
  if (!mapData || !robotPose) return []

  const { width, height, resolution, origin, data } = mapData
  const goals = []
  const visited = new Set()

  // Find frontier cells (free space adjacent to unknown/unexplored)
  for (let row = 1; row < height - 1; row++) {
    for (let col = 1; col < width - 1; col++) {
      const idx = row * width + col
      const cellVal = data[idx]

      // Current cell is free space
      if (cellVal >= 25 && cellVal !== -1) continue

      // Check 8 neighbors
      let hasOccupiedNeighbor = false
      for (let dr = -1; dr <= 1; dr++) {
        for (let dc = -1; dc <= 1; dc++) {
          if (dr === 0 && dc === 0) continue
          const nIdx = (row + dr) * width + (col + dc)
          const nVal = data[nIdx]
          if (nVal > 25 || nVal === -1) {
            hasOccupiedNeighbor = true
            break
          }
        }
        if (hasOccupiedNeighbor) break
      }

      if (hasOccupiedNeighbor) {
        const key = `${row},${col}`
        if (!visited.has(key)) {
          visited.add(key)
          const wx = origin.x + col * resolution
          const wy = origin.y + (height - 1 - row) * resolution
          goals.push({ x: wx, y: wy })
        }
      }
    }
  }

  // Sort by distance to robot and return top N
  const { x: rx, y: ry } = robotPose
  goals.sort((a, b) => {
    const dA = Math.sqrt((a.x - rx) ** 2 + (a.y - ry) ** 2)
    const dB = Math.sqrt((b.x - rx) ** 2 + (b.y - ry) ** 2)
    return dA - dB
  })

  return goals.slice(0, count)
}

export default function FreeRoamPanel({
  mapData, robotPose, connected, showToast, launcherUrl, mapName
}) {
  const [roamActive, setRoamActive] = useState(false)
  const [roamPaused, setRoamPaused] = useState(false)
  const [currentGoal, setCurrentGoal] = useState(null)
  const [navigationStatus, setNavigationStatus] = useState('idle')
  const [roamStats, setRoamStats] = useState({ goalsReached: 0, goalsFailed: 0, totalDistance: 0 })
  const [explorationMode, setExplorationMode] = useState('random') // 'random' or 'frontier'

  const roamIntervalRef = useRef(null)
  const navStatusPollRef = useRef(null)
  const currentGoalRef = useRef(null)

  // Poll navigation status
  useEffect(() => {
    if (!roamActive || roamPaused) return

    const pollNavStatus = async () => {
      try {
        const response = await fetch(`${launcherUrl}/nav/goto/status`)
        const status = await response.json()

        setNavigationStatus(status.running ? 'navigating' : 'idle')

        // If navigation finished, try to generate next goal
        if (!status.running && currentGoalRef.current) {
          // Navigation completed (success or failure)
          const wasSuccess = status.phase === 'done'
          if (wasSuccess) {
            setRoamStats(prev => ({
              ...prev,
              goalsReached: prev.goalsReached + 1,
            }))
            showToast('Goal reached! Finding next destination...', 'ok')
          } else if (status.phase === 'failed') {
            setRoamStats(prev => ({
              ...prev,
              goalsFailed: prev.goalsFailed + 1,
            }))
            showToast('Goal unreachable, finding alternative...', 'info')
          }
          currentGoalRef.current = null
          setCurrentGoal(null)
        }
      } catch (err) {
        console.error('Failed to poll nav status:', err)
      }
    }

    navStatusPollRef.current = setInterval(pollNavStatus, 1000)
    return () => clearInterval(navStatusPollRef.current)
  }, [roamActive, roamPaused, launcherUrl, showToast])

  // Generate and send next goal
  useEffect(() => {
    if (!roamActive || roamPaused || navigationStatus === 'navigating' || !mapData || !robotPose) return

    const generateAndNavigate = async () => {
      let nextGoal = null

      if (explorationMode === 'frontier') {
        const frontierGoals = findFrontierGoals(mapData, robotPose, 1)
        if (frontierGoals.length > 0) {
          nextGoal = frontierGoals[0]
        }
      }

      // Fallback to random if no frontier found or random mode
      if (!nextGoal) {
        nextGoal = generateRandomGoal(mapData, robotPose, 0.5)
      }

      if (!nextGoal) {
        showToast('Could not find safe goal location', 'danger')
        return
      }

      // Send navigation goal
      try {
        const mapToUse = mapName || 'office_map'
        const response = await fetch(`${launcherUrl}/nav/goto`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            destination: `roam_${Math.random().toString(36).substr(2, 9)}`,
            map: mapToUse,
          }),
        })

        const data = await response.json()
        if (data.ok) {
          setCurrentGoal(nextGoal)
          currentGoalRef.current = nextGoal
          setNavigationStatus('navigating')
        } else {
          showToast(`Navigation error: ${data.error}`, 'danger')
        }
      } catch (err) {
        showToast('Failed to send navigation goal', 'danger')
        console.error('Navigation error:', err)
      }
    }

    roamIntervalRef.current = setTimeout(generateAndNavigate, 500)
    return () => clearTimeout(roamIntervalRef.current)
  }, [roamActive, roamPaused, navigationStatus, mapData, robotPose, explorationMode, launcherUrl, mapName, showToast])

  const startRoam = () => {
    if (!connected) {
      showToast('Not connected to Argo', 'danger')
      return
    }
    setRoamActive(true)
    setRoamPaused(false)
    setRoamStats({ goalsReached: 0, goalsFailed: 0, totalDistance: 0 })
    showToast(`Free roam started (${explorationMode} mode)`, 'ok')
  }

  const pauseRoam = () => {
    setRoamPaused(true)
    showToast('Free roam paused', 'info')
  }

  const resumeRoam = () => {
    setRoamPaused(false)
    showToast('Free roam resumed', 'ok')
  }

  const stopRoam = () => {
    setRoamActive(false)
    setRoamPaused(false)
    setCurrentGoal(null)
    currentGoalRef.current = null
    setNavigationStatus('idle')
    showToast('Free roam stopped', 'info')
  }

  return (
    <div style={{
      padding: 14,
      background: 'rgba(147,112,219,0.05)',
      border: '1px solid rgba(147,112,219,0.2)',
      borderRadius: 12,
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      flex: 1,
      minHeight: 0,
      overflow: 'auto',
    }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 8,
      }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: '#fff', textTransform: 'uppercase', letterSpacing: '0.03em' }}>Free Roam</div>
        <span style={{
          fontSize: 10,
          fontWeight: 700,
          padding: '3px 10px',
          borderRadius: 99,
          background: roamActive ? 'rgba(147,112,219,0.15)' : 'rgba(255,255,255,0.04)',
          border: `1px solid ${roamActive ? 'rgba(147,112,219,0.4)' : 'rgba(255,255,255,0.08)'}`,
          color: roamActive ? '#b98cf5' : 'var(--muted)',
        }}>
          {roamActive ? (roamPaused ? 'Paused' : 'Active') : 'Idle'}
        </span>
      </div>

      {/* Mode selector */}
      {!roamActive && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 2 }}>
          <button
            onClick={() => setExplorationMode('random')}
            style={{
              flex: 1,
              padding: '8px 10px',
              borderRadius: 8,
              fontSize: 10,
              fontWeight: 600,
              background: explorationMode === 'random' ? 'rgba(147,112,219,0.15)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${explorationMode === 'random' ? 'rgba(147,112,219,0.3)' : 'rgba(255,255,255,0.08)'}`,
              color: explorationMode === 'random' ? '#b98cf5' : 'var(--muted)',
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
          >
            Random
          </button>
          <button
            onClick={() => setExplorationMode('frontier')}
            style={{
              flex: 1,
              padding: '8px 10px',
              borderRadius: 8,
              fontSize: 10,
              fontWeight: 600,
              background: explorationMode === 'frontier' ? 'rgba(147,112,219,0.15)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${explorationMode === 'frontier' ? 'rgba(147,112,219,0.3)' : 'rgba(255,255,255,0.08)'}`,
              color: explorationMode === 'frontier' ? '#b98cf5' : 'var(--muted)',
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
          >
            Frontier
          </button>
        </div>
      )}

      {/* Stats */}
      {roamActive && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 10,
          fontSize: 12,
          color: 'var(--muted)',
        }}>
          <div style={{ padding: '10px', background: 'rgba(255,255,255,0.03)', borderRadius: 8 }}>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.5)', marginBottom: 2 }}>Reached</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--ok)' }}>{roamStats.goalsReached}</div>
          </div>
          <div style={{ padding: '10px', background: 'rgba(255,255,255,0.03)', borderRadius: 8 }}>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.5)', marginBottom: 2 }}>Failed</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--danger)' }}>{roamStats.goalsFailed}</div>
          </div>
        </div>
      )}

      {/* Status indicator */}
      {roamActive && (
        <div style={{
          padding: '12px 14px',
          background: 'rgba(255,255,255,0.03)',
          borderRadius: 10,
          fontSize: 12,
          color: 'var(--muted)',
        }}>
          {navigationStatus === 'navigating' && currentGoal && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{
                width: 10,
                height: 10,
                borderRadius: '50%',
                border: '2px solid #b98cf5',
                borderTopColor: 'transparent',
                animation: 'spin-slow 0.8s linear infinite',
              }} />
              <div>
                <div style={{ fontSize: 11, color: '#b98cf5', fontWeight: 600 }}>Navigating</div>
                <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>
                  To ({currentGoal.x.toFixed(1)}, {currentGoal.y.toFixed(1)})
                </div>
              </div>
            </div>
          )}
          {navigationStatus === 'idle' && roamActive && !roamPaused && (
            <div>Searching for next destination...</div>
          )}
          {roamPaused && (
            <div style={{ color: 'var(--gold-bright)' }}>Paused</div>
          )}
        </div>
      )}

      {/* Controls */}
      <div style={{ display: 'flex', gap: 6, marginTop: 'auto' }}>
        {!roamActive ? (
          <button
            onClick={startRoam}
            disabled={!connected}
            style={{
              flex: 1,
              padding: '9px 10px',
              borderRadius: 8,
              fontSize: 11,
              fontWeight: 700,
              background: connected ? 'rgba(147,112,219,0.15)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${connected ? 'rgba(147,112,219,0.3)' : 'rgba(255,255,255,0.08)'}`,
              color: connected ? '#b98cf5' : 'var(--muted)',
              cursor: connected ? 'pointer' : 'not-allowed',
              transition: 'all 0.2s',
            }}
          >
            Start Free Roam
          </button>
        ) : (
          <>
            {!roamPaused ? (
              <button
                onClick={pauseRoam}
                style={{
                  flex: 1,
                  padding: '9px 10px',
                  borderRadius: 8,
                  fontSize: 11,
                  fontWeight: 700,
                  background: 'rgba(226,179,92,0.1)',
                  border: '1px solid rgba(226,179,92,0.25)',
                  color: 'var(--gold-bright)',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
              >
                Pause
              </button>
            ) : (
              <button
                onClick={resumeRoam}
                style={{
                  flex: 1,
                  padding: '9px 10px',
                  borderRadius: 8,
                  fontSize: 11,
                  fontWeight: 700,
                  background: 'rgba(59,240,155,0.1)',
                  border: '1px solid rgba(59,240,155,0.25)',
                  color: 'var(--ok)',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
              >
                Resume
              </button>
            )}
            <button
              onClick={stopRoam}
              style={{
                padding: '9px 12px',
                borderRadius: 8,
                fontSize: 11,
                fontWeight: 700,
                background: 'rgba(255,94,94,0.1)',
                border: '1px solid rgba(255,94,94,0.25)',
                color: 'var(--danger)',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
            >
              Stop
            </button>
          </>
        )}
      </div>
    </div>
  )
}
