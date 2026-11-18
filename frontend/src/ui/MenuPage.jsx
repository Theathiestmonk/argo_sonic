import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as Ic from './icons'
import { money } from './format'

/* Menu — view and edit the live menu (src/argo_mini/menu/menu.json) through the Menu API (backend/pos_api.py).
   Every change saves immediately (serialised PUTs of the whole menu, guarded by the `savedAt` version the page
   loaded), so what you see here is exactly what the POS card, the order screens and the voice agent use. */

const LABELS = ['Best Seller', 'Popular', 'Chef Special', 'Street Favorite', 'Hot Deal', 'Weekend Special', 'Healthy Choice', 'Lunch Combo', 'Summer Special', 'Sweet Treat']
const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP', 'AED']
const isSrc = (s) => /^(data:|https?:|\/)/.test(s || '')
const effPrice = (i) => (i.discountActive && i.discountPercent ? Math.round(i.price * (1 - i.discountPercent / 100) * 100) / 100 : i.price)
const blank = (cat) => ({ id: '', name: '', category: cat && cat !== 'All' ? cat : '', price: '', desc: '', image: '', diet: 'veg', eventLabel: '', discountPercent: 0, discountActive: false, available: true, stock: '' })

function Switch({ on, onChange, label }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className={`m-switch ${on ? 'on' : ''}`} onClick={(e) => { e.stopPropagation(); onChange(!on) }}><i /></button>
  )
}

function Photo({ item, className = 'ph' }) {
  return (
    <div className={className}>
      {isSrc(item.image) ? <img src={item.image} alt="" loading="lazy" onError={(e) => e.currentTarget.remove()} /> : <span>{item.image || item.name?.charAt(0) || '＋'}</span>}
    </div>
  )
}

export default function MenuPage({ api, showToast }) {
  const [doc, setDoc] = useState(null)
  const [loadErr, setLoadErr] = useState(null)
  const [readOnly, setReadOnly] = useState(false)
  const [view, setView] = useState(() => { try { return localStorage.getItem('argo_menu_view') || 'cards' } catch { return 'cards' } })
  const [cat, setCat] = useState('All')
  const [diet, setDiet] = useState('all')
  const [avail, setAvail] = useState('all')
  const [q, setQ] = useState('')
  const [edit, setEdit] = useState(null)           // { draft, isNew, errors }
  const [showSettings, setShowSettings] = useState(false)
  const [sel, setSel] = useState(() => new Set())
  const [sort, setSort] = useState({ key: null, dir: 1 })
  const [saving, setSaving] = useState('idle')     // idle | saving | saved | error
  const [undo, setUndo] = useState(null)
  const [renaming, setRenaming] = useState(null)

  const docRef = useRef(null)
  const serverRef = useRef(null)
  const queue = useRef(Promise.resolve())
  const pending = useRef(0)
  const undoTimer = useRef(null)

  // ───────────────────────── load ─────────────────────────
  const load = useCallback(async () => {
    setLoadErr(null)
    try {
      const r = await fetch(`${api}/menu`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const d = await r.json()
      docRef.current = d; serverRef.current = d; setDoc(d); setReadOnly(false)
    } catch (e) {
      try {                                          // read-only fallback: the copy shipped with the dashboard build
        const d = await (await fetch('/menu.json')).json()
        docRef.current = d; setDoc(d); setReadOnly(true)
      } catch { setLoadErr(String(e.message || e)) }
    }
  }, [api])
  useEffect(() => { load() }, [load])

  // ───────────────────────── save (serialised, optimistic) ─────────────────────────
  const commit = useCallback((patch) => {
    if (readOnly || !docRef.current) return
    const next = { ...docRef.current, ...patch }
    docRef.current = next; setDoc(next); setSaving('saving'); pending.current += 1
    queue.current = queue.current.then(async () => {
      const send = docRef.current
      try {
        const r = await fetch(`${api}/menu`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ menu: send.menu, settings: send.settings, base: serverRef.current?.savedAt }) })
        const j = await r.json()
        pending.current -= 1
        if (r.status === 409) { docRef.current = serverRef.current = j.current; setDoc(j.current); showToast(j.error, 'info'); setSaving('idle'); return }
        if (!r.ok) { docRef.current = serverRef.current; setDoc(serverRef.current); showToast(j.error || 'Could not save', 'danger'); setSaving('error'); return }
        serverRef.current = j
        if (pending.current === 0) { docRef.current = j; setDoc(j) } else { docRef.current = { ...docRef.current, savedAt: j.savedAt } }
        setSaving('saved')
      } catch {
        pending.current -= 1
        docRef.current = serverRef.current; setDoc(serverRef.current)
        showToast('Menu service unreachable – change not saved', 'danger'); setSaving('error')
      }
    })
  }, [api, readOnly, showToast])

  const items = doc?.menu || []
  const currency = doc?.settings?.currencyCode || 'INR'

  const patchItem = (id, fields) => commit({ menu: docRef.current.menu.map(i => (i.id === id ? { ...i, ...fields } : i)) })
  const removeItems = (ids) => {
    const removed = docRef.current.menu.map((it, idx) => [idx, it]).filter(([, it]) => ids.includes(it.id))
    commit({ menu: docRef.current.menu.filter(i => !ids.includes(i.id)) })
    setSel(new Set()); setEdit(null)
    clearTimeout(undoTimer.current); setUndo({ removed })
    undoTimer.current = setTimeout(() => setUndo(null), 9000)
  }
  const restore = () => {
    if (!undo) return
    const menu = [...docRef.current.menu]
    undo.removed.forEach(([idx, it]) => menu.splice(Math.min(idx, menu.length), 0, it))
    commit({ menu }); setUndo(null)
  }

  // ───────────────────────── derived ─────────────────────────
  const cats = useMemo(() => { const m = new Map(); items.forEach(i => m.set(i.category, (m.get(i.category) || 0) + 1)); return [...m] }, [items])
  const stats = useMemo(() => ({ veg: items.filter(i => i.diet === 'veg').length, off: items.filter(i => !i.available).length, deals: items.filter(i => i.discountActive && i.discountPercent).length }), [items])
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase()
    let l = items.filter(i => (cat === 'All' || i.category === cat) && (diet === 'all' || i.diet === diet) && (avail === 'all' || (avail === 'yes') === i.available)
      && (!t || `${i.name} ${i.desc} ${i.category} ${i.eventLabel}`.toLowerCase().includes(t)))
    if (sort.key) l = [...l].sort((a, b) => { const x = a[sort.key], y = b[sort.key]; return (typeof x === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir })
    return l
  }, [items, cat, diet, avail, q, sort])

  const chooseView = (v) => { setView(v); try { localStorage.setItem('argo_menu_view', v) } catch { /* private mode */ } }

  // ───────────────────────── editor ─────────────────────────
  const openEdit = (it) => setEdit({ draft: { ...it, price: String(it.price), stock: it.stock ?? '' }, isNew: false, errors: {} })
  const openNew = () => setEdit({ draft: blank(cat), isNew: true, errors: {} })
  const setField = (k, v) => setEdit(e => ({ ...e, draft: { ...e.draft, [k]: v }, errors: { ...e.errors, [k]: undefined } }))

  const nameTaken = (name, id) => items.some(i => i.id !== id && i.name.trim().toLowerCase() === name.trim().toLowerCase())
  const saveEdit = () => {
    const d = edit.draft, errors = {}
    if (!d.name.trim()) errors.name = 'Give the dish a name'
    else if (nameTaken(d.name, d.id)) errors.name = 'Another dish already has this name'
    if (!d.category.trim()) errors.category = 'Pick or type a category'
    if (d.price === '' || isNaN(Number(d.price)) || Number(d.price) < 0) errors.price = 'Enter a price'
    const dp = Number(d.discountPercent || 0); if (dp < 0 || dp > 100 || isNaN(dp)) errors.discountPercent = '0 – 100'
    if (d.stock !== '' && (isNaN(Number(d.stock)) || Number(d.stock) < 0)) errors.stock = 'Whole number, or leave empty'
    if (Object.keys(errors).length) return setEdit(e => ({ ...e, errors }))
    const clean = { ...d, name: d.name.trim(), category: d.category.trim(), price: Number(d.price), discountPercent: dp, desc: d.desc.trim(), eventLabel: d.eventLabel.trim() }
    if (clean.stock === '') delete clean.stock; else clean.stock = Math.floor(Number(clean.stock))
    if (edit.isNew) commit({ menu: [...docRef.current.menu, { ...clean, id: `m-${Date.now()}` }] })
    else commit({ menu: docRef.current.menu.map(i => (i.id === clean.id ? clean : i)) })
    setEdit(null)
  }
  const duplicate = () => { const d = edit.draft; setEdit({ draft: { ...d, id: '', name: `${d.name} (copy)` }, isNew: true, errors: {} }) }

  const upload = async (file) => {
    if (!file) return
    setEdit(e => ({ ...e, uploading: true }))
    try {
      const r = await fetch(`${api}/menu/image?name=${encodeURIComponent(edit.draft.name || 'dish')}`, { method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream' }, body: file })
      const j = await r.json(); if (!r.ok) throw new Error(j.error)
      setEdit(e => ({ ...e, uploading: false, draft: { ...e.draft, image: j.image } }))
    } catch (err) { setEdit(e => ({ ...e, uploading: false })); showToast(`Photo not uploaded: ${err.message}`, 'danger') }
  }

  useEffect(() => {
    const k = (e) => { if (e.key === 'Escape') { setEdit(null); setShowSettings(false) } }
    window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k)
  }, [])

  // ───────────────────────── category rename ─────────────────────────
  const finishRename = (from, to) => {
    setRenaming(null); const t = to.trim()
    if (!t || t === from) return
    commit({ menu: docRef.current.menu.map(i => (i.category === from ? { ...i, category: t } : i)) })
    if (cat === from) setCat(t)
  }

  // ───────────────────────── bulk ─────────────────────────
  const toggleSel = (id) => setSel(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const allSel = shown.length > 0 && shown.every(i => sel.has(i.id))
  const bulkAvail = (v) => { commit({ menu: docRef.current.menu.map(i => (sel.has(i.id) ? { ...i, available: v } : i)) }); setSel(new Set()) }

  if (loadErr) return (
    <div className="a-content"><div className="a-card a-pad m-err">
      <h3>Can't reach the menu service</h3>
      <p>The Menu page saves through a small service on port 8890 of the robot. It isn't answering ({loadErr}).</p>
      <button className="a-btn gold" onClick={load}>Try again</button>
    </div></div>
  )
  if (!doc) return <div className="a-content"><div className="a-empty">Loading menu…</div></div>

  const th = (key, label) => (
    <th className={sort.key === key ? 'sorted' : ''} onClick={() => setSort(s => (s.key === key ? { key, dir: -s.dir } : { key, dir: 1 }))}>{label}{sort.key === key ? (sort.dir > 0 ? ' ↑' : ' ↓') : ''}</th>
  )
  const commitText = (it, field, e, validate) => {
    const v = e.target.value
    if (String(it[field] ?? '') === v) return
    const msg = validate?.(v); if (msg) { showToast(msg, 'danger'); e.target.value = it[field] ?? ''; return }
    patchItem(it.id, { [field]: field === 'price' || field === 'discountPercent' ? Number(v) : v })
  }
  const commitStock = (it, e) => {
    const v = e.target.value.trim()
    if (v === String(it.stock ?? '')) return
    if (v !== '' && (isNaN(Number(v)) || Number(v) < 0)) { showToast('Stock must be a whole number, or empty', 'danger'); e.target.value = it.stock ?? ''; return }
    const next = { ...it }; if (v === '') delete next.stock; else next.stock = Math.floor(Number(v))
    commit({ menu: docRef.current.menu.map(i => (i.id === it.id ? next : i)) })
  }

  return (
    <div className="a-content m-fit">
      {readOnly && <div className="m-banner">The menu service isn't running, so this is a <b>read-only</b> copy that may be out of date. <button className="a-link" onClick={load}>Retry</button></div>}

      <header className="a-card m-head">
        <div>
          <h2>Menu</h2>
          <p>{items.length} dishes · {cats.length} categories · {stats.veg} veg · {items.length - stats.veg} non-veg{stats.off ? ` · ${stats.off} unavailable` : ''}{stats.deals ? ` · ${stats.deals} on offer` : ''}</p>
        </div>
        <div className="m-save" data-state={saving}>{saving === 'saving' ? 'Saving…' : saving === 'saved' ? '✓ All changes saved' : saving === 'error' ? 'Not saved' : readOnly ? '' : 'Edits save automatically'}</div>
        <div className="a-search"><Ic.Search /><input placeholder="Search dishes, descriptions, labels…" value={q} onChange={e => setQ(e.target.value)} /></div>
        <div className="a-seg">
          {[['cards', 'Cards'], ['table', 'Table']].map(([id, l]) => <button key={id} className={view === id ? 'on' : ''} onClick={() => chooseView(id)}>{l}</button>)}
        </div>
        <button className="a-btn sq" title="Currency and tax" onClick={() => setShowSettings(true)} disabled={readOnly}><Ic.Gear width="19" height="19" /></button>
        <button className="a-btn gold" onClick={openNew} disabled={readOnly}>+ Add dish</button>
      </header>

      <div className="m-body">
        <aside className="a-card m-rail">
          <div className="m-rail-sec">
            <button className={`m-cat ${cat === 'All' ? 'on' : ''}`} onClick={() => setCat('All')}><span>All dishes</span><em>{items.length}</em></button>
            {cats.map(([c, n]) => (
              <div key={c} className={`m-cat ${cat === c ? 'on' : ''}`} onClick={() => setCat(c)}>
                {renaming === c
                  ? <input autoFocus defaultValue={c} onClick={e => e.stopPropagation()} onBlur={e => finishRename(c, e.target.value)} onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); if (e.key === 'Escape') setRenaming(null) }} />
                  : <span title={c}>{c}</span>}
                <em>{n}</em>
                {cat === c && renaming !== c && !readOnly && <button className="ren" title="Rename category" onClick={e => { e.stopPropagation(); setRenaming(c) }}>✎</button>}
              </div>
            ))}
          </div>
          <div className="m-rail-sec">
            <small>Diet</small>
            <div className="m-chips">{[['all', 'All'], ['veg', 'Veg'], ['nonveg', 'Non-veg']].map(([v, l]) => <button key={v} className={`a-tab ${diet === v ? 'on' : ''}`} onClick={() => setDiet(v)}>{v !== 'all' && <i className={`dot ${v}`} />}{l}</button>)}</div>
            <small>Availability</small>
            <div className="m-chips">{[['all', 'All'], ['yes', 'Available'], ['no', 'Unavailable']].map(([v, l]) => <button key={v} className={`a-tab ${avail === v ? 'on' : ''}`} onClick={() => setAvail(v)}>{l}</button>)}</div>
          </div>
        </aside>

        <main className="m-main">
          {shown.length === 0 && <div className="a-card a-empty"><b>No dishes match</b>Clear the search or filters, or add a dish.</div>}

          {view === 'cards' && shown.length > 0 && (
            <div className="m-grid">
              {shown.map(it => (
                <article key={it.id} className={`m-card ${it.available ? '' : 'off'}`} onClick={() => openEdit(it)}>
                  <div className="top">
                    <Photo item={it} />
                    <i className={`dot big ${it.diet}`} title={it.diet === 'veg' ? 'Vegetarian' : 'Non-vegetarian'} />
                    {it.eventLabel && <span className="tag">{it.eventLabel}</span>}
                    {it.discountActive && it.discountPercent > 0 && <span className="off-pill">{it.discountPercent}% off</span>}
                    {!it.available && <span className="unav">Unavailable</span>}
                  </div>
                  <div className="b">
                    <h4 title={it.name}>{it.name}</h4>
                    <div className="meta"><b>{money(effPrice(it), currency)}</b>{effPrice(it) !== it.price && <s>{money(it.price, currency)}</s>}<span>{it.category}</span></div>
                    <p>{it.desc || <i>No description</i>}</p>
                    <div className="f" onClick={e => e.stopPropagation()}>
                      <Switch on={it.available} label="Available" onChange={v => patchItem(it.id, { available: v })} />
                      <span>{it.available ? 'Available' : 'Unavailable'}</span>
                      <button className="a-btn" onClick={() => openEdit(it)}>Edit</button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}

          {view === 'table' && shown.length > 0 && (
            <div className="a-card m-tablewrap">
              {sel.size > 0 && (
                <div className="m-bulk"><b>{sel.size} selected</b>
                  <button className="a-btn" onClick={() => bulkAvail(true)}>Mark available</button>
                  <button className="a-btn" onClick={() => bulkAvail(false)}>Mark unavailable</button>
                  <button className="a-btn danger" onClick={() => removeItems([...sel])}>Delete</button>
                  <button className="a-link" onClick={() => setSel(new Set())}>Clear</button></div>
              )}
              <table className="m-table">
                <thead><tr>
                  <th className="c"><input type="checkbox" checked={allSel} onChange={() => setSel(allSel ? new Set() : new Set(shown.map(i => i.id)))} /></th>
                  <th />{th('name', 'Dish')}{th('category', 'Category')}<th>Diet</th>{th('price', `Price (${currency})`)}{th('discountPercent', 'Offer %')}<th>Offer on</th><th>Available</th><th>Stock</th><th />
                </tr></thead>
                <tbody>
                  {shown.map(it => (
                    <tr key={it.id} className={`${sel.has(it.id) ? 'sel' : ''} ${it.available ? '' : 'off'}`}>
                      <td className="c"><input type="checkbox" checked={sel.has(it.id)} onChange={() => toggleSel(it.id)} /></td>
                      <td><Photo item={it} className="ph th" /></td>
                      <td><input className="cell wide" key={`n${it.id}${it.name}`} defaultValue={it.name} disabled={readOnly}
                        onBlur={e => commitText(it, 'name', e, v => (!v.trim() ? 'A dish needs a name' : nameTaken(v, it.id) ? 'Another dish already has this name' : null))} onKeyDown={e => e.key === 'Enter' && e.currentTarget.blur()} /></td>
                      <td><input className="cell" list="m-cats" key={`c${it.id}${it.category}`} defaultValue={it.category} disabled={readOnly}
                        onBlur={e => commitText(it, 'category', e, v => (!v.trim() ? 'A dish needs a category' : null))} onKeyDown={e => e.key === 'Enter' && e.currentTarget.blur()} /></td>
                      <td><select className="cell" value={it.diet} disabled={readOnly} onChange={e => patchItem(it.id, { diet: e.target.value })}><option value="veg">Veg</option><option value="nonveg">Non-veg</option></select></td>
                      <td><input className="cell num" inputMode="decimal" key={`p${it.id}${it.price}`} defaultValue={it.price} disabled={readOnly}
                        onBlur={e => commitText(it, 'price', e, v => (v === '' || isNaN(Number(v)) || Number(v) < 0 ? 'Enter a valid price' : null))} onKeyDown={e => e.key === 'Enter' && e.currentTarget.blur()} /></td>
                      <td><input className="cell num sm" inputMode="numeric" key={`d${it.id}${it.discountPercent}`} defaultValue={it.discountPercent} disabled={readOnly}
                        onBlur={e => commitText(it, 'discountPercent', e, v => (isNaN(Number(v)) || Number(v) < 0 || Number(v) > 100 ? 'Offer must be 0 – 100' : null))} onKeyDown={e => e.key === 'Enter' && e.currentTarget.blur()} /></td>
                      <td><Switch on={!!it.discountActive} label="Offer on" onChange={v => !readOnly && patchItem(it.id, { discountActive: v })} /></td>
                      <td><Switch on={it.available} label="Available" onChange={v => !readOnly && patchItem(it.id, { available: v })} /></td>
                      <td><input className="cell num sm" inputMode="numeric" placeholder="–" key={`s${it.id}${it.stock}`} defaultValue={it.stock ?? ''} disabled={readOnly} onBlur={e => commitStock(it, e)} onKeyDown={e => e.key === 'Enter' && e.currentTarget.blur()} /></td>
                      <td><button className="a-btn" onClick={() => openEdit(it)}>Edit</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <datalist id="m-cats">{cats.map(([c]) => <option key={c} value={c} />)}</datalist>
            </div>
          )}
        </main>
      </div>

      {undo && <div className="m-undo">{undo.removed.length} dish{undo.removed.length === 1 ? '' : 'es'} deleted <button onClick={restore}>Undo</button></div>}

      {edit && (
        <>
          <div className="m-scrim" onClick={() => setEdit(null)} />
          <aside className="m-drawer" role="dialog" aria-label="Edit dish">
            <div className="dh"><h3>{edit.isNew ? 'New dish' : 'Edit dish'}</h3><button onClick={() => setEdit(null)} aria-label="Close">✕</button></div>
            <div className="db">
              <div className="m-photo">
                <Photo item={edit.draft} className="ph big" />
                <div>
                  <label className="a-btn">{edit.uploading ? 'Uploading…' : 'Upload photo'}<input type="file" accept="image/*" hidden onChange={e => upload(e.target.files?.[0])} /></label>
                  {edit.draft.image && <button className="a-btn" onClick={() => setField('image', '')}>Remove</button>}
                  <input className="m-in" placeholder="…or paste an image URL / an emoji" value={edit.draft.image} onChange={e => setField('image', e.target.value)} />
                  <small>Photos are cropped to 16:10 and stored on the robot.</small>
                </div>
              </div>

              <label className="m-f"><span>Name</span><input className={`m-in ${edit.errors.name ? 'bad' : ''}`} value={edit.draft.name} onChange={e => setField('name', e.target.value)} autoFocus />{edit.errors.name && <em>{edit.errors.name}</em>}</label>
              <div className="m-row">
                <label className="m-f"><span>Category</span><input className={`m-in ${edit.errors.category ? 'bad' : ''}`} list="m-cats2" value={edit.draft.category} onChange={e => setField('category', e.target.value)} />{edit.errors.category && <em>{edit.errors.category}</em>}</label>
                <label className="m-f"><span>Price ({currency})</span><input className={`m-in ${edit.errors.price ? 'bad' : ''}`} inputMode="decimal" value={edit.draft.price} onChange={e => setField('price', e.target.value)} />{edit.errors.price && <em>{edit.errors.price}</em>}</label>
              </div>
              <datalist id="m-cats2">{cats.map(([c]) => <option key={c} value={c} />)}</datalist>
              <div className="m-f"><span>Diet</span>
                <div className="a-seg">{[['veg', 'Vegetarian'], ['nonveg', 'Non-vegetarian']].map(([v, l]) => <button type="button" key={v} className={edit.draft.diet === v ? 'on' : ''} onClick={() => setField('diet', v)}><i className={`dot ${v}`} /> {l}</button>)}</div>
              </div>
              <label className="m-f"><span>Description</span><textarea className="m-in" rows={3} value={edit.draft.desc} onChange={e => setField('desc', e.target.value)} placeholder="One appetising sentence – the robot reads this aloud" /></label>
              <div className="m-row">
                <label className="m-f"><span>Label</span><input className="m-in" list="m-labels" value={edit.draft.eventLabel} onChange={e => setField('eventLabel', e.target.value)} placeholder="e.g. Best Seller" /></label>
                <label className="m-f"><span>Stock</span><input className={`m-in ${edit.errors.stock ? 'bad' : ''}`} inputMode="numeric" value={edit.draft.stock} onChange={e => setField('stock', e.target.value)} placeholder="not tracked" />{edit.errors.stock && <em>{edit.errors.stock}</em>}</label>
              </div>
              <datalist id="m-labels">{LABELS.map(l => <option key={l} value={l} />)}</datalist>
              <div className="m-row">
                <label className="m-f"><span>Offer %</span><input className={`m-in ${edit.errors.discountPercent ? 'bad' : ''}`} inputMode="numeric" value={edit.draft.discountPercent} onChange={e => setField('discountPercent', e.target.value)} />{edit.errors.discountPercent && <em>{edit.errors.discountPercent}</em>}</label>
                <div className="m-f"><span>Offer is on</span><Switch on={edit.draft.discountActive} label="Offer on" onChange={v => setField('discountActive', v)} /></div>
              </div>
              <div className="m-f inline"><Switch on={edit.draft.available} label="Available" onChange={v => setField('available', v)} /><span>Available to order</span></div>
              {Number(edit.draft.discountPercent) > 0 && edit.draft.discountActive && edit.draft.price !== '' && <p className="m-hint">Guests pay <b>{money(effPrice({ ...edit.draft, price: Number(edit.draft.price), discountPercent: Number(edit.draft.discountPercent) }), currency)}</b> after the offer.</p>}
            </div>
            <div className="df">
              {!edit.isNew && <button className="a-btn danger" onClick={() => { if (window.confirm(`Delete “${edit.draft.name}”?`)) removeItems([edit.draft.id]) }}>Delete</button>}
              {!edit.isNew && <button className="a-btn" onClick={duplicate}>Duplicate</button>}
              <span style={{ flex: 1 }} />
              <button className="a-btn" onClick={() => setEdit(null)}>Cancel</button>
              <button className="a-btn gold" onClick={saveEdit}>{edit.isNew ? 'Add dish' : 'Save changes'}</button>
            </div>
          </aside>
        </>
      )}

      {showSettings && (
        <>
          <div className="m-scrim" onClick={() => setShowSettings(false)} />
          <div className="m-modal" role="dialog" aria-label="Menu settings">
            <h3>Menu settings</h3>
            <label className="m-f"><span>Currency</span>
              <select className="m-in" defaultValue={currency} id="m-cur">{[...new Set([currency, ...CURRENCIES])].map(c => <option key={c}>{c}</option>)}</select></label>
            <label className="m-f"><span>Tax on the bill (%)</span><input className="m-in" id="m-tax" inputMode="decimal" defaultValue={doc.settings?.taxPercent ?? 0} /></label>
            <div className="df">
              <span style={{ flex: 1 }} />
              <button className="a-btn" onClick={() => setShowSettings(false)}>Cancel</button>
              <button className="a-btn gold" onClick={() => {
                const tax = Number(document.getElementById('m-tax').value)
                if (isNaN(tax) || tax < 0 || tax > 100) return showToast('Tax must be 0 – 100', 'danger')
                commit({ settings: { currencyCode: document.getElementById('m-cur').value, taxPercent: tax } }); setShowSettings(false)
              }}>Save</button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
