// Shared presentation helpers: colours, ordering, and time formatting.

// The production-line order. Main-line columns always render (so the "line" is
// visible even when empty); attention columns only render when populated.
export const LINE = [
  'discussion', 'queued', 'assessment', 'planning',
  'in_development', 'self_review', 'pr', 'user_review', 'done',
]
export const ATTENTION = ['needs_info', 'changes_requested', 'stalled']
// Dismissed work — its own lane at the end, only shown when it holds something.
export const CANCELLED = 'cancelled'

// Colour per status "kind" (from the backend meta) — drives column accents.
export const KIND_ACCENT = {
  discussion: 'border-slate-300 bg-slate-50',
  queue: 'border-amber-300 bg-amber-50',
  agent: 'border-indigo-300 bg-indigo-50',
  human_gate: 'border-rose-300 bg-rose-50',
  terminal: 'border-emerald-300 bg-emerald-50',
  cancelled: 'border-slate-300 bg-slate-100',
}
export const KIND_DOT = {
  discussion: 'bg-slate-400',
  queue: 'bg-amber-500',
  agent: 'bg-indigo-500',
  human_gate: 'bg-rose-500',
  terminal: 'bg-emerald-500',
  cancelled: 'bg-slate-400',
}

export const PRIORITY_BADGE = {
  P0: 'bg-red-600 text-white',
  P1: 'bg-orange-500 text-white',
  P2: 'bg-sky-600 text-white',
  P3: 'bg-slate-400 text-white',
}

export function relTime(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return `${days}d ago`
}

// Compact duration: 42s, 3m 10s, 1h 4m.
export function fmtDuration(secs) {
  if (secs == null || Number.isNaN(secs)) return ''
  secs = Math.round(secs)
  if (secs < 60) return `${secs}s`
  const m = Math.floor(secs / 60), s = secs % 60
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

// How far along the main line a ticket is (0..1), for the mini progress bar.
export function lineProgress(status) {
  const i = LINE.indexOf(status)
  if (i < 0) return null
  return i / (LINE.length - 1)
}
