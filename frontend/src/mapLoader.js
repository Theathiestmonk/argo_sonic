// Fallback map source for when the live /map topic hasn't (re)delivered yet.
// slam_toolbox publishes /map latched (transient_local) and then only on
// map updates; a browser reload subscribes fresh through rosbridge and may
// never receive that one message, leaving the map blank. The saved
// .pgm/.yaml the launcher already serves is the same grid, so rebuild an
// OccupancyGrid-shaped object from it.

function parsePGM(buffer) {
  const bytes = new Uint8Array(buffer)
  let pos = 0
  const readToken = () => {
    while (pos < bytes.length) {
      const c = bytes[pos]
      if (c === 0x23) { while (pos < bytes.length && bytes[pos] !== 0x0a) pos++ }
      else if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) pos++
      else break
    }
    const start = pos
    while (pos < bytes.length && ![0x20, 0x09, 0x0a, 0x0d].includes(bytes[pos])) pos++
    return String.fromCharCode(...bytes.subarray(start, pos))
  }
  if (readToken() !== 'P5') return null
  const width = parseInt(readToken(), 10)
  const height = parseInt(readToken(), 10)
  parseInt(readToken(), 10) // maxval
  pos += 1
  if (!width || !height) return null
  return { width, height, pixels: bytes.subarray(pos, pos + width * height) }
}

// Resolves to the same shape App.jsx's /map subscriber produces, or null.
export async function loadSavedMap(launcherUrl, mapName) {
  const [metaRes, pgmRes] = await Promise.all([
    fetch(`${launcherUrl}/maps/${mapName}/meta`),
    fetch(`${launcherUrl}/maps/${mapName}/preview`),
  ])
  if (!metaRes.ok || !pgmRes.ok) return null
  const meta = await metaRes.json()
  const pgm = parsePGM(await pgmRes.arrayBuffer())
  if (!pgm) return null

  // PGM rows run top-down, OccupancyGrid rows bottom-up → flip vertically.
  // map_saver trinary encoding: 0 = occupied, 254 = free, 205 = unknown.
  const { width, height, pixels } = pgm
  const data = new Array(width * height)
  for (let y = 0; y < height; y++) {
    const src = (height - 1 - y) * width
    for (let x = 0; x < width; x++) {
      const v = pixels[src + x]
      data[y * width + x] = v <= 80 ? 100 : v >= 240 ? 0 : -1
    }
  }
  return {
    width, height,
    resolution: meta.resolution,
    origin: { x: meta.origin[0], y: meta.origin[1] },
    data,
  }
}
