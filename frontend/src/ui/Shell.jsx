import { useEffect, useState } from 'react'
import * as Ic from './icons'
import { openNow } from './format'

export function useRestaurant() {
  // Same restaurant.json the voice agent uses (NLP/data/restaurant.json, symlinked into public/).
  const [r, setR] = useState(null)
  useEffect(() => { fetch('/restaurant.json').then(x => x.json()).then(setR).catch(() => {}) }, [])
  return r
}

const ITEMS = [
  ['overview', 'Overview', Ic.Home],
  ['orders', 'Orders', Ic.Clip],
  ['menu', 'Menu', Ic.Menu],
  ['tables', 'Tables', Ic.Tables],
  ['map', 'Map & Navigation', Ic.MapNav],
  ['settings', 'Settings', Ic.Gear],
]

export default function Shell({ active, onNav, badges = {}, connected, restaurant, top, sideActions, sideTop, children }) {
  const open = restaurant ? openNow(restaurant.hours) : null
  return (
    <div className="argo">
      <aside className="a-side">
        {sideTop && <div className="a-side-top">{sideTop}</div>}
        <nav className="a-nav">
          {ITEMS.map(([id, label, Icon]) => (
            <button key={id} className={active === id ? 'on' : ''} onClick={() => onNav(id)} title={label}>
              <Icon /> <span>{label}</span>
              {badges[id] > 0 && <em className="badge" style={{ fontStyle: 'normal' }}>{badges[id]}</em>}
            </button>
          ))}
        </nav>
        <div className="a-side-foot">
          {sideActions && <div className="a-side-actions">{sideActions}</div>}
          {restaurant && (
            <div className="a-rest">
              <small>Restaurant</small>
              <b>{restaurant.name}</b>
              <div className={`st ${open === false ? 'closed' : ''}`}>
                {open !== null && <><i />{open ? 'Open' : 'Closed'}</>}
                <span>{restaurant.hours?.replace(/^Open (every day )?/i, '')}</span>
              </div>
            </div>
          )}
        </div>
      </aside>
      <div className="a-main">
        <header className="a-top">
          <span className={`a-conn ${connected ? 'ok' : ''}`}><i />{connected ? 'Argo connected' : 'Not connected'}</span>
          <div className="spacer" />
          {top}
          <div className="a-admin"><span className="av">A</span>Admin</div>
        </header>
        {children}
      </div>
    </div>
  )
}
