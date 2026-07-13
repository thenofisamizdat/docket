import React, { useEffect, useState } from 'react'
import { AlertCircle, RefreshCw } from 'lucide-react'
import { api } from '../api.js'
import { fmtDuration } from '../ui.js'

// Coaching dashboard (Phase 4): make throughput, effort, and ask-quality legible
// so testers learn what good stories cost — and where vague ones bounce.
export default function Analytics() {
  const [a, setA] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    const load = () => api.analytics().then(setA).catch((e) => setErr(e.message))
    load()
    const iv = setInterval(load, 15000)
    return () => clearInterval(iv)
  }, [])

  if (err) return <div className="p-6 text-red-600 text-sm">{err}</div>
  if (!a) return <div className="p-6 text-slate-400">Loading analytics…</div>

  const clar = a.clarity.distribution
  const clarTotal = clar.low + clar.medium + clar.high

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      {/* Top metric cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Tickets" value={a.totals.total} sub={`${a.totals.done} done · ${a.totals.open} open`} />
        <Stat label="Bounce rate" value={`${a.quality.bounce_rate}%`} sub={`${a.quality.bounced_tickets} needed clarification`} tone={a.quality.bounce_rate > 25 ? 'warn' : 'ok'} />
        <Stat label="Avg effort / ticket" value={fmtDuration(a.effort.avg_secs) || '—'} sub={`$${a.effort.avg_cost_usd} avg`} />
        <Stat label="Total agent cost" value={`$${a.effort.total_cost_usd}`} sub={`${fmtDuration(a.effort.total_secs)} compute · ${a.quality.resubmits} resubmits`} />
      </div>

      {/* Ask clarity */}
      <Card title="Ask clarity" hint="Quality of the stories testers write (scored at submit)">
        <div className="flex items-center gap-4">
          <div className="text-3xl font-semibold text-slate-800 w-20">{a.clarity.avg ?? '—'}<span className="text-base text-slate-400">/100</span></div>
          <div className="flex-1">
            {clarTotal === 0
              ? <div className="text-sm text-slate-400 italic">No scored tickets yet — new tickets get a clarity score.</div>
              : <div className="flex h-4 rounded-full overflow-hidden">
                  <Seg n={clar.high} total={clarTotal} cls="bg-emerald-500" label="high" />
                  <Seg n={clar.medium} total={clarTotal} cls="bg-amber-400" label="medium" />
                  <Seg n={clar.low} total={clarTotal} cls="bg-rose-500" label="low" />
                </div>}
            <div className="flex gap-3 mt-1 text-[11px] text-slate-500">
              <span><span className="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1" />High {clar.high}</span>
              <span><span className="inline-block w-2 h-2 rounded-full bg-amber-400 mr-1" />Medium {clar.medium}</span>
              <span><span className="inline-block w-2 h-2 rounded-full bg-rose-500 mr-1" />Low {clar.low}</span>
            </div>
          </div>
        </div>
      </Card>

      {/* Per-tester coaching table */}
      <Card title="By tester" hint="Who writes the clearest asks, and whose bounce / iterate most">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase text-slate-400">
              <th className="py-1">Tester</th>
              <th className="py-1 text-right">Tickets</th>
              <th className="py-1 text-right">Avg clarity</th>
              <th className="py-1 text-right">Bounced</th>
              <th className="py-1 text-right">Resubmits</th>
            </tr>
          </thead>
          <tbody>
            {a.per_author.map((r) => (
              <tr key={r.author} className="border-t border-slate-100">
                <td className="py-1.5 font-medium text-slate-700">{r.author}</td>
                <td className="py-1.5 text-right">{r.tickets}</td>
                <td className="py-1.5 text-right">{r.avg_clarity ?? '—'}</td>
                <td className={`py-1.5 text-right ${r.bounced ? 'text-rose-600' : 'text-slate-400'}`}>{r.bounced}</td>
                <td className={`py-1.5 text-right ${r.iterations ? 'text-amber-600' : 'text-slate-400'}`}>{r.iterations}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {/* Recently bounced & why */}
      <Card title="Bounced & why" hint="Where asks needed clarification or failed review — the coaching gold">
        {a.recently_bounced.length === 0
          ? <div className="text-sm text-slate-400 italic">Nothing bounced yet.</div>
          : <ul className="space-y-2">
              {a.recently_bounced.map((b, i) => (
                <li key={i} className="flex gap-2 text-sm">
                  <AlertCircle className={`w-4 h-4 mt-0.5 shrink-0 ${b.kind === 'needs_info' ? 'text-rose-500' : 'text-amber-500'}`} />
                  <div>
                    <span className="font-mono text-[11px] text-slate-400 mr-1">{b.ref}</span>
                    <span className="text-slate-700">{b.title}</span>
                    <span className="ml-1 text-[10px] uppercase text-slate-400">{b.kind === 'needs_info' ? 'needs info' : 'resubmit'}</span>
                    <div className="text-xs text-slate-500">{b.reason}</div>
                  </div>
                </li>
              ))}
            </ul>}
      </Card>
    </div>
  )
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className={`rounded-xl border p-3 ${tone === 'warn' ? 'border-rose-200 bg-rose-50' : 'border-slate-200 bg-white'}`}>
      <div className="text-[11px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className="text-2xl font-semibold text-slate-800">{value}</div>
      {sub && <div className="text-[11px] text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

function Card({ title, hint, children }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
        {hint && <p className="text-[11px] text-slate-400">{hint}</p>}
      </div>
      {children}
    </div>
  )
}

function Seg({ n, total, cls, label }) {
  if (!n) return null
  return <div className={cls} style={{ width: `${(n / total) * 100}%` }} title={`${label}: ${n}`} />
}
