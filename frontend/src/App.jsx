import React, { useEffect, useState, useCallback } from 'react'
import { ClipboardList, Plus, LogOut, RefreshCw, LayoutGrid, ListChecks, BarChart3, Users, HelpCircle, CalendarRange, Rocket } from 'lucide-react'
import { api, getToken, getName, clearSession } from './api.js'
import Login from './components/Login.jsx'
import Board from './components/Board.jsx'
import Checklist from './components/Checklist.jsx'
import Analytics from './components/Analytics.jsx'
import Profiles from './components/Profiles.jsx'
import Help from './components/Help.jsx'
import TicketDetail from './components/TicketDetail.jsx'
import NewTicketModal from './components/NewTicketModal.jsx'

export default function App() {
  const [authed, setAuthed] = useState(!!getToken())
  const [name, setName] = useState(getName())
  const [view, setView] = useState('board') // 'board' | 'checklist'
  const [meta, setMeta] = useState(null)
  const [tickets, setTickets] = useState([])
  const [statusMeta, setStatusMeta] = useState({})
  const [openId, setOpenId] = useState(null)
  const [showNew, setShowNew] = useState(false)
  const [newPrefill, setNewPrefill] = useState(null)
  const [err, setErr] = useState('')

  // Drop to login on any 401 from the API client.
  useEffect(() => {
    const onUnauth = () => { setAuthed(false); setName('') }
    window.addEventListener('docket-unauth', onUnauth)
    return () => window.removeEventListener('docket-unauth', onUnauth)
  }, [])

  const loadBoard = useCallback(async () => {
    try {
      const r = await api.board()
      setTickets(r.tickets)
      setStatusMeta(r.status_meta)
      setErr('')
    } catch (e) {
      setErr(e.message)
    }
  }, [])

  // Load vocabulary once, then poll the board for live movement (board view only).
  useEffect(() => {
    if (!authed) return
    let alive = true
    api.meta().then((m) => { if (alive) setMeta(m) }).catch((e) => setErr(e.message))
    loadBoard()
    const iv = setInterval(() => { if (view === 'board') loadBoard() }, 4000)
    return () => { alive = false; clearInterval(iv) }
  }, [authed, loadBoard, view])

  function openNewTicket(prefill = null) {
    setNewPrefill(prefill)
    setShowNew(true)
  }

  if (!authed) {
    return <Login onAuthed={(n) => { setName(n); setAuthed(true) }} />
  }

  const tab = (key, label, Icon) => (
    <button onClick={() => setView(key)}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium ${
        view === key ? 'bg-slate-100 text-slate-800' : 'text-slate-500 hover:text-slate-700'}`}>
      <Icon className="w-4 h-4" /> {label}
    </button>
  )

  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-white border-b border-slate-200 px-4 py-2.5 flex items-center gap-3">
        <ClipboardList className="w-5 h-5 text-indigo-600" />
        <span className="font-semibold text-slate-800">Docket</span>
        <nav className="flex items-center gap-1 ml-3">
          {tab('board', 'Board', LayoutGrid)}
          {/* Checklist is the host-specific QA catalogue; hidden in the portable build. */}
          {import.meta.env.VITE_DOCKET_PORTABLE !== '1' && tab('checklist', 'Checklist', ListChecks)}
          {tab('analytics', 'Analytics', BarChart3)}
          {tab('profiles', 'Profiles', Users)}
          {tab('help', 'Help', HelpCircle)}
          {/* Roadmap + Build are separate self-contained pages, not SPA views. */}
          <a href="/roadmap" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-slate-500 hover:text-slate-700">
            <CalendarRange className="w-4 h-4" /> Roadmap
          </a>
          <a href="/build" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-slate-500 hover:text-slate-700">
            <Rocket className="w-4 h-4" /> Build
          </a>
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <button onClick={() => openNewTicket()}
            className="flex items-center gap-1 px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm font-medium">
            <Plus className="w-4 h-4" /> New ticket
          </button>
          <span className="text-sm text-slate-500 px-2">{name}</span>
          <button onClick={() => { clearSession(); setAuthed(false) }}
            className="text-slate-400 hover:text-slate-600" title="Sign out">
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </header>

      {err && (
        <div className="bg-red-50 text-red-700 text-sm px-4 py-1.5 flex items-center gap-2">
          <RefreshCw className="w-3.5 h-3.5" /> {err}
        </div>
      )}

      <main className="flex-1 overflow-auto">
        {!meta ? (
          <div className="p-8 text-slate-400">Loading…</div>
        ) : view === 'board' ? (
          <Board tickets={tickets} statusMeta={statusMeta} onOpen={setOpenId} />
        ) : view === 'checklist' ? (
          <Checklist onRaiseTicket={openNewTicket} />
        ) : view === 'analytics' ? (
          <Analytics />
        ) : view === 'profiles' ? (
          <Profiles onOpenTicket={setOpenId} />
        ) : (
          <Help />
        )}
      </main>

      {openId != null && meta && (
        <TicketDetail
          ticketId={openId} meta={meta}
          onClose={() => setOpenId(null)}
          onChanged={loadBoard}
        />
      )}

      {showNew && meta && (
        <NewTicketModal
          meta={meta} prefill={newPrefill}
          onClose={() => { setShowNew(false); setNewPrefill(null) }}
          onCreated={(t) => { setShowNew(false); setNewPrefill(null); loadBoard(); setView('board'); setOpenId(t.id) }}
        />
      )}
    </div>
  )
}
