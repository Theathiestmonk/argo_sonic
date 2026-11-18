import { useEffect, useMemo, useRef, useState } from 'react'
import { friendlyNavMessage } from '../friendlyNames'
import * as Ic from './icons'
import { money, ageText, hoursText, fmtDist, distClass } from './format'

// ───────────────────────── hero: greeting + KPIs (all derived from live orders / tables) ─────────────────────────
export function Hero({ greeting, night, subtitle, kpis }) {
  return (
    <section className="a-hero">
      <div className="a-card a-hello">
        <h2>{greeting} <span>{night ? '🌙' : '☀️'}</span></h2>
        <p>{subtitle}</p>
      </div>
      {kpis.map(k => (
        <div className="a-card a-kpi" key={k.label} style={{ display: 'flex', alignItems: 'center' }}>
          <div className="ic">{k.icon}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <small>{k.label}</small>
            <div><b>{k.value}</b>{k.note && <em>{k.note}</em>}</div>
            {k.bar != null && <div className="bar"><i style={{ width: `${Math.round(k.bar * 100)}%` }} /></div>}
          </div>
        </div>
      ))}
    </section>
  )
}

// ───────────────────────── telemetry strip under the map ─────────────────────────
export function TelemetryStrip({ drive = {}, sensors = {}, shield = {} }) {
  // /safety_shield/status: shield running + depth feed fresh. Re-render each second so a dead shield flips to OFF.
  const [, tick] = useState(0)
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  useEffect(() => { const i = setInterval(() => tick(n => n + 1), 1000); return () => clearInterval(i) }, [])
  // Close the info card on outside click or Escape.
  useEffect(() => {
    if (!open) return
    const onDown = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown); document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [open])
  const up = shield.ts && Date.now() - shield.ts < 3000
  const shieldText = !up ? 'OFF' : shield.depth ? 'ACTIVE' : 'NO DEPTH'
  const shieldCls = !up ? (shield.ts ? 'bad' : 'dim') : shield.depth ? 'ok' : 'bad'
  const speed = drive.speed
  const ms = (v) => (v == null || !Number.isFinite(v) ? '\u2014' : `${v.toFixed(2)} m/s`)
  const rows = [
    ['Speed', ms(speed), speed == null ? 'dim' : '', 'How fast the robot is moving right now.'],
    ['Left wheel', ms(drive.wheelLeft), drive.wheelLeft == null ? 'dim' : '', 'Speed of the left wheel.'],
    ['Right wheel', ms(drive.wheelRight), drive.wheelRight == null ? 'dim' : '', 'Speed of the right wheel.'],
    ['Lidar', fmtDist(sensors.lidar), distClass(sensors.lidar), 'Distance to the nearest obstacle, from the spinning laser scanner.'],
    ['Depth camera', fmtDist(sensors.depth), distClass(sensors.depth), 'Distance to the nearest obstacle seen by the front camera.'],
    ['Front-left sensor', fmtDist(sensors.usFrontLeft), distClass(sensors.usFrontLeft), 'Distance to anything close on the front-left (ultrasonic).'],
    ['Front-right sensor', fmtDist(sensors.usFrontRight), distClass(sensors.usFrontRight), 'Distance to anything close on the front-right (ultrasonic).'],
    ['Safety shield', shieldText, shieldCls, 'Automatic braking that stops the robot before it hits something. ACTIVE = working.'],
  ]
  return (
    <div className="a-tele-wrap" ref={boxRef}>
      <span className="lbl">Robot readings</span>
      <button className={`a-info ${open ? 'on' : ''}`} onClick={() => setOpen(o => !o)} aria-expanded={open}
        aria-label="Show robot readings" title="Show robot readings">i</button>
      {open && (
        <div className="a-info-pop" role="dialog" aria-label="Robot readings">
          <h4>Robot readings <small>live</small></h4>
          {rows.map(([label, text, cls, hint]) => (
            <div className="r" key={label}>
              <div><span>{label}</span><small>{hint}</small></div>
              <b className={cls}>{text}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ───────────────────────── Robot status ─────────────────────────
// Nav2 startup progress, shown under the battery until navigation is ready:
// just the percentage and a progress bar (no per-step list).
function StartupSteps({ percent = 0 }) {
  return (
    <div className="a-box">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <small>Getting ready</small>
        <b style={{ fontFamily: 'var(--font-heading)', fontSize: 15 }}>{percent}%</b>
      </div>
      <div className="a-loadbar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <i style={{ width: `${Math.max(percent, 3)}%` }} />
      </div>
    </div>
  )
}

export function RobotStatusCard({ connected, battery, task, estopped, onResume, navSteps, navPercent }) {
  const pct = battery?.connected ? Math.max(0, Math.min(100, Math.round(battery.battery_percent))) : null
  const charging = !!battery?.charging
  return (
    <section className="a-card">
      <div className="a-ch">
        <h3>Robot Status</h3>
        <span className={`a-pill ${connected ? '' : 'off'}`}><i />{connected ? 'Connected' : 'Disconnected'} <Ic.Wifi width="18" height="18" style={{ color: connected ? 'var(--a-ok)' : 'var(--a-danger)' }} /></span>
      </div>
      <div className="a-robot">
        <div className="pic"><img src="/models/argo-robot.webp" alt="Argo robot" /></div>
        <div className="side">
          <div className="a-box">
            <small>Battery{charging ? ' · charging' : ''}</small>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 4 }}>
              <span className="big">{pct == null ? '—' : `${pct}%`}</span>
              <div className="a-meter"><i style={{ width: `${pct ?? 0}%`, background: pct != null && pct < 20 ? 'var(--a-danger)' : undefined }} /></div>
            </div>
            <small style={{ color: 'var(--a-muted)' }}>
              {pct == null ? 'Battery monitor offline'
                : charging ? `Full in ${hoursText(battery.estimated_charge_remaining_hours)}`
                : `Runtime: ${hoursText(battery.estimated_remaining_hours)}`}
            </small>
          </div>
          {navSteps && <StartupSteps percent={navPercent} />}
          <div className="a-box" style={{ display: 'grid', gridTemplateColumns: '40px 1fr', gap: 12, alignItems: 'center' }}>
            <div className="ic" style={{ width: 40, height: 40, borderRadius: 11, background: estopped ? 'var(--a-danger-soft)' : 'var(--a-gold-wash)', display: 'grid', placeItems: 'center' }}>
              <Ic.Bag width="22" height="22" style={{ color: estopped ? 'var(--a-danger)' : 'var(--a-gold)' }} />
            </div>
            <div style={{ minWidth: 0 }}>
              <b style={{ fontFamily: 'var(--font-heading)', fontSize: 16, color: estopped ? 'var(--a-danger)' : undefined }}>{estopped ? 'Emergency stop' : task.title}</b>
              <small style={{ display: 'block', color: 'var(--a-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{estopped ? 'Motors are cut' : task.sub}</small>
            </div>
            <div style={{ gridColumn: '1 / -1' }}>
              {estopped
                ? <button className="a-btn ok" style={{ width: '100%', justifyContent: 'center' }} onClick={onResume}>Resume motors</button>
                : <div className={`a-meter ${task.busy ? 'ind' : ''}`}><i style={{ width: task.busy ? undefined : '0%' }} /></div>}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

// ───────────────────────── Active orders (live /orders/<map>) ─────────────────────────
const STATUS = {
  placed:            { label: 'Placed',     cls: 'placed', tab: 'preparing' },
  submitted:         { label: 'Placed',     cls: 'placed', tab: 'preparing' },
  confirmed:         { label: 'Confirmed',  cls: '',       tab: 'preparing' },
  preparing:         { label: 'Preparing',  cls: '',       tab: 'preparing' },
  ready:             { label: 'Ready',      cls: 'ready',  tab: 'ready' },
  out_for_delivery:  { label: 'Delivering', cls: 'deliv',  tab: 'delivering' },
}

export function normalizeOrders(orders) {
  return Object.entries(orders || {})
    .filter(([, o]) => o?.items?.length && STATUS[o.status])
    .map(([table, o]) => ({ table, items: o.items, total: o.total, status: o.status, updatedAt: o.updatedAt, meta: STATUS[o.status] }))
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
}

export function ActiveOrdersCard({ list, currency, onDispatch, onClear, onViewAll }) {
  const [tab, setTab] = useState('all')
  const [menuFor, setMenuFor] = useState(null)
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(id) }, [])
  const count = (t) => t === 'all' ? list.length : list.filter(o => o.meta.tab === t).length
  const shown = tab === 'all' ? list : list.filter(o => o.meta.tab === tab)
  return (
    <section className="a-card" id="a-orders">
      <div className="a-ch"><h3>Active Orders</h3>{onViewAll && <button className="a-link" onClick={onViewAll}>View All</button>}</div>
      <div className="a-tabs">
        {[['all', 'All'], ['preparing', 'Preparing'], ['ready', 'Ready'], ['delivering', 'Delivering']].map(([id, label]) => (
          <button key={id} className={`a-tab ${tab === id ? 'on' : ''}`} onClick={() => setTab(id)}>{label}{(id === 'all' || count(id) > 0) && <em>{count(id)}</em>}</button>
        ))}
      </div>
      {shown.length === 0 ? (
        <div className="a-empty"><b>No active orders</b>Orders taken by Sonic or the POS appear here as soon as they are placed.</div>
      ) : (
        <div className="a-orders" onClick={() => menuFor && setMenuFor(null)}>
          {shown.map(o => {
            const n = o.items.reduce((s, i) => s + (i.qty || 1), 0)
            return (
              <div className="a-order" key={o.table}>
                <div className="ic"><Ic.TableIc /></div>
                <div style={{ minWidth: 0 }}>
                  <b>Table {String(o.table).padStart(2, '0')}</b>
                  <small>{n} item{n === 1 ? '' : 's'} · {o.items.map(i => `${i.qty}× ${i.name}`).join(', ')}</small>
                </div>
                <span className={`a-chip ${o.meta.cls}`}><i />{o.meta.label}</span>
                {o.meta.tab === 'ready'
                  ? <button className="a-btn gold" onClick={() => onDispatch(`Table ${o.table}`)}>Dispatch</button>
                  : <span className="a-age">{ageText(o.updatedAt, now)}</span>}
                <div style={{ position: 'relative' }}>
                  <button className="a-dots" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === o.table ? null : o.table) }} title="More">⋯</button>
                  {menuFor === o.table && (
                    <div className="a-menu">
                      <button onClick={() => { setMenuFor(null); onDispatch(`Table ${o.table}`) }}>Send robot to table</button>
                      <button onClick={() => { setMenuFor(null); onClear(o.table) }} style={{ color: 'var(--a-danger)' }}>Clear order</button>
                      <div style={{ padding: '6px 10px', fontSize: 12, color: 'var(--a-muted)' }}>Total {money(o.total, currency)}</div>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ───────────────────────── POS menu (live GET /menu) ─────────────────────────
export function usePosMenu(launcherUrl, menuApi) {
  // Menu service (backend/pos_api.py → src/argo_mini/menu/menu.json, what the Menu page edits) first; then the
  // launcher's database-backed /menu; last resort the copy bundled with the build.
  const [state, setState] = useState({ items: [], currency: 'INR', loaded: false })
  useEffect(() => {
    let dead = false
    const get = (u) => fetch(u, { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error(r.status); return r.json() })
    const apply = (d) => { if (!dead) setState({ items: d.menu || [], currency: d.settings?.currencyCode || 'INR', loaded: true }) }
    const load = () => get(`${menuApi}/menu`)
      .catch(() => get(`${launcherUrl}/menu`).then(d => (d.menu?.length ? d : Promise.reject())))
      .catch(() => get('/menu.json'))
      .then(apply)
      .catch(() => { if (!dead) setState(s => ({ ...s, loaded: true })) })
    load()
    const id = setInterval(load, 20000)
    return () => { dead = true; clearInterval(id) }
  }, [launcherUrl, menuApi])
  return state
}

export function PosMenuCard({ menu }) {
  const [cat, setCat] = useState('All')
  const [q, setQ] = useState('')
  const items = useMemo(() => menu.items.filter(i => i.available !== false), [menu.items])
  const cats = useMemo(() => ['All', ...Array.from(new Set(items.map(i => i.category).filter(Boolean)))], [items])
  const shown = items.filter(i => (cat === 'All' || i.category === cat) && (!q || i.name.toLowerCase().includes(q.toLowerCase())))
  const price = (i) => i.discountActive && i.discountPercent ? i.price * (1 - i.discountPercent / 100) : i.price
  return (
    <section className="a-card">
      <div className="a-ch" style={{ flexWrap: 'wrap' }}>
        <h3>POS Menu</h3>
        <div className="a-search"><Ic.Search /><input placeholder="Search menu items…" value={q} onChange={e => setQ(e.target.value)} /></div>
        <a className="a-btn sq" href="/order.html" title="Open the POS ordering screen"><Ic.Keyboard width="20" height="20" /></a>
      </div>
      {cats.length > 1 && (
        <div className="a-tabs nowrap">
          {cats.map(c => <button key={c} className={`a-tab ${cat === c ? 'on' : ''}`} onClick={() => setCat(c)}>{c}</button>)}
        </div>
      )}
      {shown.length === 0 ? (
        <div className="a-empty">
          <b>{menu.loaded ? (items.length ? 'No matching items' : 'No menu items from the server') : 'Loading menu…'}</b>
          {menu.loaded && !items.length && 'The live menu service returned no items. Add dishes in Menu, or check the database connection.'}
        </div>
      ) : (
        <div className="a-menugrid">
          {shown.map(i => (
            <div className="a-dish" key={i.id || i.name}>
              <div className="ph">{/^(data:|https?:|\/)/.test(i.image || '') ? <img src={i.image} alt="" loading="lazy" onError={e => { e.currentTarget.remove() }} /> : (i.image || i.name.charAt(0))}</div>
              <div className="t">
                <b title={i.name}>{i.name}</b>
                <small>{money(price(i), menu.currency)}{price(i) !== i.price && <s>{money(i.price, menu.currency)}</s>}</small>
              </div>
            </div>
          ))}
          {shown.some(i => /^\/menu-images\//.test(i.image || '')) && (
            <a className="a-link" style={{ gridColumn: '1 / -1', fontSize: 11.5, fontWeight: 600, textAlign: 'right', opacity: .75 }} href="/menu-images/credits.html" target="_blank" rel="noreferrer">Photo credits · Wikimedia Commons</a>
          )}
        </div>
      )}
    </section>
  )
}

// ───────────────────────── Tables view of the Live Map card (saved table waypoints + live orders) ─────────────────────────
export function TableCards({ tables, orders, currency, goalTable, onSend, onClear }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(id) }, [])
  const list = tables
    .filter(t => /table/i.test(t.name))
    .map(t => {
      const num = String(parseInt((t.name.match(/\d+/) || [t.key])[0], 10))
      const raw = orders?.[num]
      const order = raw?.items?.length && STATUS[raw.status] ? { ...raw, meta: STATUS[raw.status] } : null
      return { ...t, num, order }
    })
    .sort((a, b) => Number(a.num) - Number(b.num))
  if (!list.length) return <div className="a-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeContent: 'center' }}><b>No tables saved on this map</b>Add table locations under Tables to see them here.</div>
  return (
    <div className="a-tablegrid">
      {list.map(t => (
        <div key={t.key} className={`a-tcard ${t.order ? 'busy' : ''} ${goalTable === t.name ? 'goal' : ''}`}>
          <div className="top">
            <b>{t.name}</b>
            {t.order ? <span className={`a-chip ${t.order.meta.cls}`}><i />{t.order.meta.label}</span> : <span className="a-chip free"><i />Free</span>}
          </div>
          {t.order ? (
            <>
              <ul>
                {t.order.items.slice(0, 3).map((i, k) => <li key={i.id || k}><span>{i.qty}× {i.name}</span><span>{money(i.qty * i.price, currency)}</span></li>)}
                {t.order.items.length > 3 && <li className="more">+{t.order.items.length - 3} more</li>}
              </ul>
              <div className="sum"><span>{ageText(t.order.updatedAt, now)} ago</span><b>{money(t.order.total, currency)}</b></div>
            </>
          ) : <div className="none">No open order</div>}
          <div className="acts">
            <button className="a-btn gold" onClick={() => onSend(t.name)}>{goalTable === t.name ? 'Heading here…' : 'Send robot'}</button>
            {t.order && <button className="a-btn danger" onClick={() => onClear(t.num)}>Clear</button>}
          </div>
        </div>
      ))}
    </div>
  )
}
