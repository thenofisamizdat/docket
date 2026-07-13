import React, { useState, useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { api } from '../api.js'

const LEVEL_TEXT = { high: 'text-emerald-600', medium: 'text-amber-600', low: 'text-rose-600' }
const LEVEL_BAR = { high: 'bg-emerald-500', medium: 'bg-amber-400', low: 'bg-rose-500' }

// Raise a new ticket. Acceptance criteria is a first-class field on purpose:
// it's the quiet lever for "write better stories" — what 'done' looks like.
export default function NewTicketModal({ meta, onClose, onCreated, prefill }) {
  const [title, setTitle] = useState(prefill?.title || '')
  const [type, setType] = useState(prefill?.type || 'feature')
  const [priority, setPriority] = useState(prefill?.priority || meta.default_priority || 'P2')
  const [description, setDescription] = useState(prefill?.description || '')
  const [acceptance, setAcceptance] = useState(prefill?.acceptance_criteria || '')
  const [clarity, setClarity] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const descRef = useRef(null)

  // Live clarity meter — debounced score of the in-progress ask.
  useEffect(() => {
    if (!title.trim() && !description.trim() && !acceptance.trim()) { setClarity(null); return }
    const id = setTimeout(() => {
      api.clarity({ title, description, acceptance_criteria: acceptance, type })
        .then(setClarity).catch(() => {})
    }, 400)
    return () => clearTimeout(id)
  }, [title, description, acceptance, type])

  async function submit(e) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const r = await api.create({
        title, type, priority,
        description, acceptance_criteria: acceptance,
      })
      onCreated(r.ticket)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
        className="w-full max-w-lg bg-white rounded-xl shadow-lg p-6"
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-800">New ticket</h2>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        <label className="block text-xs font-medium text-slate-600 mb-1">Title</label>
        <input
          className="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"
          value={title} onChange={(e) => setTitle(e.target.value)} autoFocus
          placeholder="Short summary of the ask"
          onKeyDown={(e) => {
            // Enter mid-title must not submit a half-typed ticket (a real tester
            // accident in the old hub) — hop to the description instead.
            if (e.key === 'Enter') { e.preventDefault(); descRef.current?.focus() }
          }}
        />

        <div className="flex gap-3 mb-3">
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-600 mb-1">Type</label>
            <select className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
              value={type} onChange={(e) => setType(e.target.value)}>
              {meta.types.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-600 mb-1">Priority</label>
            <select className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
              value={priority} onChange={(e) => setPriority(e.target.value)}>
              {meta.priorities.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
        </div>

        <label className="block text-xs font-medium text-slate-600 mb-1">Description</label>
        <textarea
          ref={descRef}
          className="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm h-24 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          value={description} onChange={(e) => setDescription(e.target.value)}
          placeholder="What's the problem / ask? Why does it matter?"
        />

        <label className="block text-xs font-medium text-slate-600 mb-1">
          Acceptance criteria <span className="text-slate-400">— what does “done” look like?</span>
        </label>
        <textarea
          className="w-full mb-4 px-3 py-2 border border-slate-300 rounded-lg text-sm h-20 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          value={acceptance} onChange={(e) => setAcceptance(e.target.value)}
          placeholder="e.g. The PDF prints full-width with no clipping on A4 and Letter"
        />

        {clarity && (
          <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-xs font-medium text-slate-600">Ask clarity</span>
              <div className="flex-1 h-2 rounded-full bg-slate-200 overflow-hidden">
                <div className={`h-full ${LEVEL_BAR[clarity.level]}`} style={{ width: `${clarity.score}%` }} />
              </div>
              <span className={`text-xs font-semibold tabular-nums ${LEVEL_TEXT[clarity.level]}`}>
                {clarity.score}/100
              </span>
            </div>
            {clarity.suggestions.length > 0 && (
              <ul className="text-[11px] text-slate-500 space-y-0.5 list-disc list-inside">
                {clarity.suggestions.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            )}
          </div>
        )}

        {err && <div className="mb-3 text-sm text-red-600">{err}</div>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800">
            Cancel
          </button>
          <button type="submit" disabled={busy || !title.trim()}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium">
            {busy ? 'Creating…' : 'Create ticket'}
          </button>
        </div>
      </form>
    </div>
  )
}
