import { useEffect, useRef, useState, useCallback } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// Renders the GLB that map_to_3d.py builds from a saved map (served by the launcher at
// /maps/<name>/model.glb). The GLB is glTF Y-up but otherwise in the ROS map frame, so map
// (x, y, theta) is three.js (x, 0, -y) with rotation.y = theta — no other transform anywhere.
const toThree = (x, y) => new THREE.Vector3(x, 0, -y)

// Robot footprint from nav2.yaml's local_costmap: 0.53 x 0.45 m.
const ROBOT_LEN = 0.53
const ROBOT_WID = 0.45

const btn = (active, tone = 'gold') => ({
  padding: '5px 10px', borderRadius: 6, fontSize: 11, fontWeight: 700, cursor: 'pointer',
  color: '#fff', transition: 'all 0.15s',
  background: active ? (tone === 'gold' ? 'rgba(226,179,92,0.28)' : 'rgba(127,168,232,0.28)') : 'rgba(255,255,255,0.06)',
  border: `1px solid ${active ? (tone === 'gold' ? 'rgba(226,179,92,0.6)' : 'rgba(127,168,232,0.6)') : 'rgba(255,255,255,0.14)'}`,
})

function labelSprite(text) {
  const canvas = document.createElement('canvas')
  canvas.width = 256; canvas.height = 64
  const ctx = canvas.getContext('2d')
  ctx.font = 'bold 30px Inter, sans-serif'
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
  ctx.fillStyle = 'rgba(12,9,18,0.72)'
  const w = Math.min(248, ctx.measureText(text).width + 28)
  ctx.beginPath(); ctx.roundRect ? ctx.roundRect((256 - w) / 2, 8, w, 48, 12) : ctx.rect((256 - w) / 2, 8, w, 48)
  ctx.fill()
  ctx.fillStyle = '#ffd76a'
  ctx.fillText(text, 128, 33)
  const tex = new THREE.CanvasTexture(canvas)
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }))
  sprite.scale.set(1.0, 0.25, 1)
  sprite.renderOrder = 10
  return sprite
}

function disposeObject(obj) {
  obj.traverse(o => {
    o.geometry?.dispose?.()
    const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : []
    mats.forEach(m => { m.map?.dispose?.(); m.dispose?.() })
  })
}

export default function Map3DViewer({ launcherUrl, mapName, robotPose, goalPose, plannedPath = [], markers = [], showToast }) {
  const hostRef = useRef(null)
  const three = useRef({})             // renderer/scene/camera/controls/groups, created once
  const [info, setInfo] = useState(null)          // GET /maps/<name>/model
  const [netErr, setNetErr] = useState(false)
  const [oldLauncher, setOldLauncher] = useState(false)   // launcher answers, but predates the 3D endpoints
  const [loadState, setLoadState] = useState('idle') // idle | loading | ready | error
  const [view, setView] = useState('orbit')          // orbit | top | follow
  const [cutaway, setCutaway] = useState(false)
  const [starting, setStarting] = useState(false)
  const viewRef = useRef(view)
  viewRef.current = view

  // ── Model status from the launcher ──────────────────────────────────────────
  const refreshInfo = useCallback(async () => {
    if (!mapName) return
    try {
      const r = await fetch(`${launcherUrl}/maps/${encodeURIComponent(mapName)}/model`)
      if (r.status === 404) { setOldLauncher(true); setNetErr(false); return }
      if (!r.ok) throw new Error(String(r.status))
      setOldLauncher(false)
      setInfo(await r.json())
      setNetErr(false)
    } catch {
      setNetErr(true)
    }
  }, [launcherUrl, mapName])

  useEffect(() => { setInfo(null); setLoadState('idle'); refreshInfo() }, [refreshInfo])
  useEffect(() => {
    if (!info?.building) return
    const id = setInterval(refreshInfo, 2000)
    return () => clearInterval(id)
  }, [info?.building, refreshInfo])

  const build = async () => {
    setStarting(true)
    try {
      const r = await fetch(`${launcherUrl}/maps/${encodeURIComponent(mapName)}/model/build`, { method: 'POST' })
      if (r.status === 503) showToast?.('3D converter is not set up on the robot — see setup note below', 'danger')
      else if (r.status === 409) showToast?.('A 3D model is already being built', 'info')
      else if (!r.ok) showToast?.('Could not start the 3D build', 'danger')
    } catch {
      showToast?.('Could not reach launcher', 'danger')
    }
    setStarting(false)
    refreshInfo()
  }

  // ── Scene (once) ────────────────────────────────────────────────────────────
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const T = three.current

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.outputEncoding = THREE.sRGBEncoding
    renderer.setClearColor(0x000000, 0)
    host.appendChild(renderer.domElement)
    renderer.domElement.style.display = 'block'

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 500)
    camera.position.set(8, 9, 10)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.12
    controls.maxPolarAngle = Math.PI / 2 - 0.02   // never go below the floor

    scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2a35, 0.95))
    const sun = new THREE.DirectionalLight(0xffffff, 0.75)
    sun.position.set(6, 12, 4)
    scene.add(sun)

    const world = new THREE.Group()      // GLB
    const ground = new THREE.Group()     // floor + grid, rebuilt per model
    const robot = new THREE.Group()
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(ROBOT_LEN, 0.28, ROBOT_WID),
      new THREE.MeshStandardMaterial({ color: 0x800000, roughness: 0.6 }),
    )
    body.position.y = 0.17
    const nose = new THREE.Mesh(
      new THREE.ConeGeometry(0.1, 0.26, 20),
      new THREE.MeshStandardMaterial({ color: 0xf2f2f2, roughness: 0.5 }),
    )
    nose.rotation.z = -Math.PI / 2                // cone points +Y by default; forward is +X
    nose.position.set(ROBOT_LEN / 2 + 0.05, 0.36, 0)
    const halo = new THREE.Mesh(
      new THREE.RingGeometry(0.38, 0.44, 40),
      new THREE.MeshBasicMaterial({ color: 0xe2b35c, transparent: true, opacity: 0.85, side: THREE.DoubleSide }),
    )
    halo.rotation.x = -Math.PI / 2
    halo.position.y = 0.02
    robot.add(body, nose, halo)
    robot.visible = false

    const goal = new THREE.Group()
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.26, 0.035, 10, 40),
      new THREE.MeshBasicMaterial({ color: 0x7fa8e8 }),
    )
    ring.rotation.x = Math.PI / 2
    ring.position.y = 0.03
    const beam = new THREE.Mesh(
      new THREE.CylinderGeometry(0.03, 0.03, 1.4, 10),
      new THREE.MeshBasicMaterial({ color: 0x7fa8e8, transparent: true, opacity: 0.55 }),
    )
    beam.position.y = 0.7
    goal.add(ring, beam)
    goal.visible = false

    const pathGroup = new THREE.Group()
    const markerGroup = new THREE.Group()
    scene.add(ground, world, pathGroup, markerGroup, robot, goal)

    let dirty = true
    let raf = 0
    const requestRender = () => { dirty = true }
    controls.addEventListener('change', requestRender)
    const tick = () => {
      raf = requestAnimationFrame(tick)
      if (controls.update()) dirty = true
      if (dirty) { renderer.render(scene, camera); dirty = false }
    }
    tick()

    const resize = () => {
      const w = host.clientWidth, h = host.clientHeight
      if (!w || !h) return
      renderer.setSize(w, h, false)
      renderer.domElement.style.width = '100%'
      renderer.domElement.style.height = '100%'
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      requestRender()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(host)
    resize()

    Object.assign(T, { renderer, scene, camera, controls, world, ground, robot, goal, pathGroup, markerGroup,
                       requestRender, bounds: null, lastRobot: null, fittedFor: null })

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      ;[world, ground, pathGroup, markerGroup, robot, goal].forEach(disposeObject)
      renderer.dispose()
      if (host.contains(renderer.domElement)) host.removeChild(renderer.domElement)
      for (const k of Object.keys(T)) delete T[k]
    }
  }, [])

  // ── Camera presets ──────────────────────────────────────────────────────────
  const applyView = useCallback((mode) => {
    const T = three.current
    if (!T.camera) return
    const b = T.bounds
    const c = b ? b.getCenter(new THREE.Vector3()) : new THREE.Vector3()
    const size = b ? b.getSize(new THREE.Vector3()) : new THREE.Vector3(10, 2.5, 10)
    const r = Math.max(size.x, size.z, 4)
    if (mode === 'top') {
      // Slightly south of vertical so screen-up is map +y, matching the 2D map.
      T.camera.position.set(c.x, r * 1.25, c.z + r * 0.02)
      T.controls.target.set(c.x, 0, c.z)
    } else if (mode === 'follow' && T.lastRobot) {
      const { p, theta } = T.lastRobot
      const f = new THREE.Vector3(Math.cos(theta), 0, -Math.sin(theta))
      T.camera.position.copy(p).addScaledVector(f, -2.4).add(new THREE.Vector3(0, 1.7, 0))
      T.controls.target.copy(p).addScaledVector(f, 0.6).add(new THREE.Vector3(0, 0.3, 0))
    } else {
      T.camera.position.set(c.x + r * 0.55, r * 0.85, c.z + r * 0.85)
      T.controls.target.set(c.x, 0.6, c.z)
    }
    T.controls.update()
    T.requestRender()
  }, [])

  useEffect(() => { applyView(view) }, [view, applyView])

  // ── Load / reload the GLB ───────────────────────────────────────────────────
  useEffect(() => {
    const T = three.current
    if (!T.world || !info?.exists) return
    let cancelled = false
    setLoadState('loading')
    new GLTFLoader().load(
      `${launcherUrl}/maps/${encodeURIComponent(mapName)}/model.glb?v=${info.mtime}`,
      (gltf) => {
        if (cancelled || !three.current.world) return
        const t = three.current
        while (t.world.children.length) { const c = t.world.children[0]; t.world.remove(c); disposeObject(c) }
        gltf.scene.traverse(o => {
          if (o.isMesh && o.material) {
            o.material.metalness = 0
            o.material.roughness = 0.92
            o.material.side = THREE.DoubleSide
            o.material.needsUpdate = true
          }
        })
        t.world.add(gltf.scene)

        const box = new THREE.Box3().setFromObject(gltf.scene)
        t.bounds = box
        const c = box.getCenter(new THREE.Vector3())
        const s = box.getSize(new THREE.Vector3())

        while (t.ground.children.length) { const g = t.ground.children[0]; t.ground.remove(g); disposeObject(g) }
        const span = Math.ceil(Math.max(s.x, s.z) + 4)
        const floor = new THREE.Mesh(
          new THREE.PlaneGeometry(span, span),
          new THREE.MeshBasicMaterial({ color: 0x15141c, transparent: true, opacity: 0.9 }),
        )
        floor.rotation.x = -Math.PI / 2
        floor.position.set(c.x, -0.005, c.z)
        const grid = new THREE.GridHelper(span, span, 0x6b5a30, 0x2b2a35)   // 1 m cells
        grid.position.set(c.x, 0, c.z)
        t.ground.add(floor, grid)

        if (t.fittedFor !== mapName) { t.fittedFor = mapName; applyView(viewRef.current) }
        setLoadState('ready')
        t.requestRender()
      },
      undefined,
      () => { if (!cancelled) setLoadState('error') },
    )
    return () => { cancelled = true }
  }, [info?.exists, info?.mtime, launcherUrl, mapName, applyView])

  // ── Cutaway (see-through walls) ─────────────────────────────────────────────
  useEffect(() => {
    const T = three.current
    if (!T.world) return
    T.world.traverse(o => {
      if (o.isMesh && o.material) {
        o.material.transparent = cutaway
        o.material.opacity = cutaway ? 0.3 : 1
        o.material.depthWrite = !cutaway
        o.material.needsUpdate = true
      }
    })
    T.requestRender()
  }, [cutaway, loadState])

  // ── Robot ───────────────────────────────────────────────────────────────────
  useEffect(() => {
    const T = three.current
    if (!T.robot) return
    if (!robotPose) { T.robot.visible = false; T.lastRobot = null; T.requestRender(); return }
    const p = toThree(robotPose.x, robotPose.y)
    if (viewRef.current === 'follow' && T.lastRobot) {
      const d = p.clone().sub(T.lastRobot.p)
      T.camera.position.add(d)
      T.controls.target.add(d)
    }
    T.robot.position.copy(p)
    T.robot.rotation.y = robotPose.theta || 0
    T.robot.visible = true
    const first = !T.lastRobot
    T.lastRobot = { p, theta: robotPose.theta || 0 }
    if (first && viewRef.current === 'follow') applyView('follow')
    T.requestRender()
  }, [robotPose, applyView])

  // ── Goal ────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const T = three.current
    if (!T.goal) return
    T.goal.visible = !!goalPose
    if (goalPose) T.goal.position.copy(toThree(goalPose.x, goalPose.y))
    T.requestRender()
  }, [goalPose])

  // ── Planned path ────────────────────────────────────────────────────────────
  useEffect(() => {
    const T = three.current
    if (!T.pathGroup) return
    while (T.pathGroup.children.length) { const c = T.pathGroup.children[0]; T.pathGroup.remove(c); disposeObject(c) }
    if (plannedPath.length > 1) {
      const pts = plannedPath.map(p => new THREE.Vector3(p.x, 0.06, -p.y))
      T.pathGroup.add(new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color: 0xe2b35c }),
      ))
    }
    T.requestRender()
  }, [plannedPath])

  // ── Table / place markers ───────────────────────────────────────────────────
  const markerKey = JSON.stringify(markers.map(m => [m.key, m.name, m.x, m.y]))
  useEffect(() => {
    const T = three.current
    if (!T.markerGroup) return
    while (T.markerGroup.children.length) { const c = T.markerGroup.children[0]; T.markerGroup.remove(c); disposeObject(c) }
    markers.forEach(m => {
      if (typeof m.x !== 'number' || typeof m.y !== 'number') return
      const post = new THREE.Mesh(
        new THREE.CylinderGeometry(0.11, 0.11, 0.9, 20),
        new THREE.MeshStandardMaterial({ color: 0xffd76a, roughness: 0.5, transparent: true, opacity: 0.85 }),
      )
      post.position.copy(toThree(m.x, m.y)).setY(0.45)
      const label = labelSprite(String(m.name ?? m.key))
      label.position.copy(toThree(m.x, m.y)).setY(1.25)
      T.markerGroup.add(post, label)
    })
    T.requestRender()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markerKey, loadState])

  // ── Overlays ────────────────────────────────────────────────────────────────
  const overlay = (children) => (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, pointerEvents: 'none' }}>
      <div style={{ pointerEvents: 'auto', maxWidth: 340, textAlign: 'center', padding: '18px 20px', borderRadius: 14,
                    background: 'rgba(12,9,18,0.82)', border: '1px solid rgba(255,255,255,0.1)', backdropFilter: 'blur(12px)' }}>
        {children}
      </div>
    </div>
  )
  const spinner = (
    <span style={{ display: 'inline-block', width: 14, height: 14, borderRadius: '50%', border: '2px solid var(--gold)',
                   borderTopColor: 'transparent', animation: 'spin-slow 0.9s linear infinite', marginRight: 10, verticalAlign: 'middle' }} />
  )
  const primary = {
    marginTop: 12, padding: '9px 16px', borderRadius: 8, fontSize: 12, fontWeight: 700, cursor: 'pointer', color: '#fff',
    background: 'rgba(226,179,92,0.22)', border: '1px solid rgba(226,179,92,0.55)',
  }

  let panel = null
  if (!mapName) {
    panel = overlay(<div style={{ fontSize: 13 }}>Select a map to see it in 3D.</div>)
  } else if (oldLauncher) {
    panel = overlay(<div style={{ fontSize: 12.5, lineHeight: 1.6 }}>The launcher is running an older version that can’t serve 3D models yet.
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>Restart it on the robot:</div>
      <pre style={{ margin: '6px 0 0', padding: 8, borderRadius: 6, background: 'rgba(255,255,255,0.06)', fontSize: 11, color: '#e6e6e6' }}>sudo systemctl restart argo-launcher</pre>
      <button style={primary} onClick={refreshInfo}>Check again</button></div>)
  } else if (netErr && !info) {
    panel = overlay(<div style={{ fontSize: 13, color: 'var(--danger)' }}>Can't reach the launcher — 3D models are served from there.
      <div><button style={primary} onClick={refreshInfo}>Retry</button></div></div>)
  } else if (!info) {
    panel = overlay(<div style={{ fontSize: 13 }}>{spinner}Checking for a 3D model…</div>)
  } else if (info.building && !info.exists) {
    panel = overlay(<div style={{ fontSize: 13 }}>{spinner}Building the 3D model for <b>{mapName}</b>…
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>Usually a few seconds.</div></div>)
  } else if (!info.exists) {
    panel = overlay(
      <>
        <div style={{ fontSize: 13, fontWeight: 700 }}>No 3D model for “{mapName}” yet</div>
        {info.phase === 'failed' && <div style={{ fontSize: 12, color: 'var(--danger)', marginTop: 6 }}>The last build failed — check the launcher log.</div>}
        {info.available ? (
          <>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 6 }}>Extrudes the saved map’s walls into a 3D building.</div>
            <button style={primary} onClick={build} disabled={starting}>{starting ? 'Starting…' : 'Generate 3D model'}</button>
          </>
        ) : (
          <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 8, lineHeight: 1.6, textAlign: 'left' }}>
            The converter isn’t set up on the robot. Once, in the repo folder:
            <pre style={{ margin: '8px 0 0', padding: 8, borderRadius: 6, background: 'rgba(255,255,255,0.06)', fontSize: 10.5, whiteSpace: 'pre-wrap', color: '#e6e6e6' }}>
{`python3 -m venv ~/.venvs/map3d
~/.venvs/map3d/bin/pip install -r requirements-map3d.txt`}
            </pre>
            then restart the launcher.
          </div>
        )}
      </>,
    )
  } else if (loadState === 'loading') {
    panel = overlay(<div style={{ fontSize: 13 }}>{spinner}Loading model…</div>)
  } else if (loadState === 'error') {
    panel = overlay(<div style={{ fontSize: 13, color: 'var(--danger)' }}>The 3D model file couldn’t be loaded.
      <div><button style={primary} onClick={build}>Rebuild</button></div></div>)
  }

  const ready = info?.exists && loadState === 'ready'
  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div ref={hostRef} style={{ position: 'absolute', inset: 0 }} />

      {ready && (
        <div style={{ position: 'absolute', top: 10, left: 10, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {[['orbit', 'Orbit'], ['top', 'Top'], ['follow', 'Follow']].map(([id, label]) => (
            <button key={id} style={btn(view === id)} onClick={() => setView(id)}
                    disabled={id === 'follow' && !robotPose} title={id === 'follow' && !robotPose ? 'Waiting for the robot’s position' : undefined}>
              {label}
            </button>
          ))}
          <button style={btn(cutaway, 'blue')} onClick={() => setCutaway(v => !v)} title="See through the walls">Cutaway</button>
        </div>
      )}

      {ready && info.building && (
        <div style={{ position: 'absolute', bottom: 10, left: 10, padding: '6px 10px', borderRadius: 8, fontSize: 11.5,
                      background: 'rgba(12,9,18,0.82)', border: '1px solid rgba(226,179,92,0.4)', color: 'var(--gold-bright)' }}>
          {spinner}Rebuilding the 3D model…
        </div>
      )}

      {ready && info.stale && !info.building && (
        <div style={{ position: 'absolute', bottom: 10, left: 10, display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderRadius: 8,
                      background: 'rgba(12,9,18,0.82)', border: '1px solid rgba(226,179,92,0.4)', fontSize: 11.5, color: 'var(--gold-bright)' }}>
          The map changed since this model was built
          {info.available && <button style={{ ...btn(false), padding: '3px 8px' }} onClick={build} disabled={starting}>Rebuild</button>}
        </div>
      )}

      {panel}
    </div>
  )
}
