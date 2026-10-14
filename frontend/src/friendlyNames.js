// Display-only wording for the operator UI. The backend/terminal logs keep
// their technical names (argo_sonic_nav.py's step names); only what the UI
// shows is translated here.

const STEP_NAMES = {
  'Robot State Publisher': 'robot body model',
  'Camera TF Bridge':      'camera position',
  'Serial Bridge':         'motor controller',
  'Scan Relay':            'obstacle sensor feed',
  'RPLidar A1':            'obstacle sensor',
  'SLAM Toolbox':          'map & position tracking',
  'NTFields Planner':      'route planner',
  'Controller Server':     'path follower',
  'Velocity Smoother':     'smooth driving',
  'Behavior Server':       'recovery behaviours',
  'BT Navigator':          'navigation brain',
  'Depth Camera':          'depth camera',
  'PC Restamper':          'depth camera data',
  'Safety Shield':         'safety system',
  'Patrol Manager':        'patrol manager',
  'RViz':                  'visualiser',
}

// Whole-message rewrites for the non-"Starting …" messages navProgress carries.
const MESSAGES = [
  [/^Nav2 fully activated.*/i,                'Ready — Argo can navigate'],
  [/^Stack shut down/i,                       'Argo stopped'],
  [/^RPLidar failed.*/i,                      'Obstacle sensor did not start — check its USB cable'],
  [/^NTFields model not found.*/i,            'Route model missing for this map — train it first'],
  [/^NTFields planner.*failed to activate.*/i, 'Route planner did not start — check this map\'s model'],
  [/^NTFields planner failed.*/i,             'Route planner did not start'],
  [/^bt_navigator failed.*/i,                 'Navigation did not start'],
  [/^Workspace not built.*/i,                 'Robot software is not built yet'],
  [/^FAILED to bring up (.+?) after .*/i,     (_, n) => `Could not start ${STEP_NAMES[n.trim()] || n}`],
  [/^FAILED\s*>>\s*(.+?) exited on startup.*/i, (_, n) => `${cap(STEP_NAMES[n.trim()] || n)} stopped unexpectedly`],
  [/^Starting\s+(.+?)(\s+\(attempt (\d+)\/(\d+)\))?$/i,
    (_, n, __, a, t) => `Starting ${STEP_NAMES[n.trim()] || n}…` + (a && a !== '1' ? ` (try ${a} of ${t})` : '')],
  [/^(.+?) never appeared.*/i,                (_, n) => `${cap(STEP_NAMES[n.trim()] || 'A component')} did not start`],
  [/^Velocity smoother not publishing.*/i,    'Smooth driving did not start'],
]

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s }

export function friendlyNavMessage(msg) {
  if (!msg) return msg
  for (const [re, out] of MESSAGES) {
    if (re.test(msg)) return msg.replace(re, out)
  }
  return msg
}

// Teleop-pad wording.
export const TELEOP_BADGE = {
  starting:        ['Getting ready…',      'var(--gold-bright)'],
  started:         ['Ready to drive',      '#4ade80'],
  already_running: ['Ready to drive',      '#4ade80'],
  nav_starting:    ['Waiting for Argo to finish starting', 'var(--gold-bright)'],
  estopped:        ['Emergency stop active', 'var(--danger)'],
  error:           ['Can\'t reach the robot', 'var(--danger)'],
}
