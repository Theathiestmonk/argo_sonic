// Small formatters shared by the dashboard cards. No data is made up here – purely presentation.
const SYMBOL = { INR: '₹', USD: '$', EUR: '€', GBP: '£', AED: 'AED ' }

export function money(n, code = 'INR') {
  const sym = SYMBOL[code] ?? `${code} `
  const v = Number(n || 0)
  const txt = new Intl.NumberFormat(code === 'INR' ? 'en-IN' : 'en-US', { minimumFractionDigits: v % 1 ? 2 : 0, maximumFractionDigits: 2 }).format(v)
  return `${sym}${txt}`
}

export function ageText(iso, now = Date.now()) {
  if (!iso) return '—'
  const mins = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000))
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins} min`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

export function hoursText(h) {
  if (h == null || !Number.isFinite(h) || h <= 0) return '—'
  const m = Math.round(h * 60)
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

export const fmtDist = (v) => (v == null ? '—' : !Number.isFinite(v) ? 'Clear' : `${v.toFixed(2)} m`)
export const distClass = (v) => (v == null ? 'dim' : !Number.isFinite(v) ? 'ok' : v < 0.4 ? 'bad' : v < 0.8 ? 'warn' : 'ok')

// "Open every day from 10 AM to 11 PM" → open/closed right now, or null when the text isn't parseable.
export function openNow(hours, now = new Date()) {
  const m = String(hours || '').match(/(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*(?:to|-|–)\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)/i)
  if (!m) return null
  const t = (h, mi, ap) => ((+h % 12) + (/pm/i.test(ap) ? 12 : 0)) * 60 + (+mi || 0)
  const a = t(m[1], m[2], m[3]), b = t(m[4], m[5], m[6]), cur = now.getHours() * 60 + now.getMinutes()
  return a <= b ? cur >= a && cur < b : cur >= a || cur < b
}
