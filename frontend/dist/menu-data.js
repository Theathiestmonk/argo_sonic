// Shared menu-catalog storage, used by order.html (reads it) and menu.html (writes it).
// Persisted to localStorage so items added in Menu Settings show up on the Order page.
const MENU_STORAGE_KEY = 'argo_menu'

// Backend mirror (backend/launcher.py's GET/POST /menu) — the same host this page was
// served from, port 8888, matching frontend/src/App.jsx's launcherUrl() convention. This
// is what lets a separate process (the sonic/ voice agent) see the same menu staff edit
// here: every save below also POSTs here, and menu.html pulls this on load so a Jetson
// running both this page and Sonic always shows one real, current stock/availability
// picture instead of two independently-drifting copies (localStorage vs whatever Sonic
// last fetched).
const LAUNCHER_URL = `${location.protocol}//${location.hostname}:8888`

function _syncToBackendFireAndForget() {
  try {
    const menu = JSON.parse(localStorage.getItem(MENU_STORAGE_KEY) || 'null') || []
    const settings = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || 'null') || DEFAULT_SETTINGS
    fetch(`${LAUNCHER_URL}/menu`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ menu, settings, savedAt: new Date().toISOString() }),
    }).catch(() => {}) // launcher.py not running is a normal, expected state — never block on it
  } catch {}
}

// Pulls the backend's copy into localStorage, if reachable — call once on page load
// before rendering (see menu.html's init sequence, same pattern already used for
// ArgoBackupSync.init()). Short timeout since a missing/unreachable launcher.py is
// routine (e.g. testing purely in a browser with no robot backend running at all),
// not an error worth waiting on.
async function refreshMenuFromBackend() {
  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 1500)
    const res = await fetch(`${LAUNCHER_URL}/menu`, { signal: ctrl.signal })
    clearTimeout(timer)
    if (!res.ok) return false
    const data = await res.json()
    if (!Array.isArray(data.menu) || !data.menu.length) return false
    saveMenu(data.menu)
    saveSettings({ ...DEFAULT_SETTINGS, ...(data.settings || {}) })
    return true
  } catch {
    return false
  }
}

const DEFAULT_MENU = [
  { id: 'm1', name: 'Classic Chicken Burger',      category: 'Burger',    price: 5.47, image: '🍔', desc: 'Grilled chicken, lettuce and house sauce in a soft bun.', stock: 25, available: true, diet: 'nonveg' },
  { id: 'm2', name: 'Double Chicken Cheese Burger', category: 'Burger',    price: 6.10, image: '🍔', desc: 'Double patty with melted cheese and pickles.', stock: 18, available: true, diet: 'nonveg' },
  { id: 'm3', name: 'Chicken Pizza',                category: 'Pizza',     price: 7.00, image: '🍕', desc: 'Wood-fired base topped with roasted chicken.', stock: 12, available: true, diet: 'nonveg' },
  { id: 'm4', name: 'Chicken Mushroom Pizza',       category: 'Pizza',     price: 7.50, image: '🍕', desc: 'Mushroom, mozzarella and a smoky chicken topping.', stock: 0, available: true, diet: 'nonveg' },
  { id: 'm5', name: 'Triple Scoop Ice Cream',       category: 'Ice Cream', price: 2.47, image: '🍨', desc: 'Three scoops, your pick of flavours.', stock: 30, available: true, diet: 'veg' },
  { id: 'm6', name: 'Vanilla Ice Cream',            category: 'Ice Cream', price: 2.00, image: '🍦', desc: 'Classic single scoop, served cold.', stock: 40, available: true, diet: 'veg' },
  { id: 'm7', name: 'Orange Juice',                 category: 'Juice',     price: 1.20, image: '🧃', desc: 'Freshly squeezed, no added sugar.', stock: 50, available: true, diet: 'veg' },
  { id: 'm8', name: 'Green Detox Juice',            category: 'Juice',     price: 3.00, image: '🥤', desc: 'Spinach, apple and celery blend.', stock: 20, available: true, diet: 'veg' },
  { id: 'm9', name: 'French Fries',                 category: 'Sides',    price: 2.20, image: '🍟', desc: 'Crispy golden fries, lightly salted.', stock: 8, available: false, diet: 'veg' },
]

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]))
}

function loadMenu() {
  try {
    const raw = localStorage.getItem(MENU_STORAGE_KEY)
    if (!raw) { saveMenu(DEFAULT_MENU); return DEFAULT_MENU.slice() }
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) && parsed.length ? parsed : DEFAULT_MENU.slice()
  } catch { return DEFAULT_MENU.slice() }
}

function saveMenu(items) {
  localStorage.setItem(MENU_STORAGE_KEY, JSON.stringify(items))
  window.dispatchEvent(new CustomEvent('argo:data-saved'))
  _syncToBackendFireAndForget()
}

function menuCategories(items) {
  return [...new Set(items.map(i => i.category).filter(Boolean))]
}

// Category → emoji used when a card has no real photo (default seed items, or
// user-added items left without an image upload).
const CATEGORY_ICONS = {
  Burger: '🍔', Pizza: '🍕', 'Ice Cream': '🍨', Juice: '🧃', Sides: '🍟', Drinks: '🥤', Dessert: '🍰',
}
function categoryIcon(category) {
  return CATEGORY_ICONS[category] || '🍽️'
}

function isImageSrc(src) {
  return /^(data:|https?:)/.test(src || '')
}

// Renders a real <img> when a photo was uploaded; otherwise a small icon
// badge on a plain plate background — kept deliberately understated (a
// single small glyph, not a full-tile icon) so real photos read as the
// primary visual and the fallback doesn't compete with them.
//
// `loading="lazy"` defers offscreen photos so a catalog full of external
// image URLs doesn't all fetch at once on load. `onerror` falls back to the
// icon tile instead of leaving a blank box when a URL is slow, hotlink-
// blocked, or just broken (common with third-party image links in bulk CSVs).
function thumbHtml(item, sizeClass) {
  const cls = 'item-thumb' + (sizeClass ? ' ' + sizeClass : '')
  const icon = (item.image && !isImageSrc(item.image)) ? item.image : categoryIcon(item.category)
  if (isImageSrc(item.image)) {
    const fallback = escapeHtml(icon).replace(/'/g, '&#39;')
    return `<div class="${cls}"><img src="${escapeHtml(item.image)}" alt="${escapeHtml(item.name)}" loading="lazy" decoding="async" onerror="handleThumbBroken(this, '${fallback}')"></div>`
  }
  return `<div class="${cls} placeholder-thumb"><span class="thumb-badge">${icon}</span></div>`
}

function handleThumbBroken(imgEl, fallbackIcon) {
  const wrap = imgEl.parentElement
  if (!wrap) return
  wrap.classList.add('placeholder-thumb')
  wrap.innerHTML = `<span class="thumb-badge">${fallbackIcon}</span>`
}

// Shared downscale step so a stored data URI stays a sane size regardless of
// where the source image came from (a phone photo upload or a fetched URL).
function resizeImageToDataUrl(imgEl, maxSize) {
  const scale = Math.min(1, maxSize / Math.max(imgEl.width, imgEl.height))
  const w = Math.round(imgEl.width * scale)
  const h = Math.round(imgEl.height * scale)
  const canvas = document.createElement('canvas')
  canvas.width = w; canvas.height = h
  canvas.getContext('2d').drawImage(imgEl, 0, 0, w, h)
  return canvas.toDataURL('image/jpeg', 0.82)
}

// Downscales an uploaded image to keep localStorage usage sane before it's
// stored as a data URI (raw phone photos would otherwise blow past quota fast).
function fileToDataUrl(file, maxSize = 480) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error)
    reader.onload = () => {
      const img = new Image()
      img.onerror = reject
      img.onload = () => resolve(resizeImageToDataUrl(img, maxSize))
      img.src = reader.result
    }
    reader.readAsDataURL(file)
  })
}

// Fetches an external image URL (e.g. a link pasted into a bulk CSV) and
// converts it to a local data URI, so the item no longer depends on that
// outside host at all — no more re-fetching it on every reload, no pop-in
// while scrolling past it, and it can't go down/get hotlink-blocked later.
// Needs the host to allow cross-origin fetches (CORS); if it doesn't, this
// throws and the caller should just leave the original URL in place.
async function cacheImageAsDataUrl(url, maxSize = 480) {
  const res = await fetch(url, { mode: 'cors' })
  if (!res.ok) throw new Error('HTTP ' + res.status)
  const blob = await res.blob()
  const blobDataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error)
    reader.onload = () => resolve(reader.result)
    reader.readAsDataURL(blob)
  })
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onerror = () => reject(new Error('Downloaded file is not a valid image'))
    img.onload = () => resolve(resizeImageToDataUrl(img, maxSize))
    img.src = blobDataUrl
  })
}

function isExternalImageUrl(src) {
  return /^https?:/.test(src || '')
}

// ── Store settings: currency + tax rate, shared by order.html and menu.html ──
const SETTINGS_STORAGE_KEY = 'argo_settings'
const DEFAULT_SETTINGS = { currencyCode: 'USD', taxPercent: 10 }
const CURRENCY_OPTIONS = [
  { code: 'USD', symbol: '$', label: 'US Dollar ($)' },
  { code: 'INR', symbol: '₹', label: 'Indian Rupee (₹)' },
  { code: 'EUR', symbol: '€', label: 'Euro (€)' },
  { code: 'GBP', symbol: '£', label: 'British Pound (£)' },
  { code: 'JPY', symbol: '¥', label: 'Japanese Yen (¥)' },
  { code: 'AUD', symbol: 'A$', label: 'Australian Dollar (A$)' },
]

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return { ...DEFAULT_SETTINGS, ...parsed }
  } catch { return { ...DEFAULT_SETTINGS } }
}

function saveSettings(settings) {
  localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings))
  window.dispatchEvent(new CustomEvent('argo:data-saved'))
  _syncToBackendFireAndForget()
}

function currencySymbol(settings) {
  return (CURRENCY_OPTIONS.find(c => c.code === settings.currencyCode) || CURRENCY_OPTIONS[0]).symbol
}

function formatMoney(amount, settings) {
  return currencySymbol(settings) + Number(amount).toFixed(2)
}

// ── Per-item event discounts (e.g. a weekend sale on one dish) ──
function hasActiveDiscount(item) {
  return !!item.discountActive && Number(item.discountPercent) > 0
}

function effectivePrice(item) {
  if (!hasActiveDiscount(item)) return Number(item.price)
  const pct = Math.min(95, Math.max(0, Number(item.discountPercent)))
  return Number(item.price) * (1 - pct / 100)
}

// Price markup shared by the item grid and the bill/catalog cards — shows a
// struck-through original price next to the discounted one when a discount
// is active, otherwise just the plain price.
function priceBlockHtml(item, settings) {
  if (!hasActiveDiscount(item)) {
    return `<span class="price-now">${formatMoney(item.price, settings)}</span>`
  }
  return `<span class="price-was">${formatMoney(item.price, settings)}</span>` +
    `<span class="price-now">${formatMoney(effectivePrice(item), settings)}</span>`
}

function discountBadgeHtml(item) {
  if (!hasActiveDiscount(item)) return ''
  const label = item.eventLabel ? escapeHtml(item.eventLabel) : `${Number(item.discountPercent)}% OFF`
  return `<span class="discount-badge">${label}</span>`
}

// ── Stock + availability ──
// Stock is optional — a missing/blank value means "unlimited, not tracked".
function hasTrackedStock(item) {
  return typeof item.stock === 'number' && !isNaN(item.stock)
}

// The manual toggle wins for "unavailable"; a tracked stock of 0 always
// forces unavailable too, regardless of the toggle (can't sell what's out).
function isAvailable(item) {
  if (item.available === false) return false
  if (hasTrackedStock(item) && item.stock <= 0) return false
  return true
}

function stockLabel(item) {
  return hasTrackedStock(item) ? `Stock: ${item.stock}` : 'Unlimited stock'
}

// ── Veg / Non-Veg ──
// Normalizes whatever's stored in `diet` before comparing — a strict
// `!== 'nonveg'` check missed values like "Non-veg" or "NON_VEG" that come
// from hand-edited backup JSON or bulk CSVs (those bypass the CSV importer's
// own normalization entirely), silently mis-filing items as veg.
// Missing/unset still defaults to veg, since most default catalog items are.
function isVeg(item) {
  const raw = String(item.diet ?? '').trim().toLowerCase().replace(/[\s_]+/g, '-')
  return raw !== 'non-veg' && raw !== 'nonveg' && raw !== 'non-vegetarian' && raw !== 'nonvegetarian'
}

// The familiar FSSAI-style square-with-a-dot mark, colored green/red.
function dietMarkHtml(item) {
  const veg = isVeg(item)
  return `<span class="diet-mark ${veg ? 'veg' : 'nonveg'}" title="${veg ? 'Vegetarian' : 'Non-Vegetarian'}"></span>`
}

// ── Bulk import/export via CSV ──
function parseCsvLine(line) {
  const result = []
  let cur = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++ }
        else inQuotes = false
      } else cur += ch
    } else if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      result.push(cur); cur = ''
    } else {
      cur += ch
    }
  }
  result.push(cur)
  return result.map(s => s.trim())
}

const CSV_COLUMNS = ['name', 'price', 'category', 'desc', 'image', 'discountpercent', 'discountactive', 'eventlabel', 'stock', 'available', 'diet']

// Parses a CSV file's text into menu items, skipping rows that are missing
// the required name/price/category fields (collected as human-readable errors
// rather than silently dropped, so a bulk upload can report what happened).
function csvToMenuItems(text) {
  const lines = text.split(/\r?\n/).filter(l => l.trim().length)
  if (lines.length < 2) return { items: [], errors: ['No data rows found in the file.'] }

  const header = parseCsvLine(lines[0]).map(h => h.toLowerCase())
  const col = name => header.indexOf(name)
  const iName = col('name'), iPrice = col('price'), iCategory = col('category')
  const iDesc = col('desc'), iImage = col('image')
  const iDiscPct = col('discountpercent'), iDiscActive = col('discountactive'), iEvent = col('eventlabel')
  const iStock = col('stock'), iAvailable = col('available'), iDiet = col('diet')

  if (iName === -1 || iPrice === -1 || iCategory === -1) {
    return { items: [], errors: ['CSV must include at least "name", "price" and "category" columns.'] }
  }

  const items = []
  const errors = []
  for (let r = 1; r < lines.length; r++) {
    const cols = parseCsvLine(lines[r])
    const name = (cols[iName] || '').trim()
    const price = parseFloat(cols[iPrice])
    const category = (cols[iCategory] || '').trim()
    if (!name || !category || isNaN(price) || price < 0) {
      errors.push(`Row ${r + 1}: missing or invalid name/price/category — skipped.`)
      continue
    }
    const stockRaw = iStock !== -1 ? (cols[iStock] || '').trim() : ''
    items.push({
      id: 'm' + Date.now() + '-' + r,
      name, price, category,
      desc: iDesc !== -1 ? (cols[iDesc] || '') : '',
      image: iImage !== -1 ? (cols[iImage] || '') : '',
      discountPercent: iDiscPct !== -1 ? (parseFloat(cols[iDiscPct]) || 0) : 0,
      discountActive: iDiscActive !== -1 ? /^(1|true|yes)$/i.test(cols[iDiscActive] || '') : false,
      eventLabel: iEvent !== -1 ? (cols[iEvent] || '') : '',
      ...(stockRaw !== '' && !isNaN(parseFloat(stockRaw)) ? { stock: Math.max(0, Math.floor(parseFloat(stockRaw))) } : {}),
      available: iAvailable !== -1 ? /^(1|true|yes)$/i.test(cols[iAvailable] || 'true') : true,
      diet: iDiet !== -1 && /nonveg|non-veg/i.test(cols[iDiet] || '') ? 'nonveg' : 'veg',
    })
  }
  return { items, errors }
}

function menuTemplateCsv() {
  return [
    CSV_COLUMNS.join(','),
    'Classic Chicken Burger,5.47,Burger,"Grilled chicken, lettuce and house sauce",,20,true,Weekend Special,25,true,nonveg',
    'Orange Juice,1.20,Juice,Freshly squeezed,,0,false,,,true,veg',
  ].join('\n')
}

function downloadTextFile(filename, text, mime = 'text/csv') {
  const blob = new Blob([text], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
