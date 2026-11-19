import { useCallback, useEffect, useRef, useState } from 'react'

// Customisable dashboard board: 12-column grid, every card is a "slot" that can be moved (drag the
// handle, or use the ◀ ▶ buttons) and resized (drag the corner) while "Customize" mode is on.
// The layout is stored per device in localStorage. Cards themselves are untouched — they are just
// children of a slot — so none of their behaviour changes. Below 1100px wide the board is a single
// column (sizes are ignored, order is kept).

const KEY = 'argo_dash_layout_v1'
const COLS = 12
export const DEFAULT_ORDER = ['overview', 'map', 'robot', 'orders', 'menu']
const DEFAULT_SIZES = {
  overview: { w: 12, h: 2 },
  map:      { w: 12, h: 10 },
  robot:    { w: 4,  h: 7 },
  orders:   { w: 4,  h: 7 },
  menu:     { w: 4,  h: 7 },
}
const MIN = {
  overview: { w: 6, h: 2 },
  map:      { w: 4, h: 5 },
  robot:    { w: 3, h: 5 },
  orders:   { w: 3, h: 4 },
  menu:     { w: 3, h: 4 },
}
const MAX_H = 24
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

function defaults() {
  return { order: [...DEFAULT_ORDER], sizes: JSON.parse(JSON.stringify(DEFAULT_SIZES)) }
}

function load() {
  const d = defaults()
  try {
    const s = JSON.parse(localStorage.getItem(KEY))
    if (!s) return d
    const order = (Array.isArray(s.order) ? s.order : []).filter(id => DEFAULT_ORDER.includes(id))
    for (const id of DEFAULT_ORDER) if (!order.includes(id)) order.push(id)   // new cards added later still appear
    const sizes = {}
    for (const id of DEFAULT_ORDER) {
      const z = s.sizes?.[id]
      sizes[id] = z && Number.isFinite(z.w) && Number.isFinite(z.h)
        ? { w: clamp(Math.round(z.w), MIN[id].w, COLS), h: clamp(Math.round(z.h), MIN[id].h, MAX_H) }
        : d.sizes[id]
    }
    return { order, sizes }
  } catch { return d }
}

export function useDashLayout() {
  const [lay, setLay] = useState(load)
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(lay)) } catch { /* private mode — layout just won't persist */ } }, [lay])

  const moveTo = useCallback((id, targetId) => setLay(cur => {
    const from = cur.order.indexOf(id), to = cur.order.indexOf(targetId)
    if (from < 0 || to < 0 || from === to) return cur
    const order = cur.order.filter(x => x !== id)
    order.splice(to, 0, id)
    return { ...cur, order }
  }), [])
  const shift = useCallback((id, dir) => setLay(cur => {
    const from = cur.order.indexOf(id), to = from + dir
    if (from < 0 || to < 0 || to >= cur.order.length) return cur
    const order = [...cur.order]
    ;[order[from], order[to]] = [order[to], order[from]]
    return { ...cur, order }
  }), [])
  const setSize = useCallback((id, w, h) => setLay(cur => {
    const z = { w: clamp(w, MIN[id].w, COLS), h: clamp(h, MIN[id].h, MAX_H) }
    const o = cur.sizes[id]
    return o.w === z.w && o.h === z.h ? cur : { ...cur, sizes: { ...cur.sizes, [id]: z } }
  }), [])
  const reset = useCallback(() => setLay(defaults()), [])
  return { ...lay, moveTo, shift, setSize, reset }
}

// Gesture to enter Customize mode (no button): double-click a card on a laptop, press-and-hold a card
// for 3 s on touch. Interactive things (buttons, inputs, the map canvas…) never trigger it.
const HOLD_MS = 3000
const INTERACTIVE = 'button, a, input, select, textarea, label, canvas, summary, [role="button"], .a-slot-bar, .a-slot-grip'

export function DashBoard({ lay, editing, items, onRequestEdit, onRequestExit }) {
  const [dragId, setDragId] = useState(null)
  const overRef = useRef(null)
  const resizeRef = useRef(null)
  const holdRef = useRef(null)

  const cancelHold = () => { if (holdRef.current) { clearTimeout(holdRef.current.t); holdRef.current = null } }
  useEffect(() => cancelHold, [])
  useEffect(() => {                                   // Esc leaves Customize mode
    if (!editing) return
    const k = e => { if (e.key === 'Escape') onRequestExit?.() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [editing, onRequestExit])

  const boardDblClick = e => {
    if (editing || e.target.closest?.(INTERACTIVE)) return
    window.getSelection?.()?.removeAllRanges()
    onRequestEdit?.()
  }
  const boardDown = e => {
    if (editing || e.pointerType === 'mouse' || e.target.closest?.(INTERACTIVE)) return
    cancelHold()
    holdRef.current = {
      x: e.clientX, y: e.clientY,
      t: setTimeout(() => { holdRef.current = null; try { navigator.vibrate?.(40) } catch { /* optional */ } onRequestEdit?.() }, HOLD_MS),
    }
  }
  const boardMove = e => {                            // finger moved = scrolling, not holding
    const h = holdRef.current
    if (h && Math.hypot(e.clientX - h.x, e.clientY - h.y) > 12) cancelHold()
  }

  // ── move: drag the handle over another card to take its place ──
  const barDown = id => e => {
    if (e.button !== undefined && e.button !== 0) return
    e.currentTarget.setPointerCapture(e.pointerId)
    overRef.current = null
    setDragId(id)
  }
  const barMove = id => e => {
    if (dragId !== id) return
    const el = document.elementsFromPoint(e.clientX, e.clientY).find(n => n.dataset?.slot)
    if (!el || el.dataset.slot === id) { overRef.current = null; return }
    if (overRef.current === el.dataset.slot) return        // already swapped with this one; wait until the pointer leaves it
    overRef.current = el.dataset.slot
    lay.moveTo(id, el.dataset.slot)
  }
  const barUp = () => { setDragId(null); overRef.current = null }

  // ── resize: drag the corner; snaps to grid columns / rows ──
  const gripDown = id => e => {
    if (e.button !== undefined && e.button !== 0) return
    const slot = e.currentTarget.parentElement, board = slot.parentElement
    const cs = getComputedStyle(board)
    const gap = parseFloat(cs.columnGap) || 12
    const rowH = parseFloat(cs.gridAutoRows) || 30
    e.currentTarget.setPointerCapture(e.pointerId)
    resizeRef.current = {
      id, x: e.clientX, y: e.clientY, w: lay.sizes[id].w, h: lay.sizes[id].h,
      colPitch: (board.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0) + gap) / COLS,
      rowPitch: rowH + (parseFloat(cs.rowGap) || gap),
    }
    setDragId(id)
  }
  const gripMove = e => {
    const r = resizeRef.current
    if (!r) return
    lay.setSize(r.id, r.w + Math.round((e.clientX - r.x) / r.colPitch), r.h + Math.round((e.clientY - r.y) / r.rowPitch))
  }
  const gripUp = () => { resizeRef.current = null; setDragId(null) }

  return (
    <div className={`a-board ${editing ? 'editing' : ''}`}
      onDoubleClick={boardDblClick} onPointerDown={boardDown} onPointerMove={boardMove} onPointerUp={cancelHold} onPointerCancel={cancelHold}
      onContextMenu={e => { if (holdRef.current) e.preventDefault() }}>
      {lay.order.map((id, i) => {
        const it = items[id]
        if (!it) return null
        const { w, h } = lay.sizes[id]
        return (
          <div key={id} data-slot={id} className={`a-slot ${editing ? 'edit' : ''} ${dragId === id ? 'dragging' : ''}`}
            style={{ '--w': w, '--h': h }}>
            {it.node}
            {editing && <>
              <div className="a-slot-bar" onPointerDown={barDown(id)} onPointerMove={barMove(id)} onPointerUp={barUp} onPointerCancel={barUp}
                title="Drag to move this card">
                <button type="button" aria-label="Move earlier" disabled={i === 0}
                  onPointerDown={e => e.stopPropagation()} onClick={() => lay.shift(id, -1)}>◀</button>
                <span>⠿ {it.title} <em>{w}×{h}</em></span>
                <button type="button" aria-label="Move later" disabled={i === lay.order.length - 1}
                  onPointerDown={e => e.stopPropagation()} onClick={() => lay.shift(id, +1)}>▶</button>
              </div>
              <div className="a-slot-grip" onPointerDown={gripDown(id)} onPointerMove={gripMove} onPointerUp={gripUp} onPointerCancel={gripUp}
                title="Drag to resize" />
            </>}
          </div>
        )
      })}
    </div>
  )
}
