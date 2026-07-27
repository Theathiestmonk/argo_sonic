import { useEffect, useRef, useState } from 'react'

// Minimal PGM (P5, binary greyscale) parser — map_saver's output has no
// comment lines in practice, but the format allows them, so we skip any.
function parsePGM(buffer) {
  const bytes = new Uint8Array(buffer)
  let pos = 0

  const readToken = () => {
    while (pos < bytes.length) {
      const c = bytes[pos]
      if (c === 0x23) { // '#' comment → skip to end of line
        while (pos < bytes.length && bytes[pos] !== 0x0a) pos++
      } else if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) {
        pos++
      } else break
    }
    const start = pos
    while (pos < bytes.length && ![0x20, 0x09, 0x0a, 0x0d].includes(bytes[pos])) pos++
    return String.fromCharCode(...bytes.subarray(start, pos))
  }

  if (readToken() !== 'P5') return null
  const width  = parseInt(readToken(), 10)
  const height = parseInt(readToken(), 10)
  parseInt(readToken(), 10) // maxval — unused, values are already 0-255
  pos += 1 // single whitespace byte separating header from binary data
  if (!width || !height) return null
  return { width, height, pixels: bytes.subarray(pos, pos + width * height) }
}

export default function MapPreviewThumb({ launcherUrl, mapName, width = 200, height = 140 }) {
  const canvasRef = useRef(null)
  const [status, setStatus] = useState('loading') // 'loading' | 'ok' | 'error'

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    fetch(`${launcherUrl}/maps/${mapName}/preview`)
      .then(r => { if (!r.ok) throw new Error('not found'); return r.arrayBuffer() })
      .then(buf => {
        if (cancelled) return
        const pgm = parsePGM(buf)
        const canvas = canvasRef.current
        if (!pgm || !canvas) { setStatus('error'); return }
        canvas.width = pgm.width
        canvas.height = pgm.height
        const ctx = canvas.getContext('2d')
        const imgData = ctx.createImageData(pgm.width, pgm.height)
        for (let i = 0; i < pgm.width * pgm.height; i++) {
          const v = pgm.pixels[i]
          imgData.data[i * 4]     = v
          imgData.data[i * 4 + 1] = v
          imgData.data[i * 4 + 2] = v
          imgData.data[i * 4 + 3] = 255
        }
        ctx.putImageData(imgData, 0, 0)
        setStatus('ok')
      })
      .catch(() => { if (!cancelled) setStatus('error') })
    return () => { cancelled = true }
  }, [launcherUrl, mapName])

  return (
    <div style={{
      width, height, borderRadius: 10, overflow: 'hidden',
      background: '#0c0a12', display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: '100%', objectFit: 'contain', display: status === 'ok' ? 'block' : 'none' }}
      />
      {status === 'loading' && <span style={{ fontSize: 11, color: 'var(--muted)' }}>Loading…</span>}
      {status === 'error' && <span style={{ fontSize: 11, color: 'var(--muted)' }}>No preview</span>}
    </div>
  )
}
