import React, { useState } from 'react'
import { ClipboardList } from 'lucide-react'
import { api, setSession } from '../api.js'

export default function Login({ onAuthed }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const r = await api.login(username, password)
      setSession(r.token, r.name)
      onAuthed(r.name)
    } catch (e) {
      setErr(e.message || 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 p-4">
      <form onSubmit={submit} className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-slate-200 p-8">
        <div className="flex items-center gap-2 mb-1">
          <ClipboardList className="w-6 h-6 text-indigo-600" />
          <h1 className="text-2xl font-semibold text-slate-800">Docket</h1>
        </div>
        <p className="text-sm text-slate-500 mb-6">From ask to merge — in the open.</p>

        <label className="block text-xs font-medium text-slate-600 mb-1">Username</label>
        <input
          className="w-full mb-4 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"
          value={username} onChange={(e) => setUsername(e.target.value)} autoFocus
        />
        <label className="block text-xs font-medium text-slate-600 mb-1">Password</label>
        <input
          type="password"
          className="w-full mb-5 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"
          value={password} onChange={(e) => setPassword(e.target.value)}
        />
        {err && <div className="mb-4 text-sm text-red-600">{err}</div>}
        <button
          type="submit" disabled={busy}
          className="w-full py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
