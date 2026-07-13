import React, { useEffect, useState, useCallback } from 'react'
import {
  X, Bug, Sparkles, ArrowRight, Activity, ClipboardCheck,
  ListChecks, MessageSquare, StickyNote, ExternalLink, RefreshCw, Check, Star,
} from 'lucide-react'
import { api, getName } from '../api.js'
import { PRIORITY_BADGE, KIND_DOT, relTime, fmtDuration } from '../ui.js'
import AmendModal from './AmendModal.jsx'
import Markdown from './Markdown.jsx'

// Long-form entries (agent assessment/plan/notes) render as markdown cards;
// short entries (transitions, activity, comments) render inline.
const LONG_KINDS = new Set(['assessment', 'plan', 'note'])

// Strip the agent's trailing machine-readable control line so it doesn't show.
function cleanBody(text) {
  return (text || '').replace(/\n*\b(VERDICT|REVIEW)\s*:.*$/is, '').trim()
}

// Roll up the agent's effort across all phases from the event payloads.
function computeEffort(events) {
  let secs = 0, cost = 0, turns = 0, phases = 0
  for (const e of events || []) {
    const p = e.payload && typeof e.payload === 'object' ? e.payload : null
    if (!p) continue
    if (p.duration_secs != null) { secs += p.duration_secs; phases++ }
    if (p.cost_usd != null) cost += p.cost_usd
    if (p.turns != null) turns += p.turns
  }
  return phases ? { secs, cost, turns, phases } : null
}

// Moving to "queued" from one of these is a resubmit — open the amend modal
// instead of a bare transition, so the tester records what changed.
const RESUBMIT_FROM = ['user_review', 'needs_info', 'stalled']

const KIND_ICON = {
  transition: ArrowRight,
  activity: Activity,
  assessment: ClipboardCheck,
  plan: ListChecks,
  comment: MessageSquare,
  note: StickyNote,
  impact: Star,
}

// Friendlier labels for the lifecycle buttons, keyed by "from->to".
const MOVE_LABEL = {
  'discussion->queued': 'Submit for processing',
  'pr->user_review': 'Approve → User Review',
  'pr->changes_requested': 'Request changes',
  'user_review->done': 'Pass — close ticket',
  'user_review->queued': 'Fail — amend & requeue',
  'user_review->discussion': 'Send back to discussion',
  'needs_info->queued': 'Info provided — requeue',
  'stalled->queued': 'Retry — requeue',
  'cancelled->discussion': 'Reopen → Discussion',
  'cancelled->queued': 'Reopen → Queue',
}

export default function TicketDetail({ ticketId, meta, onClose, onChanged }) {
  const [t, setT] = useState(null)
  const [comment, setComment] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [amending, setAmending] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await api.ticket(ticketId)
      setT(r.ticket)
    } catch (e) {
      setErr(e.message)
    }
  }, [ticketId])

  useEffect(() => {
    load()
    const iv = setInterval(load, 3000) // live timeline while open
    return () => clearInterval(iv)
  }, [load])

  async function move(to) {
    setBusy(true); setErr('')
    try {
      await api.transition(ticketId, to)
      await load(); onChanged && onChanged()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function sendComment(e) {
    e.preventDefault()
    if (!comment.trim()) return
    setBusy(true); setErr('')
    try {
      await api.comment(ticketId, comment.trim())
      setComment(''); await load()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const meta_status = t ? (meta.status_meta[t.status] || {}) : {}
  const nextMoves = t ? (meta.transitions[t.status] || []) : []
  const effort = t ? computeEffort(t.events) : null

  return (
    <div className="fixed inset-0 bg-black/30 flex justify-end z-40" onClick={onClose}>
      <div
        className="w-full max-w-xl bg-white h-full shadow-xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {!t ? (
          <div className="p-8 text-slate-400">{err || 'Loading…'}</div>
        ) : (
          <>
            {/* Header */}
            <div className="sticky top-0 bg-white border-b border-slate-200 p-4 flex items-start gap-3">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-mono text-xs text-slate-400">{t.ref}</span>
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${PRIORITY_BADGE[t.priority]}`}>{t.priority}</span>
                  <span className="flex items-center gap-1 text-[11px] text-slate-500">
                    <span className={`w-2 h-2 rounded-full ${KIND_DOT[meta_status.kind] || 'bg-slate-400'}`} />
                    {meta_status.label || t.status}
                  </span>
                  {t.iteration > 0 && (
                    <span className="flex items-center gap-0.5 text-[11px] text-slate-500" title="re-submitted">
                      <RefreshCw className="w-3 h-3" />×{t.iteration}
                    </span>
                  )}
                </div>
                <div className="flex items-start gap-1.5">
                  {t.type === 'bug'
                    ? <Bug className="w-4 h-4 text-rose-500 mt-1 shrink-0" />
                    : <Sparkles className="w-4 h-4 text-indigo-500 mt-1 shrink-0" />}
                  <h2 className="text-lg font-semibold text-slate-800 leading-snug">{t.title}</h2>
                </div>
              </div>
              <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X className="w-5 h-5" /></button>
            </div>

            <div className="p-4 space-y-4">
              {err && <div className="text-sm text-red-600">{err}</div>}

              {/* User Review — the tester's turn */}
              {t.status === 'user_review' && (
                <div className="rounded-xl border-2 border-emerald-300 bg-emerald-50 p-4">
                  <div className="flex items-center gap-2 mb-1">
                    <ClipboardCheck className="w-4 h-4 text-emerald-700" />
                    <h3 className="text-sm font-semibold text-emerald-800">Ready for you to test</h3>
                  </div>
                  <p className="text-xs text-emerald-700 mb-2">
                    Follow the steps below. If it works, close it. If not, send it back with what's wrong.
                  </p>
                  <div className="bg-white rounded-lg border border-emerald-100 p-3 mb-3">
                    {t.test_instructions
                      ? <Markdown>{t.test_instructions}</Markdown>
                      : <p className="text-sm text-slate-400 italic">No test instructions were provided.</p>}
                  </div>
                  <div className="flex gap-2">
                    <button disabled={busy} onClick={() => move('done')}
                      className="flex items-center gap-1 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-sm font-medium disabled:opacity-50">
                      <Check className="w-4 h-4" /> It works — close
                    </button>
                    <button disabled={busy} onClick={() => setAmending(true)}
                      className="flex items-center gap-1 px-3 py-1.5 bg-white border border-rose-300 text-rose-700 hover:bg-rose-50 rounded-lg text-sm font-medium disabled:opacity-50">
                      <X className="w-4 h-4" /> Needs more — send back
                    </button>
                  </div>
                </div>
              )}

              {/* Post-ship impact rating — feeds the profiles' impact dimension */}
              {t.status === 'done' && <ImpactPanel t={t} onRated={load} />}

              {/* Real post-ship performance from platform telemetry */}
              {t.perf && <PerfStrip perf={t.perf} />}

              {/* Relatedness: is this a follow-up of shipped work / does shipped
                  work have follow-ups against it? */}
              <RelatedPanel t={t} onChanged={async () => { await load(); onChanged && onChanged() }} />

              {/* Lifecycle actions */}
              {nextMoves.length > 0 && t.status !== 'user_review' && (
                <div className="flex flex-wrap gap-2">
                  {nextMoves.map((to) => {
                    const resubmit = to === 'queued' && RESUBMIT_FROM.includes(t.status)
                    const cancel = to === 'cancelled'
                    return (
                      <button key={to} disabled={busy}
                        onClick={() => (resubmit ? setAmending(true) : move(to))}
                        className={`px-3 py-1.5 text-xs font-medium rounded-lg border disabled:opacity-50 ${
                          cancel
                            ? 'border-slate-300 text-slate-500 hover:bg-slate-100'
                            : resubmit
                            ? 'border-amber-300 text-amber-700 hover:bg-amber-50'
                            : 'border-indigo-300 text-indigo-700 hover:bg-indigo-50'}`}>
                        {cancel ? "Won't do" : MOVE_LABEL[`${t.status}->${to}`] || (meta.status_meta[to]?.label) || to}
                      </button>
                    )
                  })}
                </div>
              )}

              {t.position != null && (
                <div className="text-sm text-amber-700 bg-amber-50 rounded-lg px-3 py-2">
                  In the queue — position #{t.position}.
                </div>
              )}

              {t.pr_url && (
                <a href={t.pr_url} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1 text-sm text-indigo-600 hover:underline">
                  <ExternalLink className="w-3.5 h-3.5" /> View PR
                </a>
              )}

              {effort && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2">
                  <span className="font-semibold text-indigo-700">Agent effort</span>
                  <span className="text-slate-700">⏱ {fmtDuration(effort.secs)}</span>
                  <span className="text-slate-400">·</span>
                  <span className="text-slate-700">${effort.cost.toFixed(2)}</span>
                  <span className="text-slate-400">·</span>
                  <span className="text-slate-700">{effort.turns} turns</span>
                  <span className="text-slate-400">·</span>
                  <span className="text-slate-700">{effort.phases} phases</span>
                </div>
              )}

              <Section title="Description" body={t.description} />
              <Section title="Acceptance criteria" body={t.acceptance_criteria} />
              {t.test_instructions && t.status !== 'user_review' && <Section title="How to test" body={t.test_instructions} />}

              {/* History — readable entries (explanations + comments +
                  milestones) up front; the full system-stage log tucked into a
                  collapsible per iteration so a resubmitted ticket keeps BOTH
                  the clean story and the detailed record of every attempt. */}
              <div>
                <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">History</h3>
                <div className="space-y-4">
                  {(() => {
                    const segs = groupByIteration(t.events)
                    const multi = segs.length > 1
                    return segs.map((seg) => (
                      <IterationBlock key={seg.iter} seg={seg} multi={multi} />
                    ))
                  })()}
                </div>
              </div>

              {/* Comment */}
              <form onSubmit={sendComment} className="pt-2">
                <textarea
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm h-16 focus:outline-none focus:ring-2 focus:ring-indigo-300"
                  value={comment} onChange={(e) => setComment(e.target.value)}
                  placeholder="Add a comment…"
                />
                <div className="flex justify-end mt-2">
                  <button type="submit" disabled={busy || !comment.trim()}
                    className="px-3 py-1.5 bg-slate-800 hover:bg-slate-900 disabled:opacity-50 text-white rounded-lg text-xs font-medium">
                    Comment
                  </button>
                </div>
              </form>
            </div>
          </>
        )}
      </div>

      {amending && t && (
        <AmendModal
          ticket={t} meta={meta}
          onClose={() => setAmending(false)}
          onDone={async () => { setAmending(false); await load(); onChanged && onChanged() }}
        />
      )}
    </div>
  )
}

const PERF_CHIP = {
  healthy: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  degraded: 'bg-rose-50 text-rose-700 border-rose-200',
  watch: 'bg-amber-50 text-amber-700 border-amber-200',
  no_traffic: 'bg-slate-50 text-slate-500 border-slate-200',
}
const PERF_LABEL = {
  healthy: 'healthy', degraded: 'errors above baseline',
  watch: 'platform errors since ship — unattributed', no_traffic: 'no traffic yet',
}

// Live platform telemetry for the routes this shipped ticket touched.
function PerfStrip({ perf }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs bg-violet-50 border border-violet-100 rounded-lg px-3 py-2">
      <span className="font-semibold text-violet-700">In the platform</span>
      <span className={`px-1.5 py-0.5 rounded border text-[11px] font-medium ${PERF_CHIP[perf.verdict] || ''}`}>
        {PERF_LABEL[perf.verdict] || perf.verdict}
      </span>
      <span className="text-slate-700">{perf.hits} requests since ship</span>
      {perf.hits > 0 && (
        <>
          <span className="text-slate-400">·</span>
          <span className={perf.errors ? 'text-rose-600' : 'text-slate-700'}>
            {perf.errors} errors ({Math.round(perf.err_rate * 100)}%)
          </span>
          {perf.avg_ms != null && (
            <><span className="text-slate-400">·</span>
              <span className="text-slate-700">{Math.round(perf.avg_ms)}ms avg</span></>
          )}
        </>
      )}
      <span className="w-full text-[10px] text-slate-400">
        routes: {perf.routes.join(', ')}
      </span>
    </div>
  )
}

// Relatedness links. Outgoing = this ticket may be a follow-up of shipped work
// (suspected ones carry confirm/dismiss — confirming counts against the old
// ticket's health). Incoming = follow-ups reported against THIS shipped ticket.
function RelatedPanel({ t, onChanged }) {
  const links = t.links || { out: [], in: [] }
  const out = links.out.filter((l) => l.status !== 'dismissed')
  const inn = links.in.filter((l) => l.status !== 'dismissed')
  const [busy, setBusy] = useState(false)
  if (out.length === 0 && inn.length === 0) return null

  async function resolve(linkId, action) {
    setBusy(true)
    try { await api.resolveLink(t.id, linkId, action); onChanged && onChanged() }
    finally { setBusy(false) }
  }

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3 space-y-2">
      <h3 className="text-xs font-semibold text-amber-800 uppercase tracking-wide">Related shipped work</h3>
      {out.map((l) => (
        <div key={l.id} className="text-sm">
          <span className="text-slate-700">
            {l.status === 'confirmed' ? 'Follow-up of ' : 'Possibly related to '}
            <span className="font-mono text-xs">{l.other_ref}</span> “{l.other_title}”
          </span>
          <span className="ml-1.5 text-[10px] uppercase text-slate-400">
            {l.status === 'confirmed' ? `confirmed · ${l.source}` : `unconfirmed${l.score ? ` · ${Math.round(l.score * 100)}%` : ''}`}
          </span>
          {l.note && <div className="text-xs text-slate-500">{l.note}</div>}
          {l.status === 'suspected' && (
            <div className="flex gap-2 mt-1">
              <button disabled={busy} onClick={() => resolve(l.id, 'confirm')}
                className="px-2 py-1 text-[11px] font-medium rounded border border-rose-300 text-rose-700 hover:bg-rose-50 disabled:opacity-50">
                Yes — {l.other_ref}'s fix didn't solve this
              </button>
              <button disabled={busy} onClick={() => resolve(l.id, 'dismiss')}
                className="px-2 py-1 text-[11px] font-medium rounded border border-slate-300 text-slate-600 hover:bg-white disabled:opacity-50">
                Not related
              </button>
            </div>
          )}
        </div>
      ))}
      {inn.map((l) => (
        <div key={l.id} className="text-sm text-slate-700">
          {l.status === 'confirmed'
            ? <>⚠ Follow-up reported: <span className="font-mono text-xs">{l.other_ref}</span> “{l.other_title}” — counts against this ticket's post-ship health.</>
            : <>Possible follow-up (unconfirmed): <span className="font-mono text-xs">{l.other_ref}</span> “{l.other_title}”</>}
        </div>
      ))}
    </div>
  )
}

// "How's this working out?" — 1-5 stars + optional note on a Done ticket.
// Latest rating per tester wins; the profile maths aggregates them into the
// creator's "shipped impact" dimension.
function ImpactPanel({ t, onRated }) {
  const me = (getName() || '').trim().toLowerCase()
  const latest = {} // rater(normalised) -> {rating, note}
  for (const ev of t.events || []) {
    if (ev.kind === 'impact' && ev.payload && typeof ev.payload === 'object' && ev.payload.rating) {
      latest[(ev.actor || '').trim().toLowerCase()] = ev.payload
    }
  }
  const mine = latest[me]
  const others = Object.entries(latest).filter(([who]) => who !== me)
  const all = Object.values(latest).map((r) => r.rating)
  const avg = all.length ? (all.reduce((a, b) => a + b, 0) / all.length).toFixed(1) : null

  const [stars, setStars] = useState(mine?.rating || 0)
  const [hover, setHover] = useState(0)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function rate(n) {
    setStars(n); setBusy(true); setErr('')
    try {
      await api.impact(t.id, { rating: n, note: note.trim() })
      setNote('')
      onRated && onRated()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="rounded-xl border border-violet-200 bg-violet-50 p-4">
      <div className="flex items-center gap-2 mb-1">
        <Star className="w-4 h-4 text-violet-600" />
        <h3 className="text-sm font-semibold text-violet-800">How's this working out?</h3>
        {avg && <span className="ml-auto text-xs text-violet-700">team ★{avg} ({all.length})</span>}
      </div>
      <p className="text-xs text-violet-700 mb-2">
        Rate the shipped result — it feeds the creator's impact score.
        {mine && ' You can change your rating any time.'}
      </p>
      <div className="flex items-center gap-2">
        <div className="flex" onMouseLeave={() => setHover(0)}>
          {[1, 2, 3, 4, 5].map((n) => (
            <button key={n} disabled={busy} onClick={() => rate(n)} onMouseEnter={() => setHover(n)}
              className="p-0.5 disabled:opacity-50" title={`${n}/5`}>
              <Star className={`w-5 h-5 ${(hover || stars) >= n
                ? 'fill-amber-400 text-amber-400' : 'text-slate-300'}`} />
            </button>
          ))}
        </div>
        <input
          className="flex-1 px-2 py-1 border border-violet-200 rounded-lg text-xs bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
          value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="Optional note — sent with your next star click"
        />
      </div>
      {others.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {others.map(([who, r]) => (
            <span key={who} className="text-[10px] bg-white border border-violet-100 rounded px-1.5 py-0.5 text-slate-600"
              title={r.note || ''}>
              {who} ★{r.rating}
            </span>
          ))}
        </div>
      )}
      {err && <div className="mt-2 text-xs text-red-600">{err}</div>}
    </div>
  )
}

function Section({ title, body }) {
  return (
    <div>
      <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">{title}</h3>
      {body
        ? <Markdown>{body}</Markdown>
        : <p className="text-sm text-slate-400 italic">—</p>}
    </div>
  )
}

const KIND_LABEL = {
  assessment: 'Assessment', plan: 'Plan', note: 'Note',
  comment: 'Comment', transition: 'Status', activity: 'Activity',
}

// Status changes worth showing in the READABLE history (milestones). The
// intermediate pipeline hops (assessment/planning/in_development/self_review)
// are system-stage noise → they live only in the detailed log.
const MILESTONE = new Set([
  'queued', 'changes_requested', 'pr', 'user_review', 'done',
  'needs_info', 'stalled', 'cancelled', 'discussion',
])

// A readable entry = an explanation (assessment/plan/note), a comment, an
// impact rating, or a MILESTONE status change. Everything else — the activity
// ticker and intermediate transitions — is detail-log only.
function isReadable(ev) {
  if (!ev) return false
  if (ev.kind === 'activity') return false
  if (ev.kind === 'transition') return MILESTONE.has(ev.phase)
  return true
}

// Split the flat event stream into iteration segments. A new iteration starts
// each time the ticket re-enters the queue from a failed/bounced state
// (user_review / needs_info / stalled → queued), i.e. a resubmit. The very
// first submit (discussion → queued) is NOT a boundary.
function groupByIteration(events) {
  const segs = []
  let cur = { iter: 1, events: [] }
  segs.push(cur)
  const resubmit = /^(user_review|needs_info|stalled)\s*[→\->]/
  for (const ev of (events || [])) {
    if (ev.kind === 'transition' && ev.phase === 'queued'
        && resubmit.test(ev.summary || '')) {
      cur = { iter: cur.iter + 1, events: [] }
      segs.push(cur)
    }
    cur.events.push(ev)
  }
  return segs
}

// Collapse whitespace + clip a long body to a single detail-log line.
function oneLine(s, n = 140) {
  const t = (s || '').replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n - 1) + '…' : t
}

function IterationBlock({ seg, multi }) {
  const readable = seg.events.filter(isReadable)
  return (
    <div>
      {multi && (
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
            Attempt {seg.iter}
          </span>
          <span className="h-px flex-1 bg-slate-100" />
        </div>
      )}
      <div className="space-y-2">
        {readable.length === 0
          ? <div className="text-xs text-slate-400 italic">No summary entries yet.</div>
          : readable.map((ev) => <TimelineEvent key={ev.id} ev={ev} />)}
      </div>
      {/* Full system-stage record for this attempt — collapsed by default. */}
      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] text-slate-400 hover:text-slate-600 select-none">
          ⚙ Detailed log · {seg.events.length} step{seg.events.length === 1 ? '' : 's'}
        </summary>
        <div className="mt-1.5 pl-3 border-l border-slate-100 space-y-1">
          {seg.events.map((ev) => <DetailEvent key={ev.id} ev={ev} />)}
        </div>
      </details>
    </div>
  )
}

// Compact one-liner for the detailed log — every event, including the activity
// ticker, on a single tight row.
function DetailEvent({ ev }) {
  const Icon = KIND_ICON[ev.kind] || Activity
  const text = ev.kind === 'activity'
    ? ev.summary
    : `${KIND_LABEL[ev.kind] || ev.kind}${ev.summary ? `: ${cleanBody(ev.summary)}` : ''}`
  return (
    <div className="flex gap-2 text-[11px] text-slate-500">
      <Icon className="w-3 h-3 mt-0.5 shrink-0 text-slate-300" />
      <div className="flex-1 min-w-0 truncate">
        {oneLine(text)}
        <span className="ml-2 text-slate-400">{ev.actor || 'system'} · {relTime(ev.ts)}</span>
      </div>
    </div>
  )
}

function TimelineEvent({ ev }) {
  const Icon = KIND_ICON[ev.kind] || StickyNote
  const cost = ev.payload && typeof ev.payload === 'object' ? ev.payload : null
  const meta = [ev.actor || 'system', relTime(ev.ts)]
  if (cost && cost.duration_secs != null) meta.push(`⏱ ${fmtDuration(cost.duration_secs)}`)
  if (cost && cost.cost_usd != null) meta.push(`$${Number(cost.cost_usd).toFixed(3)}`)
  if (cost && cost.turns != null) meta.push(`${cost.turns} turns`)

  if (LONG_KINDS.has(ev.kind)) {
    return (
      <div className="flex gap-2">
        <Icon className="w-3.5 h-3.5 text-slate-400 mt-1 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-[11px] text-slate-400 mb-1">
            {KIND_LABEL[ev.kind] || ev.kind} · {meta.join(' · ')}
          </div>
          <div className="bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 overflow-x-auto">
            <Markdown>{cleanBody(ev.summary)}</Markdown>
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className="flex gap-2 text-sm">
      <Icon className="w-3.5 h-3.5 text-slate-400 mt-0.5 shrink-0" />
      <div className="flex-1">
        <span className="text-slate-700">{ev.summary}</span>
        <span className="ml-2 text-[11px] text-slate-400">{ev.actor || 'system'} · {relTime(ev.ts)}</span>
      </div>
    </div>
  )
}
