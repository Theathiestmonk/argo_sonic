import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as Ic from './icons'
import { money } from './format'

/* Orders — live board + full history, backed by the POS order ledger (backend/pos_api.py).
   POS practices used here: short sequential order numbers, strict status flow with one-click "next step",
   oldest-first queues with ageing colours, payment tracked separately from fulfilment, cancel needs a reason,
   items editable only before the kitchen starts, a full audit timeline, KOT + receipt printing, CSV export,
   an audible alert for new orders, and optimistic-concurrency (a stale screen can't overwrite a newer change). */

const FLOW = ['placed', 'confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered']
const ACTIVE = FLOW.slice(0, 5)
const META = {
  placed: { label: 'New', next: 'confirmed', cta: 'Accept' },
  confirmed: { label: 'Confirmed', next: 'preparing', cta: 'Start preparing' },
  preparing: { label: 'Preparing', next: 'ready', cta: 'Mark ready' },
  ready: { label: 'Ready', next: 'out_for_delivery', cta: 'Send robot' },
  out_for_delivery: { label: 'Delivering', next: 'delivered', cta: 'Mark delivered' },
  delivered: { label: 'Served' }, cancelled: { label: 'Cancelled' }, refunded: { label: 'Refunded' },
}
const SOURCES = { voice_assistant: 'Sonic (voice)', pos: 'POS', staff: 'Staff', mobile: 'Mobile', dine_in: 'Dine-in' }
const CANCEL_REASONS = ['Guest left', 'Wrong order', 'Out of stock', 'Duplicate order', 'Kitchen issue', 'Other']
const RANGES = [['today', 'Today'], ['yesterday', 'Yesterday'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['all', 'All time']]

const t12 = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const dayLabel = (iso) => { const d = new Date(iso), n = new Date(); return d.toDateString() === n.toDateString() ? 'Today' : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) }
const mins = (iso, now) => Math.max(0, Math.floor((now - new Date(iso).getTime()) / 60000))
const ageTxt = (m) => (m < 1 ? 'now' : m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`)
const ageCls = (o, m) => (['placed', 'confirmed', 'preparing'].includes(o.status) ? (m >= 20 ? 'late' : m >= 10 ? 'warn' : '') : ['ready', 'out_for_delivery'].includes(o.status) ? (m >= 10 ? 'late' : m >= 5 ? 'warn' : '') : '')
const itemCount = (o) => o.items.reduce((s, i) => s + i.qty, 0)

function rangeParams(r) {
  if (r === 'all') return {}
  const d0 = new Date(); d0.setHours(0, 0, 0, 0)
  const day = (n) => new Date(d0.getTime() + n * 86400000).toISOString()
  if (r === 'today') return { from: day(0), to: day(1) }
  if (r === 'yesterday') return { from: day(-1), to: day(0) }
  return { from: day(r === '7d' ? -6 : -29), to: day(1) }
}
const qs = (o) => Object.entries(o).filter(([, v]) => v !== '' && v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&')

function chime() {
  try {
    const A = window.AudioContext || window.webkitAudioContext, c = new A(), t = c.currentTime
    ;[[880, 0], [1320, 0.16]].forEach(([f, d]) => { const o = c.createOscillator(), g = c.createGain(); o.frequency.value = f; o.type = 'sine'; o.connect(g); g.connect(c.destination)
      g.gain.setValueAtTime(0.0001, t + d); g.gain.exponentialRampToValueAtTime(0.25, t + d + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.3); o.start(t + d); o.stop(t + d + 0.32) })
    setTimeout(() => c.close(), 900)
  } catch { /* audio blocked until the first click – fine */ }
}

function printTicket(o, kind, restaurant) {
  const w = window.open('', '_blank', 'width=400,height=700'); if (!w) return
  const cur = o.currency || 'INR', row = (l, r, b) => `<div class="r${b ? ' b' : ''}"><span>${l}</span><span>${r ?? ''}</span></div>`
  const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
  const items = o.items.map(i => kind === 'kot'
    ? `<div class="k"><b>${i.qty} ×</b> ${esc(i.name)}${i.notes ? `<small>— ${esc(i.notes)}</small>` : ''}</div>`
    : row(`${i.qty} × ${esc(i.name)}${i.notes ? ` (${esc(i.notes)})` : ''}`, money(i.unit_price * i.qty, cur))).join('')
  w.document.write(`<!doctype html><title>${kind === 'kot' ? 'KOT' : 'Receipt'} #${o.number}</title><style>
    body{font:13px/1.45 ui-monospace,Menlo,monospace;width:300px;margin:12px auto;color:#000}h1{font-size:18px;text-align:center;margin:0}h2{font-size:15px;text-align:center;margin:2px 0 8px}
    hr{border:0;border-top:1px dashed #000;margin:8px 0}.r{display:flex;justify-content:space-between;gap:8px}.b{font-weight:700;font-size:15px}.k{font-size:16px;margin:5px 0}.k small{display:block;font-size:12px}
    .c{text-align:center}.note{border:2px solid #000;padding:6px;margin:8px 0;font-weight:700}</style>
    <h1>${esc(kind === 'kot' ? 'KITCHEN ORDER' : restaurant?.name || 'Receipt')}</h1><h2>#${o.number} · ${o.table_no ? 'Table ' + esc(o.table_no) : 'Counter'}</h2>
    <div class="c">${new Date(o.created_at).toLocaleString()}<br>${esc(SOURCES[o.source] || o.source)}</div><hr>${items}<hr>
    ${o.notes ? `<div class="note">NOTE: ${esc(o.notes)}</div>` : ''}
    ${kind === 'kot' ? '' : row('Subtotal', money(o.subtotal, cur)) + row(`Tax (${o.tax_percent}%)`, money(o.tax, cur)) + row('TOTAL', money(o.total, cur), true) +
      `<hr><div class="c">${o.payment_status === 'paid' ? 'PAID · ' + esc((o.payment_method || '').toUpperCase()) : 'PAYMENT PENDING'}<br>Thank you!</div>`}
    <script>window.onload=()=>{window.print()}<\/script>`)
  w.document.close()
}

function Chip({ status }) { return <span className={`o-chip ${status}`}><i />{META[status]?.label || status}</span> }
function Pay({ o }) { return <span className={`o-pay ${o.payment_status}`}>{o.payment_status === 'paid' ? `Paid · ${(o.payment_method || '').toUpperCase()}` : o.payment_status === 'refunded' ? 'Refunded' : 'Unpaid'}</span> }
const SRC_TAG = { voice_assistant: 'Sonic', pos: 'POS', staff: 'Staff', mobile: 'App', dine_in: 'Dine-in' }
const SrcIcon = ({ s }) => <span className="o-src" title={SOURCES[s] || s}>{SRC_TAG[s] || s}</span>

export default function OrdersPage({ api, launcherUrl, selectedMap, showToast, onOpen }) {
  const [scope, setScope] = useState('live')                    // live | history | all
  const [statusF, setStatusF] = useState('')
  const [range, setRange] = useState('today')
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [payment, setPayment] = useState('')
  const [view, setView] = useState(() => { try { return localStorage.getItem('argo_orders_view') || 'board' } catch { return 'board' } })
  const [page, setPage] = useState(0)
  const [data, setData] = useState({ orders: [], total: 0, counts: {} })
  const [sum, setSum] = useState(null)
  const [err, setErr] = useState(null)
  const [now, setNow] = useState(Date.now())
  const [sel, setSel] = useState(null)                          // full order in the drawer
  const [creating, setCreating] = useState(false)
  const [sound, setSound] = useState(() => { try { return localStorage.getItem('argo_orders_sound') !== '0' } catch { return true } })
  const [fresh, setFresh] = useState(() => new Set())
  const [restaurant, setRestaurant] = useState(null)
  const revRef = useRef(null), seen = useRef(null), soundRef = useRef(sound)
  soundRef.current = sound
  const PAGE = 50

  const effScope = view === 'board' ? 'live' : scope
  const params = useMemo(() => ({
    status: statusF || (effScope === 'live' ? 'active' : effScope === 'history' ? 'processed' : ''),
    ...(effScope === 'live' && !q ? {} : rangeParams(range)), q, source, payment, limit: view === 'board' ? 200 : PAGE, offset: view === 'board' ? 0 : page * PAGE, sort: view === 'board' ? 'oldest' : 'newest',
  }), [statusF, effScope, range, q, source, payment, view, page])

  const get = useCallback(async (path) => { const r = await fetch(`${api}${path}`, { cache: 'no-store' }); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j }, [api])
  const send = useCallback(async (method, path, body) => {
    const r = await fetch(`${api}${path}`, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor: 'staff', ...body }) })
    const j = await r.json(); if (!r.ok) { const e = new Error(j.error || r.status); e.status = r.status; e.current = j.current; throw e }; return j
  }, [api])

  const refresh = useCallback(async () => {
    try {
      const [list, s] = await Promise.all([get(`/orders?${qs(params)}`), get(`/orders/summary?${qs(rangeParams('today'))}`)])
      setData(list); setSum(s); setErr(null); revRef.current = list.rev
      // new-order alert: anything 'placed' we haven't seen since the first load
      const live = view === 'board' || effScope !== 'history' ? list.orders : []
      const ids = new Set(live.map(o => o.id))
      if (seen.current) {
        const added = live.filter(o => !seen.current.has(o.id) && o.status === 'placed')
        if (added.length) {
          if (soundRef.current) chime()
          showToast(`New order #${added[0].number}${added[0].table_no ? ` · Table ${added[0].table_no}` : ''}${added.length > 1 ? ` (+${added.length - 1} more)` : ''}`, 'ok')
          setFresh(new Set(added.map(o => o.id))); setTimeout(() => setFresh(new Set()), 7000)
        }
      }
      seen.current = new Set([...(seen.current || []), ...ids])
    } catch (e) { setErr(String(e.message || e)) }
  }, [get, params, view, effScope, showToast])

  useEffect(() => { seen.current = null; refresh() }, [refresh])
  useEffect(() => {                                              // cheap change-detection poll
    const id = setInterval(async () => { try { const r = await get('/orders/rev'); if (r.rev !== revRef.current) refresh() } catch { /* shown on next full refresh */ } }, 3000)
    return () => clearInterval(id)
  }, [get, refresh])
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 20000); return () => clearInterval(id) }, [])
  useEffect(() => { fetch('/restaurant.json').then(r => r.json()).then(setRestaurant).catch(() => {}) }, [])
  useEffect(() => { setPage(0) }, [statusF, scope, range, q, source, payment, view])
  useEffect(() => { const k = (e) => { if (e.key === 'Escape') { setSel(null); setCreating(false) } }; window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k) }, [])

  const open = async (o) => { try { setSel(await get(`/orders/${o.id}`)) } catch (e) { showToast(String(e.message), 'danger') } }
  const patch = async (o, body, okMsg) => {
    try {
      const upd = await send('PATCH', `/orders/${o.id}`, { version: o.version, ...body })
      if (sel?.id === o.id) setSel(upd)
      if (okMsg) showToast(okMsg, 'ok')
      refresh(); return upd
    } catch (e) {
      showToast(e.message, e.status === 409 ? 'info' : 'danger')
      if (e.current && sel?.id === o.id) setSel(e.current)
      refresh()
    }
  }
  const dispatch = async (o) => {
    const upd = await patch(o, { status: 'out_for_delivery' })
    if (!upd || !o.table_no) return
    try {
      const r = await fetch(`${launcherUrl}/nav/goto`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ destination: `Table ${o.table_no}`, map: selectedMap }) })
      const j = await r.json().catch(() => ({}))
      showToast(r.ok ? `Argo is on its way to Table ${o.table_no}` : j.error === 'nav_goto_busy' ? 'Argo is already on a trip – order marked as delivering' : `Order marked delivering, but Argo couldn't be sent: ${j.error || r.status}`, r.ok ? 'ok' : 'info')
    } catch { showToast('Order marked delivering, but the robot launcher is unreachable', 'info') }
  }
  const advance = (o) => (o.status === 'ready' ? dispatch(o) : patch(o, { status: META[o.status].next }))
  const toggleView = (v) => { setView(v); try { localStorage.setItem('argo_orders_view', v) } catch { /* private mode */ } }
  const toggleSound = () => { const v = !sound; setSound(v); try { localStorage.setItem('argo_orders_sound', v ? '1' : '0') } catch { /* */ } if (v) chime() }

  const counts = data.counts || {}
  const live = ACTIVE.reduce((n, s) => n + (counts[s] || 0), 0)
  const chips = effScope === 'live' ? ACTIVE : effScope === 'history' ? ['delivered', 'cancelled', 'refunded'] : [...FLOW, 'cancelled', 'refunded']
  const cols = ACTIVE.map(s => ({ s, list: data.orders.filter(o => o.status === s) }))
  const cur = sum ? (data.orders[0]?.currency || 'INR') : 'INR'

  if (err && !data.orders.length) return (
    <div className="a-content"><div className="a-card a-pad m-err"><h3>Can't reach the orders service</h3>
      <p>The Orders page uses the POS service on port 8890 of the robot. It isn't answering ({err}).</p>
      <button className="a-btn gold" onClick={refresh}>Try again</button></div></div>
  )

  return (
    <div className="a-content o-fit">
      <header className="a-card o-head">
        <div className="o-title"><h2>Orders</h2>
          <p>{live} open now · {sum ? `${sum.orders} order${sum.orders === 1 ? '' : 's'} today` : '…'}</p></div>
        <div className="o-kpis">
          <div><small>Today's sales</small><b>{money(sum?.sales ?? 0, cur)}</b></div>
          <div><small>Collected</small><b>{money(sum?.paid ?? 0, cur)}</b></div>
          <div><small>Avg. prep time</small><b>{sum?.avg_prep_min != null ? `${sum.avg_prep_min} min` : '—'}</b></div>
          <div><small>Cancelled today</small><b>{sum?.cancelled ?? 0}</b></div>
        </div>
        <div className="a-search"><Ic.Search /><input placeholder="Search #, table, dish, note…" value={q} onChange={e => setQ(e.target.value)} /></div>
        <button className="a-btn sq" title={sound ? 'New-order sound is on' : 'New-order sound is off'} onClick={toggleSound} style={{ opacity: sound ? 1 : .55 }}><Ic.Bell width="19" height="19" /></button>
        <a className="a-btn" href={`${api}/orders/export.csv?${qs({ ...rangeParams(range), status: statusF })}`} download>Export CSV</a>
        <button className="a-btn gold" onClick={() => setCreating(true)}>+ New order</button>
      </header>

      <div className="o-bar">
        <div className="a-seg">
          {[['board', 'Board'], ['list', 'List']].map(([id, l]) => <button key={id} className={view === id ? 'on' : ''} onClick={() => toggleView(id)}>{l}</button>)}
        </div>
        {view === 'list' && <div className="a-seg">{[['live', 'Live'], ['history', 'Completed'], ['all', 'All']].map(([id, l]) => <button key={id} className={scope === id ? 'on' : ''} onClick={() => { setScope(id); setStatusF('') }}>{l}</button>)}</div>}
        {view === 'list' && <div className="o-chips">
          <button className={`a-tab ${statusF === '' ? 'on' : ''}`} onClick={() => setStatusF('')}>All<em>{effScope === 'live' ? live : Object.values(counts).reduce((a, b) => a + b, 0)}</em></button>
          {chips.map(s => <button key={s} className={`a-tab ${statusF === s ? 'on' : ''}`} onClick={() => setStatusF(statusF === s ? '' : s)}>{META[s].label}{counts[s] ? <em>{counts[s]}</em> : null}</button>)}
        </div>}
        <span style={{ flex: 1 }} />
        {(view === 'list' || q) && <select className="a-select" value={range} onChange={e => setRange(e.target.value)}>{RANGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>}
        <select className="a-select" value={source} onChange={e => setSource(e.target.value)}><option value="">All sources</option>{Object.entries(SOURCES).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        <select className="a-select" value={payment} onChange={e => setPayment(e.target.value)}><option value="">Any payment</option><option value="unpaid">Unpaid</option><option value="paid">Paid</option><option value="refunded">Refunded</option></select>
      </div>

      {view === 'board' ? (
        <div className="o-board">
          {cols.map(({ s, list }) => (
            <section key={s} className="o-col">
              <h4><Chip status={s} /><em>{list.length}</em>{list[0] && <span title="Oldest order in this column">oldest {ageTxt(mins(list[0].created_at, now))}</span>}</h4>
              <div className="o-cards">
                {list.length === 0 && <div className="o-none">{s === 'placed' ? 'No new orders' : 'Nothing here'}</div>}
                {list.map(o => {
                  const m = mins(['ready', 'out_for_delivery'].includes(s) ? o.updated_at : o.created_at, now)
                  return (
                    <article key={o.id} className={`o-card ${fresh.has(o.id) ? 'fresh' : ''}`} onClick={() => open(o)}>
                      <div className="h"><b>#{o.number}</b><span className="tb">{o.table_no ? `Table ${o.table_no}` : 'Counter'}</span><SrcIcon s={o.source} /><span className={`age ${ageCls(o, m)}`}>{ageTxt(m)}</span></div>
                      <ul>{o.items.slice(0, 4).map(i => <li key={i.id}><b>{i.qty}×</b> {i.name}{i.notes && <small> · {i.notes}</small>}</li>)}{o.items.length > 4 && <li className="more">+{o.items.length - 4} more</li>}</ul>
                      {o.notes && <div className="note">Note: {o.notes}</div>}
                      <div className="f"><b>{money(o.total, o.currency)}</b><Pay o={o} /></div>
                      <button className="a-btn gold" onClick={e => { e.stopPropagation(); advance(o) }}>{META[s].cta}</button>
                    </article>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div className="a-card o-list">
          {data.orders.length === 0 ? <div className="a-empty"><b>No orders match</b>Try another date range or clear the filters.</div> : (
            <table className="m-table">
              <thead><tr><th>Order</th><th>Placed</th><th>Table</th><th>Items</th><th>Source</th><th>Status</th><th>Payment</th><th className="r">Total</th><th /></tr></thead>
              <tbody>{data.orders.map(o => (
                <tr key={o.id} className={`o-row ${fresh.has(o.id) ? 'fresh' : ''}`} onClick={() => open(o)}>
                  <td><b>#{o.number}</b></td>
                  <td>{t12(o.created_at)} <small>{dayLabel(o.created_at)}</small></td>
                  <td>{o.table_no ? `Table ${o.table_no}` : 'Counter'}</td>
                  <td className="it" title={o.items.map(i => `${i.qty}× ${i.name}`).join(', ')}>{itemCount(o)} · {o.items.map(i => `${i.qty}× ${i.name}`).join(', ')}</td>
                  <td>{SOURCES[o.source] || o.source}</td>
                  <td><Chip status={o.status} /></td>
                  <td><Pay o={o} /></td>
                  <td className="r"><b>{money(o.total, o.currency)}</b></td>
                  <td>{META[o.status]?.next && <button className="a-btn" onClick={e => { e.stopPropagation(); advance(o) }}>{META[o.status].cta}</button>}</td>
                </tr>))}</tbody>
            </table>
          )}
          {data.total > PAGE && (
            <div className="o-pager"><span>{page * PAGE + 1}–{Math.min((page + 1) * PAGE, data.total)} of {data.total}</span>
              <button className="a-btn" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button>
              <button className="a-btn" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(p => p + 1)}>Next</button></div>
          )}
        </div>
      )}

      {sel && <OrderDrawer key={sel.id} o={sel} api={api} now={now} onClose={() => setSel(null)} patch={patch} dispatch={dispatch} advance={advance} restaurant={restaurant} showToast={showToast} />}
      {creating && <NewOrder api={api} launcherUrl={launcherUrl} selectedMap={selectedMap} onClose={() => setCreating(false)}
        onCreate={async (body) => { try { const o = await send('POST', '/orders', { source: 'pos', map: selectedMap, ...body }); showToast(`Order #${o.number} placed`, 'ok'); setCreating(false); refresh(); open(o) } catch (e) { showToast(e.message, 'danger') } }} />}
    </div>
  )
}

// ═════════════════════════ order detail ═════════════════════════
function OrderDrawer({ o, api, now, onClose, patch, dispatch, advance, restaurant, showToast }) {
  const editable = ['placed', 'confirmed'].includes(o.status)
  const closed = ['delivered', 'cancelled', 'refunded'].includes(o.status)
  const [items, setItems] = useState(o.items)
  const [cancelling, setCancelling] = useState(false)
  const [reason, setReason] = useState(''), [other, setOther] = useState('')
  const [method, setMethod] = useState('cash')
  const [notes, setNotes] = useState(o.notes)
  const [menu, setMenu] = useState(null), [adding, setAdding] = useState(''), [qm, setQm] = useState('')
  useEffect(() => { setItems(o.items); setNotes(o.notes) }, [o.version])       // a newer version arrived
  const dirty = JSON.stringify(items.map(i => [i.name, i.qty])) !== JSON.stringify(o.items.map(i => [i.name, i.qty]))
  const when = Object.fromEntries(o.events.filter(e => e.kind === 'status' || e.kind === 'created').map(e => [e.to_status, e.at]))
  const idx = FLOW.indexOf(o.status)
  const loadMenu = async () => { if (menu) return; try { setMenu((await (await fetch(`${api}/menu`)).json()).menu.filter(m => m.available !== false)) } catch { setMenu([]) } }
  const calc = useMemo(() => { const sub = items.reduce((s, i) => s + i.unit_price * i.qty, 0), tax = Math.round(sub * o.tax_percent) / 100; return { sub, tax, total: sub + tax } }, [items, o.tax_percent])
  const setQty = (k, d) => setItems(l => l.map((i, n) => (n === k ? { ...i, qty: Math.max(1, Math.min(99, i.qty + d)) } : i)))
  const addDish = (m) => { setItems(l => { const k = l.findIndex(i => i.menu_id === String(m.id)); if (k >= 0) return l.map((i, n) => (n === k ? { ...i, qty: i.qty + 1 } : i)); const p = m.discountActive && m.discountPercent ? Math.round(m.price * (1 - m.discountPercent / 100) * 100) / 100 : m.price; return [...l, { id: `n${Date.now()}`, menu_id: String(m.id), name: m.name, qty: 1, unit_price: p, list_price: m.price, notes: '' }] }); setAdding(''); setQm('') }
  const saveItems = () => patch(o, { items: items.map(i => ({ name: i.name, qty: i.qty, menu_id: i.menu_id, price: i.unit_price, list_price: i.list_price, notes: i.notes, kind: i.kind })) }, 'Items updated')
  const matches = (menu || []).filter(m => !qm || m.name.toLowerCase().includes(qm.toLowerCase())).slice(0, 8)

  return (
    <>
      <div className="m-scrim" onClick={onClose} />
      <aside className="m-drawer o-drawer" role="dialog" aria-label={`Order ${o.number}`}>
        <div className="dh"><div><h3>#{o.number} <span className="o-sub">· {o.table_no ? `Table ${o.table_no}` : 'Counter'}</span></h3>
          <small>{dayLabel(o.created_at)} {t12(o.created_at)} · {SOURCES[o.source] || o.source} · {ageTxt(mins(o.created_at, now))} ago</small></div>
          <div className="o-dh-r"><Chip status={o.status} /><button onClick={onClose} aria-label="Close">✕</button></div></div>

        <div className="db">
          {o.status === 'cancelled' ? <div className="o-banner bad">Cancelled — {o.cancel_reason || 'no reason recorded'}</div> : (
            <ol className="o-steps">{FLOW.map((s, i) => (
              <li key={s} className={i < idx ? 'done' : i === idx ? 'now' : ''}><i>{i < idx ? '✓' : i + 1}</i><span>{META[s].label}</span><small>{when[s] ? t12(when[s]) : ''}</small></li>))}</ol>
          )}

          <section className="o-sec">
            <div className="o-sh"><h5>Items</h5>{editable && <button className="a-link" onClick={() => { setAdding(a => (a ? '' : 'open')); loadMenu() }}>+ Add dish</button>}</div>
            {adding && editable && (
              <div className="o-add"><input className="m-in" autoFocus placeholder="Search the menu…" value={qm} onChange={e => setQm(e.target.value)} />
                <div>{matches.map(m => <button key={m.id} onClick={() => addDish(m)}><span>{m.name}</span><b>{money(m.price, o.currency)}</b></button>)}{menu && !matches.length && <em>No match</em>}</div></div>)}
            {items.map((i, k) => (
              <div className="o-item" key={i.id ?? k}>
                <div className="q">{editable ? <><button onClick={() => setQty(k, -1)}>−</button><b>{i.qty}</b><button onClick={() => setQty(k, 1)}>+</button></> : <b>{i.qty}×</b>}</div>
                <div className="n"><b>{i.name}</b>{i.notes && <small>{i.notes}</small>}</div>
                <div className="p">{i.list_price > i.unit_price && <s>{money(i.list_price * i.qty, o.currency)}</s>}{money(i.unit_price * i.qty, o.currency)}</div>
                {editable && items.length > 1 && <button className="x" title="Remove" onClick={() => setItems(l => l.filter((_, n) => n !== k))}>✕</button>}
              </div>))}
            {dirty && <div className="o-dirty">Unsaved changes <button className="a-btn gold" onClick={saveItems}>Save items</button><button className="a-btn" onClick={() => setItems(o.items)}>Discard</button></div>}
            <div className="o-tot"><div><span>Subtotal</span><span>{money(dirty ? calc.sub : o.subtotal, o.currency)}</span></div>
              <div><span>Tax ({o.tax_percent}%)</span><span>{money(dirty ? calc.tax : o.tax, o.currency)}</span></div>
              <div className="g"><span>Total</span><span>{money(dirty ? calc.total : o.total, o.currency)}</span></div></div>
          </section>

          <section className="o-sec">
            <div className="o-sh"><h5>Kitchen note</h5></div>
            {closed ? <p className="o-p">{o.notes || '—'}</p> : <textarea className="m-in" rows={2} value={notes} onChange={e => setNotes(e.target.value)} placeholder="Allergies, no onion, serve together…"
              onBlur={() => notes !== o.notes && patch(o, { notes })} />}
          </section>

          <section className="o-sec">
            <div className="o-sh"><h5>Payment</h5><Pay o={o} /></div>
            {o.status !== 'cancelled' && o.payment_status === 'unpaid' && (
              <div className="o-pay-row">{['cash', 'card', 'upi', 'other'].map(m => <button key={m} className={`a-tab ${method === m ? 'on' : ''}`} onClick={() => setMethod(m)}>{m.toUpperCase()}</button>)}
                <button className="a-btn gold" onClick={() => patch(o, { payment: { status: 'paid', method } }, `Payment of ${money(o.total, o.currency)} recorded`)}>Mark paid</button></div>)}
            {o.payment_status === 'paid' && <div className="o-pay-row"><small>Received {o.paid_at ? t12(o.paid_at) : ''}</small><span style={{ flex: 1 }} />
              <button className="a-btn danger" onClick={() => window.confirm('Refund this payment?') && patch(o, { payment: { status: 'refunded' } }, 'Payment refunded')}>Refund</button></div>}
          </section>

          <section className="o-sec">
            <div className="o-sh"><h5>Timeline</h5></div>
            <ul className="o-tl">{[...o.events].reverse().map(e => (
              <li key={e.id}><time>{t12(e.at)}</time><span>{e.kind === 'created' ? 'Order placed' : e.kind === 'status' ? `${META[e.from_status]?.label || '—'} → ${META[e.to_status]?.label || e.to_status}` : e.kind === 'payment' ? `Payment: ${e.note}` : `Edited: ${e.note}`}
                <small>{e.kind === 'status' && e.note ? `${e.note} · ` : e.kind === 'created' ? `${e.note} · ` : ''}{e.actor}</small></span></li>))}</ul>
          </section>

          {cancelling && (
            <div className="o-cancel"><b>Why is this order being cancelled?</b>
              <div className="o-chips">{CANCEL_REASONS.map(r => <button key={r} className={`a-tab ${reason === r ? 'on' : ''}`} onClick={() => setReason(r)}>{r}</button>)}</div>
              {reason === 'Other' && <input className="m-in" placeholder="Reason" value={other} onChange={e => setOther(e.target.value)} />}
              <div className="o-pay-row"><button className="a-btn" onClick={() => setCancelling(false)}>Keep order</button><span style={{ flex: 1 }} />
                <button className="a-btn danger" disabled={!reason || (reason === 'Other' && !other.trim())} onClick={() => { patch(o, { status: 'cancelled', reason: reason === 'Other' ? other.trim() : reason }, `Order #${o.number} cancelled`); setCancelling(false) }}>Cancel order</button></div></div>)}
        </div>

        <div className="df o-df">
          <button className="a-btn" onClick={() => printTicket(o, 'kot', restaurant)}>Print KOT</button>
          <button className="a-btn" onClick={() => printTicket(o, 'receipt', restaurant)}>Receipt</button>
          <span style={{ flex: 1 }} />
          {ACTIVE.includes(o.status) && !cancelling && <button className="a-btn danger" onClick={() => setCancelling(true)}>Cancel</button>}
          {idx > 0 && ACTIVE.includes(o.status) && <button className="a-btn" title="Undo the last step" onClick={() => patch(o, { status: FLOW[idx - 1] })}>← Back</button>}
          {META[o.status]?.next && <button className="a-btn gold" onClick={() => advance(o)}>{META[o.status].cta}</button>}
        </div>
      </aside>
    </>
  )
}

// ═════════════════════════ new (manual / POS) order ═════════════════════════
function NewOrder({ api, launcherUrl, selectedMap, onClose, onCreate }) {
  const [menu, setMenu] = useState({ menu: [], settings: {} })
  const [tables, setTables] = useState([])
  const [table, setTable] = useState('')
  const [cart, setCart] = useState([])
  const [q, setQ] = useState(''), [cat, setCat] = useState('All'), [notes, setNotes] = useState(''), [busy, setBusy] = useState(false)
  useEffect(() => {
    fetch(`${api}/menu`).then(r => r.json()).then(setMenu).catch(() => fetch('/menu.json').then(r => r.json()).then(setMenu).catch(() => {}))
    fetch(`${launcherUrl}/waypoints/${selectedMap}`).then(r => r.json()).then(d => setTables(Object.values(d || {}).map(w => (w.name || '').match(/^table\s*(\d+)/i)?.[1]).filter(Boolean).sort((a, b) => a - b))).catch(() => {})
  }, [api, launcherUrl, selectedMap])
  const items = menu.menu.filter(m => m.available !== false)
  const cats = ['All', ...new Set(items.map(m => m.category))]
  const shown = items.filter(m => (cat === 'All' || m.category === cat) && (!q || m.name.toLowerCase().includes(q.toLowerCase())))
  const price = (m) => (m.discountActive && m.discountPercent ? Math.round(m.price * (1 - m.discountPercent / 100) * 100) / 100 : m.price)
  const cur = menu.settings?.currencyCode || 'INR', taxPct = Number(menu.settings?.taxPercent || 0)
  const add = (m) => setCart(c => (c.some(i => i.m.id === m.id) ? c.map(i => (i.m.id === m.id ? { ...i, qty: i.qty + 1 } : i)) : [...c, { m, qty: 1 }]))
  const sub = cart.reduce((s, i) => s + price(i.m) * i.qty, 0), tax = Math.round(sub * taxPct) / 100
  return (
    <>
      <div className="m-scrim" onClick={onClose} />
      <aside className="m-drawer o-new" role="dialog" aria-label="New order">
        <div className="dh"><h3>New order</h3><button onClick={onClose}>✕</button></div>
        <div className="db">
          <label className="m-f"><span>Table</span>
            {tables.length ? <select className="m-in" value={table} onChange={e => setTable(e.target.value)}><option value="">Counter / takeaway</option>{tables.map(t => <option key={t} value={t}>Table {t}</option>)}</select>
              : <input className="m-in" inputMode="numeric" placeholder="Table number (leave empty for counter)" value={table} onChange={e => setTable(e.target.value)} />}</label>
          <div className="a-search" style={{ maxWidth: 'none' }}><Ic.Search /><input placeholder="Search the menu…" value={q} onChange={e => setQ(e.target.value)} /></div>
          <div className="o-chips" style={{ flexWrap: 'nowrap', overflowX: 'auto' }}>{cats.map(c => <button key={c} className={`a-tab ${cat === c ? 'on' : ''}`} style={{ flexShrink: 0 }} onClick={() => setCat(c)}>{c}</button>)}</div>
          <div className="o-pick">{shown.map(m => <button key={m.id} onClick={() => add(m)}><i className={`dot ${m.diet}`} /><span>{m.name}</span><b>{money(price(m), cur)}</b></button>)}</div>
          <section className="o-sec"><div className="o-sh"><h5>Order</h5><small>{cart.reduce((n, i) => n + i.qty, 0)} items</small></div>
            {cart.length === 0 && <p className="o-p">Tap dishes above to add them.</p>}
            {cart.map(i => (
              <div className="o-item" key={i.m.id}><div className="q"><button onClick={() => setCart(c => c.map(x => (x.m.id === i.m.id ? { ...x, qty: Math.max(1, x.qty - 1) } : x)))}>−</button><b>{i.qty}</b><button onClick={() => add(i.m)}>+</button></div>
                <div className="n"><b>{i.m.name}</b></div><div className="p">{money(price(i.m) * i.qty, cur)}</div><button className="x" onClick={() => setCart(c => c.filter(x => x.m.id !== i.m.id))}>✕</button></div>))}
            <textarea className="m-in" rows={2} placeholder="Kitchen note (optional)" value={notes} onChange={e => setNotes(e.target.value)} />
            <div className="o-tot"><div><span>Subtotal</span><span>{money(sub, cur)}</span></div><div><span>Tax ({taxPct}%)</span><span>{money(tax, cur)}</span></div><div className="g"><span>Total</span><span>{money(sub + tax, cur)}</span></div></div>
          </section>
        </div>
        <div className="df"><span style={{ flex: 1 }} /><button className="a-btn" onClick={onClose}>Cancel</button>
          <button className="a-btn gold" disabled={!cart.length || busy} onClick={async () => { setBusy(true); await onCreate({ table, notes, items: cart.map(i => ({ menu_id: String(i.m.id), name: i.m.name, qty: i.qty })) }); setBusy(false) }}>Place order</button></div>
      </aside>
    </>
  )
}
