// Docket API client. Reuses the testing-hub login (tester JWT) for auth: the
// token is stored in localStorage and sent as a Bearer header on every call.
// A 401 clears the token and fires a 'docket-unauth' event so the app drops
// back to the login screen.

const TOKEN_KEY = 'docket-token'
const NAME_KEY = 'docket-name'

// The self-contained pages (/roadmap, /build) log in via the `testing_token`
// cookie; the SPA uses localStorage. Share the session across both so a deep-link
// from the roadmap into the board opens the ticket instead of a login screen.
function cookieToken() {
  const m = document.cookie.match(/(?:^|;\s*)testing_token=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}
export function getToken() { return localStorage.getItem(TOKEN_KEY) || cookieToken() }
export function getName() { return localStorage.getItem(NAME_KEY) || '' }
export function setSession(token, name) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(NAME_KEY, name || '')
  document.cookie = 'testing_token=' + token + '; path=/; max-age=' + 7 * 24 * 3600 + '; samesite=lax'
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(NAME_KEY)
  document.cookie = 'testing_token=; path=/; max-age=0'
}

// Hub handoff: the service hub opens a board as /docket/?sso=<tester-jwt>&name=<n>.
// Consume the token into the normal session and strip the params so the URL is
// clean and the token doesn't linger in history. Module scope — runs before the
// app's first getToken() read.
;(function consumeSso() {
  const params = new URLSearchParams(window.location.search)
  const sso = params.get('sso')
  if (!sso) return
  setSession(sso, params.get('name') || '')
  params.delete('sso')
  params.delete('name')
  const qs = params.toString()
  window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash)
})()

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  const t = getToken()
  if (t) headers['Authorization'] = `Bearer ${t}`
  const res = await fetch(path, { ...opts, headers })
  // A 401 on an authed call means the session lapsed — drop to the login screen.
  // A 401 on the login call itself is just bad credentials; let it fall through
  // to the normal error path so the real message ("Invalid username or password")
  // shows instead of a misleading "session expired".
  if (res.status === 401 && path !== '/api/testing/login') {
    clearSession()
    window.dispatchEvent(new Event('docket-unauth'))
    throw new Error('Session expired — please sign in again')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  login: (username, password) =>
    req('/api/testing/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  me: () => req('/api/testing/me'),

  meta: () => req('/api/tickets/meta'),
  pipelineStatus: () => req('/api/tickets/pipeline/status'),
  pipelineControl: (state) =>
    req('/api/tickets/pipeline/control', { method: 'POST', body: JSON.stringify({ state }) }),
  testers: () => req('/api/tickets/testers'),
  board: () => req('/api/tickets/board'),
  ticket: (id) => req(`/api/tickets/${id}`),
  create: (body) => req('/api/tickets', { method: 'POST', body: JSON.stringify(body) }),
  bulk: (tickets) => req('/api/tickets/bulk', { method: 'POST', body: JSON.stringify({ tickets }) }),
  importMd: (markdown, dryRun) =>
    req('/api/tickets/import', { method: 'POST', body: JSON.stringify({ markdown, dry_run: !!dryRun }) }),
  patch: (id, body) => req(`/api/tickets/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteTicket: (id) => req(`/api/tickets/${id}`, { method: 'DELETE' }),
  epics: () => req('/api/epics'),
  createEpic: (body) => req('/api/epics', { method: 'POST', body: JSON.stringify(body) }),
  patchEpic: (id, body) => req(`/api/epics/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteEpic: (id) => req(`/api/epics/${id}`, { method: 'DELETE' }),
  roadmapPatch: (id, body) => req(`/api/roadmap/tickets/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  toPipeline: (id, queue) => req(`/api/roadmap/tickets/${id}/pipeline`, { method: 'POST', body: JSON.stringify({ queue: !!queue }) }),
  submit: (id, body) => req(`/api/tickets/${id}/submit`, { method: 'POST', body: JSON.stringify(body || {}) }),
  transition: (id, to_status, summary) =>
    req(`/api/tickets/${id}/transition`, { method: 'POST', body: JSON.stringify({ to_status, summary: summary || '' }) }),
  resubmit: (id, body) =>
    req(`/api/tickets/${id}/resubmit`, { method: 'POST', body: JSON.stringify(body) }),
  comment: (id, text) =>
    req(`/api/tickets/${id}/comment`, { method: 'POST', body: JSON.stringify({ text }) }),

  // Coaching analytics + live clarity scoring + gamified tester profiles.
  analytics: () => req('/api/tickets/analytics'),
  clarity: (body) => req('/api/tickets/clarity', { method: 'POST', body: JSON.stringify(body) }),
  profiles: () => req('/api/tickets/profiles'),
  impact: (id, body) =>
    req(`/api/tickets/${id}/impact`, { method: 'POST', body: JSON.stringify(body) }),
  grade: (id, score, note) =>
    req(`/api/tickets/${id}/grade`, { method: 'POST', body: JSON.stringify({ score, note: note || '' }) }),
  resolveLink: (id, linkId, action) =>
    req(`/api/tickets/${id}/links/${linkId}/resolve`, { method: 'POST', body: JSON.stringify({ action }) }),

  // Migrated testing-hub checklist (per-tester pass/fail/blocked of shipped behaviours).
  checklist: () => req('/api/testing/checklist'),
  feedback: () => req('/api/testing/feedback'),
  postFeedback: (item_id, status, note) =>
    req('/api/testing/feedback', { method: 'POST', body: JSON.stringify({ item_id, status, note }) }),
  assignItem: (item_id, assignee) =>
    req('/api/testing/assign', { method: 'POST', body: JSON.stringify({ item_id, assignee }) }),
  itemComment: (item_id, text) =>
    req('/api/testing/item-comment', { method: 'POST', body: JSON.stringify({ item_id, text }) }),
}
