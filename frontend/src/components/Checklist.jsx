import React, { useEffect, useMemo, useState, useCallback } from 'react'
import {
  Check, X as XIcon, Ban, ChevronRight, Plus, MessageSquare, UserCircle2, Send,
} from 'lucide-react'
import { api, getName } from '../api.js'
import { relTime } from '../ui.js'

// The checklist: a catalogue of shipped behaviours where each tester records
// pass / fail / blocked + a note. Distinct from the ticket pipeline — this is
// regression-style verification. A failing item can be turned straight into a
// Docket ticket via onRaiseTicket.
//
// Layout principles (2026-06-11 refresh): the catalogue's own content does the
// teaching — the feature blurb ("what"), the how-to-test recipe, and the
// data/ui tag are all visible by default instead of hidden behind clicks.
// Lightweight per-item assignment + a small discussion thread support triage.

const VERDICTS = [
  { key: 'pass', label: 'Pass', icon: Check, on: 'bg-emerald-600 text-white border-emerald-600', off: 'text-emerald-700 border-emerald-300 hover:bg-emerald-50' },
  { key: 'fail', label: 'Fail', icon: XIcon, on: 'bg-rose-600 text-white border-rose-600', off: 'text-rose-700 border-rose-300 hover:bg-rose-50' },
  { key: 'blocked', label: 'Blocked', icon: Ban, on: 'bg-amber-500 text-white border-amber-500', off: 'text-amber-700 border-amber-300 hover:bg-amber-50' },
]
const DOT = { pass: 'bg-emerald-500', fail: 'bg-rose-500', blocked: 'bg-amber-500' }
const EDGE = { pass: 'border-l-emerald-400', fail: 'border-l-rose-400', blocked: 'border-l-amber-400' }

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'todo', label: 'Needs my verdict' },
  { key: 'failing', label: 'Failing' },
  { key: 'mine', label: 'Mine' },
]

export default function Checklist({ onRaiseTicket }) {
  const me = getName()
  const meUser = (me || '').toLowerCase()
  const [sections, setSections] = useState([])
  const [feedback, setFeedback] = useState({ items: {}, assignments: {}, item_comments: {} })
  const [testers, setTesters] = useState([])
  const [filter, setFilter] = useState('all')
  const [err, setErr] = useState('')
  const [open, setOpen] = useState({}) // section heading -> expanded

  const loadFeedback = useCallback(async () => {
    try {
      const d = await api.feedback()
      setFeedback({ items: d.items || {}, assignments: d.assignments || {}, item_comments: d.item_comments || {} })
    } catch (e) { setErr(e.message) }
  }, [])

  useEffect(() => {
    api.checklist().then((d) => {
      setSections(d.sections || [])
      setOpen(Object.fromEntries((d.sections || []).map((s) => [s.h, true])))
    }).catch((e) => setErr(e.message))
    api.testers().then((d) => setTesters(d.testers || [])).catch(() => {})
    loadFeedback()
  }, [loadFeedback])

  async function setVerdict(itemId, status, currentNote) {
    try {
      await api.postFeedback(itemId, status, currentNote ?? null)
      await loadFeedback()
    } catch (e) { setErr(e.message) }
  }

  async function saveNote(itemId, note, currentStatus) {
    try {
      await api.postFeedback(itemId, currentStatus ?? null, note)
      await loadFeedback()
    } catch (e) { setErr(e.message) }
  }

  async function assign(itemId, assignee) {
    try {
      await api.assignItem(itemId, assignee)
      await loadFeedback()
    } catch (e) { setErr(e.message) }
  }

  async function comment(itemId, text) {
    try {
      await api.itemComment(itemId, text)
      await loadFeedback()
    } catch (e) { setErr(e.message) }
  }

  // ---- filtering ----
  function itemMatches(it) {
    const byTester = feedback.items[it.id] || {}
    const mine = byTester[me] || {}
    switch (filter) {
      case 'todo':    return !mine.status
      case 'failing': return Object.values(byTester).some((r) => r.status === 'fail')
      case 'mine':    return (feedback.assignments[it.id] || '') === meUser
      default:        return true
    }
  }

  const counts = useMemo(() => {
    const c = { all: 0, todo: 0, failing: 0, mine: 0 }
    for (const sec of sections) {
      for (const it of sec.items) {
        const byTester = feedback.items[it.id] || {}
        const mine = byTester[me] || {}
        c.all++
        if (!mine.status) c.todo++
        if (Object.values(byTester).some((r) => r.status === 'fail')) c.failing++
        if ((feedback.assignments[it.id] || '') === meUser) c.mine++
      }
    }
    return c
  }, [sections, feedback, me, meUser])

  function sectionCount(sec) {
    let passed = 0
    for (const it of sec.items) {
      const byTester = feedback.items[it.id] || {}
      if (Object.values(byTester).some((r) => r.status === 'pass')) passed++
    }
    return { passed, total: sec.items.length }
  }

  return (
    <div className="max-w-4xl mx-auto p-4 pb-12">
      {err && <div className="mb-3 text-sm text-red-600">{err}</div>}

      <div className="flex flex-wrap items-center gap-2 mb-1">
        <p className="text-sm text-slate-500 mr-auto">
          Verify each shipped behaviour and record <strong>pass / fail / blocked</strong>.
          Found a problem? Turn it straight into a ticket.
        </p>
        <div className="flex items-center gap-1">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => setFilter(f.key)}
              className={`px-2.5 py-1 text-xs rounded-full border ${
                filter === f.key
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'text-slate-600 border-slate-300 hover:bg-slate-50'}`}>
              {f.label} <span className={filter === f.key ? 'text-indigo-200' : 'text-slate-400'}>{counts[f.key]}</span>
            </button>
          ))}
        </div>
      </div>
      <p className="text-[11px] text-slate-400 mb-4">
        <span className="font-medium text-violet-600">test properly</span> = new behaviour, exercise it for real ·{' '}
        <span className="font-medium text-sky-600">visual check</span> = logic already verified, confirm it looks right
      </p>

      {sections.map((sec) => {
        const items = sec.items.filter(itemMatches)
        if (filter !== 'all' && items.length === 0) return null
        const { passed, total } = sectionCount(sec)
        const isOpen = open[sec.h]
        return (
          <div key={sec.h} className="mb-3 bg-white rounded-xl border border-slate-200 overflow-hidden">
            <button
              onClick={() => setOpen((o) => ({ ...o, [sec.h]: !o[sec.h] }))}
              className="w-full px-4 py-3 text-left hover:bg-slate-50"
            >
              <div className="flex items-center gap-2">
                <ChevronRight className={`w-4 h-4 text-slate-400 transition-transform shrink-0 ${isOpen ? 'rotate-90' : ''}`} />
                <span className="font-semibold text-slate-800">{sec.h}</span>
                <div className="ml-auto flex items-center gap-2 shrink-0">
                  <div className="w-20 h-1.5 rounded-full bg-slate-100 overflow-hidden">
                    <div className="h-full bg-emerald-400" style={{ width: total ? `${(passed / total) * 100}%` : 0 }} />
                  </div>
                  <span className="text-xs text-slate-400">{passed}/{total}</span>
                </div>
              </div>
              {sec.d && <p className="mt-1 ml-6 text-xs text-slate-500">{sec.d}</p>}
            </button>

            {isOpen && (
              <div className="divide-y divide-slate-100">
                {items.map((it) => (
                  <ChecklistItem
                    key={it.id} item={it} me={me} testers={testers}
                    byTester={feedback.items[it.id] || {}}
                    assignee={feedback.assignments[it.id] || ''}
                    comments={feedback.item_comments[it.id] || []}
                    onVerdict={(s, note) => setVerdict(it.id, s, note)}
                    onNote={(n, status) => saveNote(it.id, n, status)}
                    onAssign={(a) => assign(it.id, a)}
                    onComment={(t) => comment(it.id, t)}
                    onRaiseTicket={() => onRaiseTicket({
                      title: it.t, type: 'bug',
                      description: `From checklist item "${it.id}" (${sec.h}).\n\nExpected behaviour: ${it.what || it.t}\n\nHow to reproduce: ${it.how || ''}`,
                    })}
                  />
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ChecklistItem({ item, me, testers, byTester, assignee, comments,
                         onVerdict, onNote, onAssign, onComment, onRaiseTicket }) {
  const mine = byTester[me] || {}
  const [note, setNote] = useState(mine.note || '')
  const [showThread, setShowThread] = useState(false)
  const [draft, setDraft] = useState('')
  useEffect(() => { setNote(mine.note || '') }, [mine.note])

  const others = Object.entries(byTester).filter(([, r]) => r.status)
  const assigneeName = testers.find((t) => t.username === assignee)?.name || assignee

  return (
    <div className={`px-4 py-3 border-l-2 ${EDGE[mine.status] || 'border-l-transparent'}`}>
      {/* title row */}
      <div className="flex items-start gap-2 flex-wrap">
        <div className="text-sm text-slate-800 font-medium">{item.t}</div>
        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded mt-0.5 ${
          item.tag === 'ui' ? 'bg-violet-100 text-violet-700' : 'bg-sky-100 text-sky-700'}`}>
          {item.tag === 'ui' ? 'test properly' : 'visual check'}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {others.map(([name, r]) => (
            <span key={name} title={`${name}: ${r.status}${r.note ? ' — ' + r.note : ''}`}
              className="flex items-center gap-0.5 text-[10px] text-slate-500">
              <span className={`w-2 h-2 rounded-full ${DOT[r.status] || 'bg-slate-300'}`} />
              {name[0]}
            </span>
          ))}
        </div>
      </div>

      {/* what the feature is + how to test it — visible by default */}
      {item.what && <p className="mt-1 text-xs text-slate-600">{item.what}</p>}
      {item.how && (
        <p className="mt-1.5 text-xs text-slate-500 bg-slate-50 border border-slate-100 rounded px-2 py-1.5 whitespace-pre-wrap">
          <span className="font-semibold text-slate-400 uppercase text-[10px] mr-1">How to test</span>
          {item.how}
        </p>
      )}
      {item.note && (
        <p className="mt-1 text-[11px] text-amber-700"><span className="font-semibold">Caveat:</span> {item.note}</p>
      )}

      {/* verdicts + assignment + raise ticket */}
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        {VERDICTS.map((v) => {
          const Icon = v.icon
          const active = mine.status === v.key
          return (
            <button key={v.key} onClick={() => onVerdict(active ? '' : v.key, mine.note)}
              className={`flex items-center gap-1 px-2 py-1 text-xs rounded-lg border ${active ? v.on : v.off}`}>
              <Icon className="w-3 h-3" /> {v.label}
            </button>
          )
        })}

        <div className="ml-auto flex items-center gap-1.5">
          <span className="flex items-center gap-1 text-[11px] text-slate-400">
            <UserCircle2 className="w-3.5 h-3.5" />
            <select
              value={assignee}
              onChange={(e) => onAssign(e.target.value)}
              className={`text-[11px] border rounded px-1 py-0.5 bg-white ${assignee ? 'border-indigo-300 text-indigo-700' : 'border-slate-200 text-slate-400'}`}
              title={assignee ? `Assigned to ${assigneeName}` : 'Unassigned — anyone can test this'}
            >
              <option value="">anyone</option>
              {testers.map((t) => <option key={t.username} value={t.username}>{t.name}</option>)}
            </select>
          </span>
          <button onClick={() => setShowThread((v) => !v)}
            className={`flex items-center gap-1 px-2 py-1 text-xs rounded-lg border ${
              comments.length ? 'border-indigo-300 text-indigo-700 hover:bg-indigo-50' : 'border-slate-300 text-slate-500 hover:bg-slate-50'}`}>
            <MessageSquare className="w-3 h-3" /> {comments.length || ''}
          </button>
          <button onClick={onRaiseTicket}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">
            <Plus className="w-3 h-3" /> Raise ticket
          </button>
        </div>
      </div>

      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        onBlur={() => { if (note !== (mine.note || '')) onNote(note, mine.status) }}
        placeholder="Add a note (optional)…"
        className="w-full mt-2 px-2 py-1 text-xs border border-slate-200 rounded focus:outline-none focus:ring-1 focus:ring-indigo-300"
      />

      {/* discussion thread — triage talk; decisions about fixes belong on a ticket */}
      {showThread && (
        <div className="mt-2 rounded-lg border border-slate-200 bg-slate-50 p-2">
          {comments.length === 0 && (
            <p className="text-[11px] text-slate-400 italic mb-1.5">
              No discussion yet. Use this for "is this actually broken?" — once it's a real problem, raise a ticket.
            </p>
          )}
          {comments.map((c, i) => (
            <div key={i} className="mb-1.5 text-xs">
              <span className="font-semibold text-slate-700">{c.author}</span>
              <span className="text-slate-400 ml-1.5 text-[10px]">{relTime(c.created_at)}</span>
              <div className="text-slate-600 whitespace-pre-wrap">{c.text}</div>
            </div>
          ))}
          <form
            onSubmit={(e) => { e.preventDefault(); if (draft.trim()) { onComment(draft.trim()); setDraft('') } }}
            className="flex gap-1.5 mt-1"
          >
            <input
              value={draft} onChange={(e) => setDraft(e.target.value)}
              placeholder="Reply…"
              className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-indigo-300"
            />
            <button type="submit" disabled={!draft.trim()}
              className="px-2 py-1 text-xs rounded bg-indigo-600 text-white disabled:opacity-40">
              <Send className="w-3 h-3" />
            </button>
          </form>
        </div>
      )}
    </div>
  )
}
