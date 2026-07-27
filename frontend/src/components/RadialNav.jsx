import { useState, useRef, useEffect, useCallback } from 'react'

const MAX_VISIBLE = 4        // window size — see radial-nav.js's identical constant
const MOBILE_BREAKPOINT = 700
const SWIPE_THRESHOLD = 32
const SWIPE_MOVE_GUARD = 6

const MenuIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg>
)
const CloseIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><line x1="5" y1="5" x2="19" y2="19"/><line x1="19" y1="5" x2="5" y2="19"/></svg>
)

// Faithful React port of frontend/public/radial-nav.js — same windowed cyclic
// page list, arc geometry, keyboard cursor, touch-swipe, decorative dashed
// track and glow/focus states. Kept behaviorally identical on purpose so this
// matches the original exactly rather than reinterpreting it.
export default function RadialNav({ pages, activePage: activePageProp }) {
  const [open, setOpen] = useState(false)
  const [focusIndex, setFocusIndex] = useState(0)
  const [activePage, setActivePage] = useState(activePageProp ?? pages[0]?.id)
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth <= MOBILE_BREAKPOINT)
  const navRef = useRef(null)
  const touchRef = useRef({ active: false, x: 0, y: 0 })

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth <= MOBILE_BREAKPOINT)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const arcRange = isMobile ? { start: 0, end: 180 } : { start: 6, end: 90 }

  const fabSize = typeof window !== 'undefined' ? Math.max(56, Math.min(74, window.innerWidth * 0.07)) : 64

  // Cyclic window of page indices around focusIndex, wrapping like a dial —
  // identical to radialVisibleIndices() in the original.
  const visibleIndices = (() => {
    const n = pages.length
    if (n <= MAX_VISIBLE) return pages.map((_, i) => i)
    const half = Math.floor(MAX_VISIBLE / 2)
    const idx = []
    for (let k = -half; k < MAX_VISIBLE - half; k++) idx.push(((focusIndex + k) % n + n) % n)
    return idx
  })()

  const m = visibleIndices.length
  const iconSize = typeof window !== 'undefined' ? Math.max(52, Math.min(74, window.innerWidth * 0.065)) : 60
  const fabR = fabSize / 2
  const gap = 22
  const attachFloor = fabR + 26 + iconSize / 2
  let radius = attachFloor
  if (m >= 2) {
    const halfStepRad = (arcRange.end - arcRange.start) / (m - 1) / 2 * Math.PI / 180
    const minRadius = (iconSize + gap) / (2 * Math.sin(halfStepRad))
    radius = Math.max(attachFloor, minRadius)
  }

  const angleFor = (i, n) => n === 1 ? arcRange.start : arcRange.start + (arcRange.end - arcRange.start) * (i / (n - 1))

  const points = visibleIndices.map((_, pos) => {
    const deg = angleFor(pos, m)
    const rad = (deg * Math.PI) / 180
    return { dx: radius * Math.cos(rad), dy: -radius * Math.sin(rad) }
  })

  const go = useCallback((p, pageIdx) => {
    setFocusIndex(pageIdx)
    setActivePage(p.id)
    setOpen(false)
    if (p.href) window.location.href = p.href
    else p.action?.()
  }, [])

  const openRadial = () => {
    setFocusIndex(Math.max(0, pages.findIndex(p => p.id === activePage)))
    setOpen(true)
  }
  const closeRadial = () => setOpen(false)

  const moveFocus = useCallback((step) => {
    setFocusIndex(prev => {
      const n = pages.length
      return ((prev + step) % n + n) % n
    })
  }, [pages.length])

  // Click-outside-to-close
  useEffect(() => {
    if (!open) return
    const onClick = (e) => { if (navRef.current && !navRef.current.contains(e.target)) closeRadial() }
    document.addEventListener('click', onClick)
    return () => document.removeEventListener('click', onClick)
  }, [open])

  // Keyboard: arrows move the focus cursor, Enter/Space confirms, Escape closes
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key === 'ArrowRight') { e.preventDefault(); moveFocus(-1) }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); moveFocus(1) }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(pages[focusIndex], focusIndex) }
      else if (e.key === 'Escape') closeRadial()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, focusIndex, moveFocus, go, pages])

  // Touch swipe on the backdrop rotates the focus ring, same thresholds as the original
  const onTouchStart = (e) => {
    if (!open || e.touches.length !== 1) return
    touchRef.current = { active: true, x: e.touches[0].clientX, y: e.touches[0].clientY }
  }
  const onTouchMove = (e) => {
    if (!touchRef.current.active) return
    const dx = e.touches[0].clientX - touchRef.current.x
    const dy = e.touches[0].clientY - touchRef.current.y
    if (Math.abs(dx) > SWIPE_MOVE_GUARD || Math.abs(dy) > SWIPE_MOVE_GUARD) e.preventDefault()
  }
  const onTouchEnd = (e) => {
    if (!touchRef.current.active) return
    touchRef.current.active = false
    const t = e.changedTouches[0]
    const dx = t.clientX - touchRef.current.x
    const dy = t.clientY - touchRef.current.y
    if (Math.abs(dx) < SWIPE_THRESHOLD && Math.abs(dy) < SWIPE_THRESHOLD) return
    const primary = Math.abs(dx) > Math.abs(dy) ? dx : dy
    moveFocus(primary < 0 ? 1 : -1)
  }

  // Decorative dashed track connecting the visible icons, bounds computed like the original
  let trackPath = null, trackBox = null
  if (points.length > 1) {
    const pad = 40
    const minX = Math.min(0, ...points.map(p => p.dx)) - pad
    const maxX = Math.max(0, ...points.map(p => p.dx)) + pad
    const minY = Math.min(0, ...points.map(p => p.dy)) - pad
    const maxY = Math.max(0, ...points.map(p => p.dy)) + pad
    trackBox = { minX, minY, w: maxX - minX, h: maxY - minY, bottom: -maxY }
    trackPath = points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt.dx.toFixed(1)} ${pt.dy.toFixed(1)}`).join(' ')
  }

  const anchorLeft = isMobile ? '50%' : `calc(clamp(20px, 3.5vw, 34px) + ${fabSize / 2}px)`
  const anchorBottom = `calc(clamp(20px, 3.5vw, 34px) + ${fabSize / 2}px)`

  return (
    <>
      <div
        onClick={closeRadial}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        style={{
          position: 'fixed', inset: 0, zIndex: 55, touchAction: 'none',
          background: isMobile ? 'rgba(4,3,6,0.5)' : 'rgba(4,3,6,0.32)',
          backdropFilter: isMobile ? 'blur(3px)' : 'blur(2px)',
          opacity: open ? 1 : 0, pointerEvents: open ? 'auto' : 'none',
          transition: 'opacity 0.3s ease',
        }}
      />

      <div
        ref={navRef}
        style={{
          position: 'fixed', left: anchorLeft, bottom: anchorBottom,
          width: 0, height: 0, zIndex: 60,
          transform: isMobile ? 'translateX(-50%)' : undefined,
        }}
      >
        {trackPath && (
          <svg
            width={trackBox.w} height={trackBox.h}
            viewBox={`${trackBox.minX} ${trackBox.minY} ${trackBox.w} ${trackBox.h}`}
            style={{ position: 'absolute', left: trackBox.minX, bottom: trackBox.bottom, overflow: 'visible', pointerEvents: 'none' }}
          >
            <path d={trackPath} fill="none" stroke="rgba(226,179,92,0.28)" strokeWidth="1.5" strokeDasharray="3 6" strokeLinecap="round" style={{ opacity: open ? 1 : 0, transition: 'opacity 0.3s ease 0.05s' }} />
          </svg>
        )}

        <div style={{ position: 'absolute', left: 0, bottom: 0, width: 0, height: 0 }}>
          {visibleIndices.map((pageIdx, pos) => {
            const p = pages[pageIdx]
            const { dx, dy } = points[pos]
            const active = p.id === activePage
            const focused = pageIdx === focusIndex
            return (
              <button
                key={p.id}
                onClick={() => go(p, pageIdx)}
                style={{
                  position: 'absolute', left: 0, bottom: 0,
                  width: iconSize, height: iconSize,
                  margin: `0 0 ${-iconSize / 2}px ${-iconSize / 2}px`,
                  borderRadius: '50%', display: 'grid', placeItems: 'center',
                  transform: open ? `translate(${dx}px, ${dy}px) scale(1)` : 'translate(0,0) scale(0.3)',
                  opacity: open ? 1 : 0, pointerEvents: open ? 'auto' : 'none',
                  transition: `transform 0.45s cubic-bezier(0.22,1,0.36,1) ${open ? pos * 40 : 0}ms, opacity 0.3s ease`,
                }}
                title={p.label}
              >
                {active && (
                  <span style={{
                    position: 'absolute', inset: -16, borderRadius: '50%', zIndex: 0, pointerEvents: 'none',
                    background: 'radial-gradient(circle, rgba(255,212,133,0.55) 0%, rgba(226,179,92,0.18) 45%, rgba(226,179,92,0) 72%)',
                    animation: 'pulse-dot 1.9s ease-in-out infinite',
                  }} />
                )}
                <span style={{
                  position: 'relative', width: '100%', height: '100%', borderRadius: '50%',
                  background: active ? 'linear-gradient(180deg,#fff 0%,#f3ede2 100%)' : 'rgba(16,11,24,0.58)',
                  border: `1px solid ${active ? 'rgba(226,179,92,0.6)' : focused ? 'rgba(255,255,255,0.55)' : 'var(--border-glass)'}`,
                  color: active ? '#1a1103' : 'var(--muted)',
                  display: 'grid', placeItems: 'center', zIndex: 1,
                  boxShadow: active
                    ? '0 0 0 4px rgba(226,179,92,0.16), 0 8px 24px rgba(226,179,92,0.4)'
                    : focused ? '0 0 0 3px rgba(255,255,255,0.16), var(--shadow-fluid)' : 'var(--shadow-fluid)',
                }}>
                  <span style={{ width: '44%', height: '44%' }}>{p.icon}</span>
                </span>
              </button>
            )
          })}
        </div>

        <button
          onClick={() => (open ? closeRadial() : openRadial())}
          aria-label="Open navigation"
          aria-expanded={open}
          style={{
            position: 'absolute', left: 0, bottom: 0, width: fabSize, height: fabSize, borderRadius: '50%',
            margin: `0 0 ${-fabSize / 2}px ${-fabSize / 2}px`,
            display: 'grid', placeItems: 'center',
            background: 'rgba(16,11,24,0.58)',
            border: `1px solid ${open ? 'rgba(226,179,92,0.5)' : 'var(--border-glass)'}`,
            color: open ? 'var(--gold-bright)' : 'var(--text)',
            boxShadow: 'var(--shadow-fluid)',
          }}
        >
          <span style={{ display: 'grid', width: '38%', height: '38%' }}>
            <span style={{ gridArea: '1 / 1', opacity: open ? 0 : 1, transform: open ? 'rotate(45deg) scale(0.5)' : 'none', transition: 'opacity 0.2s ease, transform 0.3s cubic-bezier(0.34,1.56,0.64,1)' }}><MenuIcon /></span>
            <span style={{ gridArea: '1 / 1', opacity: open ? 1 : 0, transform: open ? 'none' : 'rotate(-45deg) scale(0.5)', transition: 'opacity 0.2s ease, transform 0.3s cubic-bezier(0.34,1.56,0.64,1)' }}><CloseIcon /></span>
          </span>
        </button>
      </div>
    </>
  )
}
