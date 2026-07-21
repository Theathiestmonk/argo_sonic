// Shared cyclic radial nav, used by dashboard.html, order.html and menu.html
// so navigation between pages looks and behaves identically everywhere.
// Call ArgoRadialNav.init(pages, activePageId) once the nav markup
// (#radialBackdrop, #radialNav, #radialTrack, #radialItems, #radialFabBtn) is in the DOM.
//
// Each page in `pages` is either:
//   { id, label, icon, href }   -> clicking navigates to another page
//   { id, label, icon, target } -> clicking scrolls to an in-page selector (dashboard.html only)
const ArgoRadialNav = (() => {
  const RADIAL_MAX_VISIBLE = 4     // window size — only this many icons render at once, cycling like a dial.
  const RADIAL_MOBILE_BREAKPOINT = 700

  function init(pages, activePageId) {
    let radialActivePage = activePageId
    let radialFocusIndex = Math.max(0, pages.findIndex(p => p.id === activePageId))
    let radialOpen = false

    function radialIsMobile() {
      return window.innerWidth <= RADIAL_MOBILE_BREAKPOINT
    }

    // Desktop: a quarter-arc tucked close to the FAB, which sits in the corner.
    // Mobile: the FAB moves to bottom-center, so the arc opens into a full semicircle.
    function radialArcRange() {
      return radialIsMobile() ? { start: 0, end: 180 } : { start: 6, end: 90 }
    }

    function radialFabSizePx() {
      return Math.max(56, Math.min(74, window.innerWidth * 0.07))
    }

    function computeRadialGeometry(m, arc) {
      const fabR = radialFabSizePx() / 2
      const iconSize = Math.max(52, Math.min(74, window.innerWidth * 0.065))
      const gap = 22
      const attachFloor = fabR + 26 + iconSize / 2

      if (m < 2) return { radius: attachFloor, iconSize }

      const halfStepRad = (arc.end - arc.start) / (m - 1) / 2 * Math.PI / 180
      const minRadius = (iconSize + gap) / (2 * Math.sin(halfStepRad))
      return { radius: Math.max(attachFloor, minRadius), iconSize }
    }

    function radialAngleFor(i, n, arc) {
      if (n === 1) return arc.start
      return arc.start + (arc.end - arc.start) * (i / (n - 1))
    }

    // Cyclic window of page indices around radialFocusIndex, wrapping like a dial.
    function radialVisibleIndices() {
      const n = pages.length
      if (n <= RADIAL_MAX_VISIBLE) return pages.map((_, i) => i)
      const half = Math.floor(RADIAL_MAX_VISIBLE / 2)
      const idx = []
      for (let k = -half; k < RADIAL_MAX_VISIBLE - half; k++) {
        idx.push(((radialFocusIndex + k) % n + n) % n)
      }
      return idx
    }

    function buildRadialNav(stagger) {
      const itemsWrap = document.getElementById('radialItems')
      itemsWrap.innerHTML = ''
      const indices = radialVisibleIndices()
      const m = indices.length
      const arc = radialArcRange()
      const { radius, iconSize } = computeRadialGeometry(m, arc)
      document.getElementById('radialNav').style.setProperty('--icon-size', iconSize.toFixed(1) + 'px')
      const points = []

      indices.forEach((pageIdx, pos) => {
        const p = pages[pageIdx]
        const deg = radialAngleFor(pos, m, arc)
        const rad = deg * Math.PI / 180
        const dx = radius * Math.cos(rad)
        const dy = -radius * Math.sin(rad)
        points.push({ dx, dy })

        const btn = document.createElement('button')
        btn.className = 'radial-item'
          + (p.id === radialActivePage ? ' active' : '')
          + (pageIdx === radialFocusIndex ? ' focused' : '')
        btn.style.setProperty('--dx', dx.toFixed(1) + 'px')
        btn.style.setProperty('--dy', dy.toFixed(1) + 'px')
        btn.style.setProperty('--delay', stagger ? (pos * 40) + 'ms' : '0ms')
        btn.innerHTML = `<span class="radial-glow"></span><span class="radial-icon">${p.icon}</span><span class="radial-tip">${p.label}</span>`
        btn.onclick = () => { radialFocusIndex = pageIdx; goToRadialPage(p) }
        itemsWrap.appendChild(btn)
      })

      const track = document.getElementById('radialTrack')
      if (points.length > 1) {
        const pad = 40
        const minX = Math.min(0, ...points.map(p => p.dx)) - pad
        const maxX = Math.max(0, ...points.map(p => p.dx)) + pad
        const minY = Math.min(0, ...points.map(p => p.dy)) - pad
        const maxY = Math.max(0, ...points.map(p => p.dy)) + pad
        const w = maxX - minX, h = maxY - minY
        track.setAttribute('viewBox', `${minX} ${minY} ${w} ${h}`)
        track.setAttribute('width', w)
        track.setAttribute('height', h)
        track.style.left = minX + 'px'
        track.style.bottom = (-maxY) + 'px'
        const d = points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt.dx.toFixed(1)} ${pt.dy.toFixed(1)}`).join(' ')
        track.innerHTML = `<path d="${d}"/>`
      }
    }

    function goToRadialPage(p) {
      radialActivePage = p.id
      closeRadial()
      if (p.href) {
        // Avoid a pointless full reload when the user clicks the page they're already on.
        if (p.href === window.location.pathname) return
        window.location.href = p.href
        return
      }
      document.querySelector(p.target)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }

    function moveRadialFocus(step) {
      const n = pages.length
      radialFocusIndex = ((radialFocusIndex + step) % n + n) % n
      buildRadialNav(false)
    }

    function confirmRadialFocus() {
      goToRadialPage(pages[radialFocusIndex])
    }

    function openRadial() {
      radialOpen = true
      radialFocusIndex = Math.max(0, pages.findIndex(p => p.id === radialActivePage))
      buildRadialNav(true)
      document.getElementById('radialNav').classList.add('open')
      document.getElementById('radialBackdrop').classList.add('open')
      document.getElementById('radialFabBtn').setAttribute('aria-expanded', 'true')
    }
    function closeRadial() {
      radialOpen = false
      document.getElementById('radialNav').classList.remove('open')
      document.getElementById('radialBackdrop').classList.remove('open')
      document.getElementById('radialFabBtn').setAttribute('aria-expanded', 'false')
    }

    document.getElementById('radialFabBtn').onclick = () => { radialOpen ? closeRadial() : openRadial() }
    document.addEventListener('click', e => {
      if (radialOpen && !document.getElementById('radialNav').contains(e.target)) closeRadial()
    })
    document.addEventListener('keydown', e => {
      if (!radialOpen) return
      if (e.key === 'ArrowRight') { e.preventDefault(); moveRadialFocus(-1) }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); moveRadialFocus(1) }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); confirmRadialFocus() }
      else if (e.key === 'Escape') { closeRadial() }
    })
    let radialResizeTimer
    window.addEventListener('resize', () => {
      clearTimeout(radialResizeTimer)
      radialResizeTimer = setTimeout(() => buildRadialNav(false), 150)
    })

    // Swipe-to-rotate on touch devices.
    const radialBackdropEl = document.getElementById('radialBackdrop')
    let radialTouchActive = false
    let radialTouchStartX = 0, radialTouchStartY = 0
    const RADIAL_SWIPE_THRESHOLD = 32
    const RADIAL_SWIPE_MOVE_GUARD = 6

    radialBackdropEl.addEventListener('touchstart', e => {
      if (!radialOpen || e.touches.length !== 1) return
      radialTouchActive = true
      radialTouchStartX = e.touches[0].clientX
      radialTouchStartY = e.touches[0].clientY
    }, { passive: true })

    radialBackdropEl.addEventListener('touchmove', e => {
      if (!radialTouchActive) return
      const t = e.touches[0]
      const dx = t.clientX - radialTouchStartX
      const dy = t.clientY - radialTouchStartY
      if (Math.abs(dx) > RADIAL_SWIPE_MOVE_GUARD || Math.abs(dy) > RADIAL_SWIPE_MOVE_GUARD) e.preventDefault()
    }, { passive: false })

    radialBackdropEl.addEventListener('touchend', e => {
      if (!radialTouchActive) return
      radialTouchActive = false
      const t = e.changedTouches[0]
      const dx = t.clientX - radialTouchStartX
      const dy = t.clientY - radialTouchStartY
      if (Math.abs(dx) < RADIAL_SWIPE_THRESHOLD && Math.abs(dy) < RADIAL_SWIPE_THRESHOLD) return
      const primary = Math.abs(dx) > Math.abs(dy) ? dx : dy
      if (primary < 0) moveRadialFocus(1)
      else moveRadialFocus(-1)
    })

    buildRadialNav(false)
  }

  // The standard 6-page list, shared by order.html and menu.html so their
  // icons/labels/hrefs can't drift apart. dashboard.html keeps its own copy
  // since Overview/Places/Activity there scroll to in-page sections instead
  // of navigating.
  function pages() {
    return [
      { id: 'overview', label: 'Overview', href: '/dashboard.html',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v9h14v-9"/></svg>' },
      { id: 'places', label: 'Places', href: '/dashboard.html',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-6.1-7-11a7 7 0 0 1 14 0c0 4.9-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>' },
      { id: 'activity', label: 'Activity', href: '/dashboard.html',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2 8 4-16 2 8h6"/></svg>' },
      { id: 'order', label: 'Order', href: '/order.html',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18M16 10a4 4 0 0 1-8 0"/></svg>' },
      { id: 'menu', label: 'Menu', href: '/menu.html',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>' },
      { id: 'settings', label: 'Settings', href: '/',
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>' },
    ]
  }

  return { init, pages }
})()
