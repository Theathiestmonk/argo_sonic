import { useCallback, useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { useLocation } from '../lib/LocationContext'

// Explicit column list, NOT `select('*')` — menu_items.image_url stores a
// base64 data: URI (see restaurant_ops_schema.sql), so a list query that
// includes it would pull megabytes of inline images for every item. Only
// the edit form (openEditor below) fetches image_url, for one item at a time.
const LIST_COLUMNS = 'menu_item_id, item_name, description, price, is_available, category_id, extra'

const emptyDraft = { item_name: '', description: '', price: '', is_available: true, category_name: '' }

export default function Menu() {
  const { selectedLocationId } = useLocation()
  const [categories, setCategories] = useState([])
  const [items, setItems] = useState([])
  const [settings, setSettings] = useState({ currency_code: 'USD', tax_percent: 0 })
  const [loading, setLoading] = useState(false)
  const [editingId, setEditingId] = useState(null)   // menu_item_id being edited, or 'new'
  const [draft, setDraft] = useState(emptyDraft)
  const [imageUrl, setImageUrl] = useState(null)      // only loaded for the item currently being edited
  const [error, setError] = useState(null)

  const categoryName = (id) => categories.find(c => c.category_id === id)?.name || 'Uncategorized'

  const load = useCallback(async () => {
    if (!selectedLocationId) return
    setLoading(true)
    const [catRes, itemRes, settingsRes] = await Promise.all([
      supabase.from('menu_categories').select('category_id, name').eq('location_id', selectedLocationId).order('display_order'),
      supabase.from('menu_items').select(LIST_COLUMNS).eq('location_id', selectedLocationId).order('item_name'),
      supabase.from('menu_settings').select('currency_code, tax_percent').eq('location_id', selectedLocationId).maybeSingle(),
    ])
    setCategories(catRes.data || [])
    setItems(itemRes.data || [])
    if (settingsRes.data) setSettings(settingsRes.data)
    setLoading(false)
  }, [selectedLocationId])

  useEffect(() => { load() }, [load])

  const getOrCreateCategoryId = async (name) => {
    const trimmed = (name || '').trim() || 'Uncategorized'
    const existing = categories.find(c => c.name.toLowerCase() === trimmed.toLowerCase())
    if (existing) return existing.category_id
    const { data, error: insertError } = await supabase
      .from('menu_categories')
      .insert({ location_id: selectedLocationId, name: trimmed })
      .select('category_id, name')
      .single()
    if (insertError) throw insertError
    setCategories(prev => [...prev, data])
    return data.category_id
  }

  const openEditor = async (item) => {
    setError(null)
    if (item === 'new') {
      setEditingId('new')
      setDraft(emptyDraft)
      setImageUrl(null)
      return
    }
    setEditingId(item.menu_item_id)
    setDraft({
      item_name: item.item_name,
      description: item.description || '',
      price: item.price,
      is_available: item.is_available,
      category_name: categoryName(item.category_id),
    })
    // Fetch image_url lazily, only for this one item.
    const { data } = await supabase.from('menu_items').select('image_url').eq('menu_item_id', item.menu_item_id).single()
    setImageUrl(data?.image_url ?? null)
  }

  const save = async () => {
    setError(null)
    try {
      const category_id = await getOrCreateCategoryId(draft.category_name)
      const payload = {
        item_name: draft.item_name.trim(),
        description: draft.description || null,
        price: Number(draft.price),
        is_available: draft.is_available,
        category_id,
        location_id: selectedLocationId,
      }
      if (editingId === 'new') {
        const { error: insertError } = await supabase.from('menu_items').insert(payload)
        if (insertError) throw insertError
      } else {
        // Row-level update of ONE item — never a bulk replace. The on-robot
        // menu.html (backend/launcher.py's POST /menu) does a full-overwrite
        // save; if this page did too, a save from either side could delete
        // the other's concurrent edits. Row-level here means last-writer-wins
        // per item instead of per menu.
        const { error: updateError } = await supabase.from('menu_items').update(payload).eq('menu_item_id', editingId)
        if (updateError) throw updateError
      }
      setEditingId(null)
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  const remove = async (menu_item_id) => {
    if (!confirm('Delete this item?')) return
    const { error: deleteError } = await supabase.from('menu_items').delete().eq('menu_item_id', menu_item_id)
    if (deleteError) { setError(deleteError.message); return }
    setItems(prev => prev.filter(i => i.menu_item_id !== menu_item_id))
  }

  const saveSettings = async () => {
    const { error: settingsError } = await supabase
      .from('menu_settings')
      .upsert({ location_id: selectedLocationId, ...settings, saved_at: new Date().toISOString() })
    if (settingsError) setError(settingsError.message)
  }

  if (!selectedLocationId) return <p style={{ color: 'var(--muted)' }}>No location selected yet.</p>

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20 }}>Menu</div>
        <button className="btn-gold" onClick={() => openEditor('new')}>+ Add item</button>
      </div>

      <div className="glass-dense" style={{ padding: '14px 18px', display: 'flex', gap: 16, alignItems: 'center', marginBottom: 20 }}>
        <span className="label-xs">Settings</span>
        <label style={{ fontSize: 12.5, color: 'var(--muted)' }}>Currency</label>
        <input
          value={settings.currency_code}
          onChange={e => setSettings(s => ({ ...s, currency_code: e.target.value }))}
          style={{ width: 60, padding: '6px 8px', borderRadius: 6, background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)' }}
        />
        <label style={{ fontSize: 12.5, color: 'var(--muted)' }}>Tax %</label>
        <input
          type="number" step="0.01" value={settings.tax_percent}
          onChange={e => setSettings(s => ({ ...s, tax_percent: e.target.value }))}
          style={{ width: 70, padding: '6px 8px', borderRadius: 6, background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)' }}
        />
        <button className="btn-ghost" style={{ padding: '6px 14px', fontSize: 12 }} onClick={saveSettings}>Save</button>
      </div>

      {error && <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 12 }}>{error}</div>}

      {editingId && (
        <div className="glass-dense" style={{ padding: 20, marginBottom: 20, borderColor: 'rgba(226,179,92,0.3)' }}>
          <div className="label-xs" style={{ color: 'var(--gold-bright)', marginBottom: 12 }}>
            {editingId === 'new' ? 'New item' : 'Edit item'}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
            <input placeholder="Name" value={draft.item_name} onChange={e => setDraft(d => ({ ...d, item_name: e.target.value }))} style={inputStyle} />
            <input placeholder="Category" value={draft.category_name} onChange={e => setDraft(d => ({ ...d, category_name: e.target.value }))} style={inputStyle} />
            <input placeholder="Price" type="number" step="0.01" value={draft.price} onChange={e => setDraft(d => ({ ...d, price: e.target.value }))} style={inputStyle} />
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={draft.is_available} onChange={e => setDraft(d => ({ ...d, is_available: e.target.checked }))} />
              Available
            </label>
          </div>
          <textarea
            placeholder="Description" value={draft.description} onChange={e => setDraft(d => ({ ...d, description: e.target.value }))}
            style={{ ...inputStyle, width: '100%', minHeight: 60, marginBottom: 10 }}
          />
          {imageUrl && <img src={imageUrl} alt="" style={{ maxHeight: 80, borderRadius: 8, marginBottom: 10 }} />}
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn-ok" onClick={save}>Save</button>
            <button className="btn-ghost" onClick={() => setEditingId(null)}>Cancel</button>
          </div>
        </div>
      )}

      {loading ? (
        <p style={{ color: 'var(--muted)' }}>Loading…</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map(item => (
            <div key={item.menu_item_id} className="glass-dense" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 14 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>
                  {item.item_name} {!item.is_available && <span style={{ color: 'var(--danger)', fontSize: 11 }}>(unavailable)</span>}
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{categoryName(item.category_id)} · {settings.currency_code} {item.price}</div>
              </div>
              <button className="btn-ghost" style={{ padding: '6px 12px', fontSize: 12 }} onClick={() => openEditor(item)}>Edit</button>
              <button className="btn-ghost" style={{ padding: '6px 12px', fontSize: 12, color: 'var(--danger)' }} onClick={() => remove(item.menu_item_id)}>Delete</button>
            </div>
          ))}
          {items.length === 0 && <p style={{ color: 'var(--muted)' }}>No menu items yet.</p>}
        </div>
      )}
    </div>
  )
}

const inputStyle = {
  padding: '8px 10px', borderRadius: 8, background: 'rgba(255,255,255,0.04)',
  border: '1px solid var(--border-glass)', color: 'var(--text)', fontSize: 13,
}
