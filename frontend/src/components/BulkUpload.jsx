import React, { useState } from 'react'
import { X, Upload } from 'lucide-react'
import { api } from '../api.js'

// Bulk-create tickets from pasted CSV/JSON or an uploaded .csv/.json file.
// CSV header row names the fields: title,type,description,acceptance_criteria,priority.

const FIELDS = ['title', 'type', 'description', 'acceptance_criteria', 'priority']

function parseCSV(text) {
  const rows = []
  let field = '', row = [], inQ = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inQ) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++ }
      else if (c === '"') inQ = false
      else field += c
    } else if (c === '"') inQ = true
    else if (c === ',') { row.push(field); field = '' }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++
      if (field !== '' || row.length) { row.push(field); rows.push(row); row = []; field = '' }
    } else field += c
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row) }
  if (!rows.length) return []
  const header = rows[0].map((h) => h.trim().toLowerCase())
  return rows.slice(1).filter((r) => r.some((c) => c.trim())).map((r) => {
    const o = {}
    header.forEach((h, i) => { if (FIELDS.includes(h)) o[h] = (r[i] || '').trim() })
    return o
  })
}

function parseInput(text) {
  const t = text.trim()
  if (!t) return []
  if (t[0] === '[' || t[0] === '{') {
    const j = JSON.parse(t)
    return Array.isArray(j) ? j : [j]
  }
  return parseCSV(t)
}

export default function BulkUpload({ onClose, onDone }) {
  const [text, setText] = useState('')
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  function preview(v) {
    setText(v); setErr(''); setResult(null)
    try { setRows(v.trim() ? parseInput(v) : null) }
    catch (e) { setRows(null); setErr('Could not parse: ' + e.message) }
  }
  function onFile(e) {
    const f = e.target.files?.[0]
    if (!f) return
    const r = new FileReader()
    r.onload = () => preview(String(r.result))
    r.readAsText(f)
  }
  async function submit() {
    if (!rows || !rows.length) return
    setBusy(true); setErr('')
    try {
      const r = await api.bulk(rows.map((x) => ({
        title: x.title || '', type: x.type || 'feature', description: x.description || '',
        acceptance_criteria: x.acceptance_criteria || '', priority: x.priority || 'P2',
      })))
      setResult(r); onDone && onDone()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="w-full max-w-2xl bg-white rounded-xl shadow-lg p-6 max-h-[90vh] overflow-auto">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2"><Upload className="w-5 h-5 text-indigo-600" /> Bulk add tickets</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X className="w-5 h-5" /></button>
        </div>

        {result ? (
          <div className="space-y-3">
            <div className="text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg p-3">
              Created {result.count} ticket{result.count === 1 ? '' : 's'}: {result.created.map((c) => c.ref).join(', ')}
            </div>
            {result.errors.length > 0 && (
              <div className="text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-lg p-3">
                {result.errors.length} row(s) skipped: {result.errors.map((e) => `row ${e.row} (${e.error})`).join('; ')}
              </div>)}
            <button onClick={onClose} className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium">Done</button>
          </div>
        ) : (
          <>
            <p className="text-xs text-slate-500 mb-2">
              Paste a <b>JSON array</b> of tickets, or <b>CSV</b> with a header row
              (<code>title,type,description,acceptance_criteria,priority</code>), or upload a .csv/.json file.
            </p>
            <textarea value={text} onChange={(e) => preview(e.target.value)} rows={7}
              placeholder={'title,type,priority\nAdd search,feature,P2\nFix crash on save,bug,P1'}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-300" />
            <div className="flex items-center gap-3 mt-2">
              <label className="text-xs text-indigo-600 hover:underline cursor-pointer">
                upload a file<input type="file" accept=".csv,.json,text/csv,application/json" onChange={onFile} className="hidden" />
              </label>
              {rows && <span className="text-xs text-slate-500">{rows.length} ticket(s) parsed</span>}
            </div>
            {err && <div className="text-sm text-rose-600 mt-2">{err}</div>}
            {rows && rows.length > 0 && (
              <div className="mt-3 border border-slate-200 rounded-lg overflow-auto max-h-48">
                <table className="w-full text-xs">
                  <thead className="bg-slate-50 text-slate-400 uppercase text-[10px]"><tr><th className="text-left px-2 py-1">Title</th><th className="text-left px-2 py-1">Type</th><th className="text-left px-2 py-1">Priority</th></tr></thead>
                  <tbody>
                    {rows.slice(0, 20).map((r, i) => (
                      <tr key={i} className="border-t border-slate-100">
                        <td className="px-2 py-1 text-slate-700">{r.title || <span className="text-rose-500 italic">missing title</span>}</td>
                        <td className="px-2 py-1 text-slate-500">{r.type || 'feature'}</td>
                        <td className="px-2 py-1 text-slate-500">{r.priority || 'P2'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {rows.length > 20 && <div className="text-[11px] text-slate-400 px-2 py-1">…and {rows.length - 20} more</div>}
              </div>)}
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800">Cancel</button>
              <button onClick={submit} disabled={busy || !rows || !rows.length}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium">
                Create {rows?.length || 0} ticket{rows?.length === 1 ? '' : 's'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
