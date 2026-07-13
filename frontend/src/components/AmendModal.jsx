import React, { useState } from 'react'
import { X, RefreshCw } from 'lucide-react'
import { api } from '../api.js'

// The resubmit loop. Shown when a ticket fails User Review (or comes back as
// Needs Info / Stalled): the tester says what's still wrong, optionally edits
// the ask, sets priority, and sends it back to the queue. The reason + edits
// are recorded on the timeline and the iteration count bumps.
export default function AmendModal({ ticket, meta, onClose, onDone }) {
  const [reason, setReason] = useState('')
  const [priority, setPriority] = useState(ticket.priority)
  const [description, setDescription] = useState(ticket.description || '')
  const [acceptance, setAcceptance] = useState(ticket.acceptance_criteria || '')
  const [instructions, setInstructions] = useState(ticket.test_instructions || '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!reason.trim()) { setErr('Please say what still needs doing.'); return }
    setBusy(true); setErr('')
    try {
      await api.resubmit(ticket.id, {
        reason: reason.trim(),
        priority,
        description,
        acceptance_criteria: acceptance,
        test_instructions: instructions,
      })
      onDone()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[60] p-4" onClick={onClose}>
      <form onClick={(e) => e.stopPropagation()} onSubmit={submit}
        className="w-full max-w-lg bg-white rounded-xl shadow-lg p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
            <RefreshCw className="w-4 h-4 text-amber-600" /> Resubmit {ticket.ref}
          </h2>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>
        <p className="text-xs text-slate-500 mb-4">
          Send this back to the queue with what still needs doing. This will be iteration #{(ticket.iteration || 0) + 1}.
        </p>

        <label className="block text-xs font-medium text-slate-600 mb-1">
          What's still wrong / what changed? <span className="text-rose-500">*</span>
        </label>
        <textarea
          className="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm h-20 focus:outline-none focus:ring-2 focus:ring-amber-300"
          value={reason} onChange={(e) => setReason(e.target.value)} autoFocus
          placeholder="e.g. The clipping is fixed on A4 but still cuts off on Letter size"
        />

        <label className="block text-xs font-medium text-slate-600 mb-1">Priority</label>
        <select className="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
          value={priority} onChange={(e) => setPriority(e.target.value)}>
          {meta.priorities.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>

        <details className="mb-3">
          <summary className="text-xs font-medium text-slate-600 cursor-pointer select-none">
            Edit the ask (description / acceptance / test steps)
          </summary>
          <div className="mt-2 space-y-3">
            <div>
              <label className="block text-xs text-slate-500 mb-1">Description</label>
              <textarea className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm h-20"
                value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Acceptance criteria</label>
              <textarea className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm h-16"
                value={acceptance} onChange={(e) => setAcceptance(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">How to test</label>
              <textarea className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm h-16"
                value={instructions} onChange={(e) => setInstructions(e.target.value)} />
            </div>
          </div>
        </details>

        {err && <div className="mb-3 text-sm text-red-600">{err}</div>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800">
            Cancel
          </button>
          <button type="submit" disabled={busy || !reason.trim()}
            className="px-4 py-2 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium">
            {busy ? 'Resubmitting…' : 'Resubmit to queue'}
          </button>
        </div>
      </form>
    </div>
  )
}
