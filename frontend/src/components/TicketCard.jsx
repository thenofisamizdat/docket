import React from 'react'
import { Bug, Sparkles, Activity, RefreshCw, Timer, BookOpen, CheckSquare } from 'lucide-react'
import { PRIORITY_BADGE, relTime, lineProgress, fmtDuration } from '../ui.js'

// One glyph per work-item type so a lane scans by shape, not just color.
const TYPE_ICON = {
  bug: <Bug className="w-3.5 h-3.5 text-rose-500 mt-0.5 shrink-0" />,
  story: <BookOpen className="w-3.5 h-3.5 text-emerald-600 mt-0.5 shrink-0" />,
  task: <CheckSquare className="w-3.5 h-3.5 text-sky-600 mt-0.5 shrink-0" />,
  feature: <Sparkles className="w-3.5 h-3.5 text-indigo-500 mt-0.5 shrink-0" />,
}

// A single ticket card on the board. Shows the ref, type, priority, title, and
// — the point of the whole thing — live signal: the agent's current activity,
// queue position, iteration count, and a mini main-line progress bar.
export default function TicketCard({ ticket, onOpen }) {
  const prog = lineProgress(ticket.status)
  return (
    <button
      onClick={() => onOpen(ticket.id)}
      className="w-full text-left bg-white rounded-lg border border-slate-200 hover:border-indigo-400 hover:shadow-sm transition p-3 mb-2"
      style={ticket.epic_color ? { borderLeft: `3px solid ${ticket.epic_color}` } : undefined}
    >
      <div className="flex items-center justify-between mb-1 gap-1">
        <span className="font-mono text-[11px] text-slate-400">{ticket.ref}</span>
        {ticket.epic_name && (
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full truncate"
            style={{ background: `${ticket.epic_color}1f`, color: ticket.epic_color }}
            title={`Epic: ${ticket.epic_name}`}>
            {ticket.epic_name}
          </span>
        )}
        <span className="flex-1" />
        {ticket.engine && (
          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wide shrink-0 ${
            ticket.engine === 'codex' ? 'bg-teal-100 text-teal-800' : 'bg-indigo-100 text-indigo-700'}`}
            title={`build engine: ${ticket.engine}`}>
            ⚙ {ticket.engine}
          </span>
        )}
        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded shrink-0 ${PRIORITY_BADGE[ticket.priority] || 'bg-slate-300'}`}>
          {ticket.priority}
        </span>
      </div>

      <div className="flex items-start gap-1.5">
        {TYPE_ICON[ticket.type] || TYPE_ICON.feature}
        <span className="text-sm text-slate-800 leading-snug">
          {ticket.parent_ref && (
            <span className="font-mono text-[10px] text-slate-400 mr-1" title={`part of story ${ticket.parent_ref}`}>
              ↳ {ticket.parent_ref}
            </span>
          )}
          {ticket.title}
        </span>
      </div>

      {ticket.status === 'queued' && ticket.position != null && (
        <div className="mt-2 text-[11px] text-amber-700 font-medium">
          Position #{ticket.position} · next up{ticket.position === 1 ? '' : ` after ${ticket.position - 1}`}
        </div>
      )}

      {ticket.current_activity && ticket.status_kind === 'agent' && (
        <div className="mt-2 flex items-center gap-1 text-[11px] text-indigo-700">
          <Activity className="w-3 h-3 animate-pulse shrink-0" />
          <span className="truncate">{ticket.current_activity}</span>
        </div>
      )}

      {prog != null && (
        <div className="mt-2 h-1 rounded-full bg-slate-100 overflow-hidden">
          <div className="h-full bg-indigo-400" style={{ width: `${Math.round(prog * 100)}%` }} />
        </div>
      )}

      <div className="mt-2 flex items-center justify-between text-[10px] text-slate-400">
        <span className="flex items-center gap-1" title={ticket.assignee
          ? `assigned to ${ticket.assignee} (raised by ${ticket.created_by || '—'})`
          : `unassigned (raised by ${ticket.created_by || '—'})`}>
          {ticket.assignee ? (
            <>
              <span className="w-4 h-4 rounded-full bg-indigo-100 text-indigo-700 font-bold text-[8px] inline-flex items-center justify-center">
                {ticket.assignee.trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase()}
              </span>
              <span className="text-slate-600 font-medium">{ticket.assignee}</span>
            </>
          ) : (
            <span className="italic">unassigned · by {ticket.created_by || '—'}</span>
          )}
        </span>
        <span className="flex items-center gap-2">
          {ticket.effort && (
            <span className="flex items-center gap-0.5 text-slate-500" title="agent effort so far (time · cost)">
              <Timer className="w-2.5 h-2.5" />
              {fmtDuration(ticket.effort.secs)} · ${ticket.effort.cost.toFixed(2)}
            </span>
          )}
          {ticket.iteration > 0 && (
            <span className="flex items-center gap-0.5 text-slate-500" title="re-submitted">
              <RefreshCw className="w-2.5 h-2.5" />×{ticket.iteration}
            </span>
          )}
          {relTime(ticket.updated_at)}
        </span>
      </div>
    </button>
  )
}
