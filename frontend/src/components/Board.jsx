import React from 'react'
import TicketCard from './TicketCard.jsx'
import { LINE, ATTENTION, CANCELLED, KIND_DOT } from '../ui.js'

// The production line. Main-line columns always render so the pipeline is
// visible even when empty; attention lanes (Needs Info / Changes Requested /
// Stalled) only appear when they hold something.
export default function Board({ tickets, statusMeta, onOpen }) {
  const byStatus = {}
  for (const t of tickets) {
    (byStatus[t.status] = byStatus[t.status] || []).push(t)
  }

  const columns = [
    ...LINE,
    ...ATTENTION.filter((s) => (byStatus[s] || []).length > 0),
    ...((byStatus[CANCELLED] || []).length > 0 ? [CANCELLED] : []),
  ]

  return (
    <div className="board-scroll flex gap-3 overflow-x-auto p-4 items-start">
      {columns.map((status) => {
        const meta = statusMeta[status] || { label: status, kind: '' }
        const items = byStatus[status] || []
        const isAttention = ATTENTION.includes(status)
        const isCancelled = status === CANCELLED
        return (
          <div
            key={status}
            className={`shrink-0 w-64 rounded-xl p-2 ${
              isCancelled ? 'bg-slate-200/60' : isAttention ? 'bg-rose-50/60' : 'bg-slate-200/40'
            }`}
          >
            <div className="flex items-center gap-1.5 px-1 py-1.5 mb-1">
              <span className={`w-2 h-2 rounded-full ${KIND_DOT[meta.kind] || 'bg-slate-400'}`} />
              <span className="text-xs font-semibold text-slate-700 uppercase tracking-wide">
                {meta.label}
              </span>
              <span className="ml-auto text-[11px] text-slate-400">{items.length}</span>
            </div>
            <div className={`min-h-[40px] ${isCancelled ? 'opacity-60' : ''}`}>
              {items.map((t) => <TicketCard key={t.id} ticket={t} onOpen={onOpen} />)}
              {items.length === 0 && (
                <div className="text-[11px] text-slate-400 px-1 py-2 italic">—</div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
