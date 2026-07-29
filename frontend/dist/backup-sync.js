// Keeps the menu catalog + store settings safe from an accidental browser
// cache/storage clear by mirroring localStorage to a JSON file the user picks
// on their own machine, via the File System Access API.
//
// localStorage stays the fast synchronous read/write path (menu-data.js is
// unchanged there) — nothing here ever makes the UI wait on disk I/O. Every
// saveMenu()/saveSettings() call fires an 'argo:data-saved' event; this module
// listens for it and writes a debounced snapshot to the linked file in the
// background.
//
// Caveats, since this is a purely client-side static app with no server:
//  - File System Access API is Chromium-only (Chrome/Edge), and only works
//    from a secure context: https:// or http://localhost. Opening the page as
//    http://<lan-ip>:3000 will not support linking a live file.
//  - The linked file *handle* is stored in IndexedDB so it survives normal
//    reloads and most "clear cache" actions. If the user wipes site data
//    entirely (cookies + storage), the handle itself is gone too — but the
//    JSON file on disk still has the data, so re-linking that same file
//    (via "Use Existing File") restores it in one step.
// Everywhere else (Firefox, Safari, or an insecure origin), menu.html's
// manual Export/Import JSON buttons are the fallback — they work anywhere.
const ArgoBackupSync = (() => {
  const DB_NAME = 'argo-backup'
  const STORE = 'handles'
  const HANDLE_KEY = 'menuBackupFile'
  const WRITE_DEBOUNCE_MS = 500

  let fileHandle = null
  let writeTimer = null
  const statusListeners = []

  function supported() {
    return typeof window.showSaveFilePicker === 'function'
  }

  function notify(status) {
    statusListeners.forEach(fn => fn(status))
  }
  function onStatus(fn) {
    statusListeners.push(fn)
  }

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1)
      req.onupgradeneeded = () => req.result.createObjectStore(STORE)
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })
  }
  async function idbGet(key) {
    const db = await openDb()
    return new Promise((resolve, reject) => {
      const req = db.transaction(STORE, 'readonly').objectStore(STORE).get(key)
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })
  }
  async function idbSet(key, val) {
    const db = await openDb()
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).put(val, key)
      tx.oncomplete = resolve
      tx.onerror = () => reject(tx.error)
    })
  }
  async function idbDelete(key) {
    const db = await openDb()
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).delete(key)
      tx.oncomplete = resolve
      tx.onerror = () => reject(tx.error)
    })
  }

  function snapshot() {
    return { menu: loadMenu(), settings: loadSettings(), savedAt: new Date().toISOString() }
  }

  function applySnapshot(text) {
    const data = JSON.parse(text)
    if (Array.isArray(data.menu) && data.menu.length) saveMenu(data.menu)
    if (data.settings) saveSettings(data.settings)
  }

  async function writeNow() {
    if (!fileHandle) return
    try {
      const perm = await fileHandle.queryPermission({ mode: 'readwrite' })
      if (perm !== 'granted') { notify({ state: 'reconnect', name: fileHandle.name }); return }
      const writable = await fileHandle.createWritable()
      await writable.write(JSON.stringify(snapshot(), null, 2))
      await writable.close()
      notify({ state: 'synced', name: fileHandle.name, at: new Date() })
    } catch (err) {
      notify({ state: 'error', name: fileHandle && fileHandle.name, error: err.message })
    }
  }

  // Fire-and-forget: callers never await this, so a slow disk never blocks
  // the UI. Debounced so rapid edits (e.g. typing) don't thrash the file.
  function scheduleWrite() {
    clearTimeout(writeTimer)
    writeTimer = setTimeout(writeNow, WRITE_DEBOUNCE_MS)
  }

  async function linkNewFile() {
    const handle = await window.showSaveFilePicker({
      suggestedName: 'argo-menu-backup.json',
      types: [{ description: 'JSON backup', accept: { 'application/json': ['.json'] } }],
    })
    fileHandle = handle
    await idbSet(HANDLE_KEY, handle)
    await writeNow()
    return handle
  }

  // Points at a file that (usually) already has data in it — e.g. re-linking
  // after a full cache wipe lost the old handle. Whatever is in the file wins.
  async function linkExistingFile() {
    const [handle] = await window.showOpenFilePicker({
      types: [{ description: 'JSON backup', accept: { 'application/json': ['.json'] } }],
    })
    fileHandle = handle
    await idbSet(HANDLE_KEY, handle)
    try {
      applySnapshot(await (await handle.getFile()).text())
    } catch { /* empty/invalid file is fine — the next edit will populate it */ }
    notify({ state: 'synced', name: handle.name })
    return handle
  }

  async function unlink() {
    fileHandle = null
    await idbDelete(HANDLE_KEY)
    notify({ state: 'unlinked' })
  }

  async function reconnect() {
    if (!fileHandle) return false
    const perm = await fileHandle.requestPermission({ mode: 'readwrite' })
    if (perm === 'granted') { await writeNow(); return true }
    notify({ state: 'reconnect', name: fileHandle.name })
    return false
  }

  async function init() {
    if (!supported()) { notify({ state: 'unsupported' }); return }
    window.addEventListener('argo:data-saved', scheduleWrite)

    let handle
    try { handle = await idbGet(HANDLE_KEY) } catch { handle = null }
    if (!handle) { notify({ state: 'unlinked' }); return }
    fileHandle = handle

    try {
      const perm = await handle.queryPermission({ mode: 'readwrite' })
      if (perm !== 'granted') { notify({ state: 'reconnect', name: handle.name }); return }
      // A missing localStorage menu key means storage was cleared since last
      // visit — the linked file is the last known-good copy, so restore it.
      if (!localStorage.getItem(MENU_STORAGE_KEY)) {
        try { applySnapshot(await (await handle.getFile()).text()) } catch { /* file empty/unreadable */ }
      }
      notify({ state: 'synced', name: handle.name })
    } catch (err) {
      notify({ state: 'error', name: handle.name, error: err.message })
    }
  }

  return { supported, init, linkNewFile, linkExistingFile, unlink, reconnect, onStatus, writeNow }
})()
