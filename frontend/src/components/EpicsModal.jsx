import React, { useEffect, useState } from 'react'
import { X, Trash2 } from 'lucide-react'
import { api } from '../api.js'

// Manage epics: color-coded groupings tickets link to (e.g. "Cellebrite",
// "Financial"). Create with a name + palette color; deleting an epic only
// unlinks its tickets.
export default function EpicsModal({ onClose, onChanged }) {
  const [epics, setEpics] = useState([])
  const [palette, setPalette] = useState([])
  const [name, setName] = useState('')
  const [color, setColor] = useState('')
  const [description, setDescription] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = () => api.epics()
    .then((r) => { setEpics(r.epics || []); setPalette(r.palette || []) })
    .catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  async function create(e) {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true); setErr('')
    try {
      await api.createEpic({ name, color, description })
      setName(''); setColor(''); setDescription('')
      load(); onChanged && onChanged()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function remove(ep) {
    if (!confirm(`Delete epic “${ep.name}”?\n\nIts ${ep.ticket_count || 0} ticket(s) are kept — they just lose the epic link.`)) return
    try { await api.deleteEpic(ep.id); load(); onChanged && onChanged() }
    catch (e) { setErr(e.message) }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="w-full max-w-lg bg-white rounded-xl shadow-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-800">Epics</h2>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="mb-5 max-h-56 overflow-y-auto space-y-1.5">
          {epics.length === 0 && <div className="text-sm text-slate-400 italic">No epics yet — create the first one below.</div>}
          {epics.map((ep) => (
            <div key={ep.id} className="flex items-center gap-2.5 rounded-lg border border-slate-200 px-3 py-2">
              <span className="w-3 h-3 rounded-full shrink-0" style={{ background: ep.color }} />
              <span className="text-sm font-medium text-slate-700">{ep.name}</span>
              <span className="text-[11px] text-slate-400">
                {ep.ticket_count || 0} tickets{ep.done_count ? ` · ${ep.done_count} done` : ''}
              </span>
              <button onClick={() => remove(ep)} title="Delete epic (tickets are kept)"
                className="ml-auto text-slate-300 hover:text-rose-500">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>

        <form onSubmit={create} className="border-t border-slate-200 pt-4">
          <label className="block text-xs font-medium text-slate-600 mb-1">New epic</label>
          <input
            className="w-full mb-2 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"
            value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Cellebrite, Financial" autoFocus />
          <input
            className="w-full mb-2 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"
            value={description} onChange={(e) => setDescription(e.target.value)}
            placeholder="Description (optional)" />
          <div className="flex items-center gap-1.5 mb-3">
            <span className="text-xs text-slate-500 mr-1">Color</span>
            {palette.map((c) => (
              <button key={c} type="button" onClick={() => setColor(color === c ? '' : c)}
                className={`w-5 h-5 rounded-full border-2 ${color === c ? 'border-slate-700 scale-110' : 'border-transparent'}`}
                style={{ background: c }} title={c} />
            ))}
            <span className="text-[11px] text-slate-400 ml-1">{color ? color : 'auto'}</span>
          </div>
          {err && <div className="mb-2 text-sm text-red-600">{err}</div>}
          <div className="flex justify-end">
            <button type="submit" disabled={busy || !name.trim()}
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium">
              {busy ? 'Creating…' : 'Create epic'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
