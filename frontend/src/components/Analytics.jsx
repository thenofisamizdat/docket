import React, { useEffect, useMemo, useState } from 'react'
import { AlertCircle, ChevronDown, ChevronRight, Columns2 } from 'lucide-react'
import { api } from '../api.js'
import { fmtDuration } from '../ui.js'

// Analytics — a sliceable dashboard. The backend returns a per-ticket dataset
// (`a.tickets`); everything here filters/aggregates/compares it CLIENT-SIDE, so
// any filter or side-by-side is instant.

const isDone = (r) => r.status === 'done' || r.roadmap_status === 'done'

const EMPTY_FILTER = { type: 'all', status: 'all', assignee: 'all', source: 'all', days: 'all' }

function applyFilters(rows, f) {
  return rows.filter((r) => {
    if (f.type !== 'all' && r.type !== f.type) return false
    if (f.source === 'automated' && !r.is_automated) return false
    if (f.source === 'manual' && r.is_automated) return false
    if (f.status === 'open' && isDone(r)) return false
    if (f.status === 'done' && !isDone(r)) return false
    if (f.assignee !== 'all' && (r.assignee || r.created_by || '') !== f.assignee) return false
    if (f.days !== 'all' && (!r.created_at || Date.parse(r.created_at) < Date.now() - f.days * 86400000)) return false
    return true
  })
}

function agg(rows) {
  const done = rows.filter(isDone)
  const cyc = done.map((r) => r.cycle_secs).filter((x) => x != null)
  const vr = rows.filter((r) => r.verified != null)
  const sum = (k) => rows.reduce((s, r) => s + (r[k] || 0), 0)
  return {
    n: rows.length, done: done.length, open: rows.length - done.length,
    auto: rows.filter((r) => r.is_automated).length,
    man: rows.filter((r) => !r.is_automated).length,
    avgCycle: cyc.length ? cyc.reduce((a, b) => a + b, 0) / cyc.length : null,
    cost: sum('cost_usd'), agentSecs: sum('agent_secs'), humanHrs: sum('hours_done'),
    verifiedPct: vr.length ? Math.round(rows.filter((r) => r.verified === true).length / vr.length * 100) : null,
    reworkPct: rows.length ? Math.round(rows.filter((r) => r.iteration > 0).length / rows.length * 100) : 0,
  }
}

const fmtH = (secs) => (secs ? `${(secs / 3600).toFixed(1)}h` : '0h')

export default function Analytics() {
  const [a, setA] = useState(null)
  const [err, setErr] = useState('')
  const [filter, setFilter] = useState(EMPTY_FILTER)
  const [compare, setCompare] = useState(false)
  const [filterB, setFilterB] = useState({ ...EMPTY_FILTER, source: 'manual' })

  useEffect(() => {
    const load = () => api.analytics().then(setA).catch((e) => setErr(e.message))
    load()
    const iv = setInterval(load, 15000)
    return () => clearInterval(iv)
  }, [])

  const rows = a?.tickets || []
  const assignees = useMemo(() => {
    const s = new Set()
    rows.forEach((r) => { if (r.assignee) s.add(r.assignee); else if (r.created_by) s.add(r.created_by) })
    return [...s].sort()
  }, [rows])
  const filtered = useMemo(() => applyFilters(rows, filter), [rows, filter])

  if (err) return <div className="p-6 text-red-600 text-sm">{err}</div>
  if (!a) return <div className="p-6 text-slate-400">Loading analytics…</div>

  const clar = a.clarity.distribution
  const clarTotal = clar.low + clar.medium + clar.high
  const auto = filtered.filter((r) => r.is_automated)
  const man = filtered.filter((r) => !r.is_automated)

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 bg-white border border-slate-200 rounded-xl px-3 py-2 sticky top-0 z-10">
        <FilterControls f={filter} set={setFilter} assignees={assignees} />
        <span className="text-[11px] text-slate-400">{filtered.length} of {rows.length} tickets</span>
        <div className="flex-1" />
        {filter !== EMPTY_FILTER && (
          <button onClick={() => setFilter(EMPTY_FILTER)} className="text-[11px] text-slate-500 hover:text-slate-700">clear</button>)}
        <button onClick={() => setCompare((c) => !c)}
          className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium border ${compare ? 'bg-indigo-600 text-white border-indigo-600' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}`}>
          <Columns2 className="w-3.5 h-3.5" /> Compare
        </button>
      </div>

      {compare ? (
        <div className="grid md:grid-cols-2 gap-3">
          <CompareColumn label="A" f={filter} set={setFilter} assignees={assignees} rows={applyFilters(rows, filter)} />
          <CompareColumn label="B" f={filterB} set={setFilterB} assignees={assignees} rows={applyFilters(rows, filterB)} />
        </div>
      ) : (
        <>
          {/* Summary tiles (respect the filter) */}
          <StatRow rows={filtered} />

          {/* Automated vs manual */}
          <Card title="Automated vs Manual" hint="Pipeline-built tickets vs those worked by hand — effort & rates">
            <div className="grid grid-cols-3 text-sm">
              <div className="text-[11px] uppercase tracking-wide text-slate-400 py-1" />
              <div className="text-center text-xs font-semibold text-indigo-600 py-1">🤖 Automated</div>
              <div className="text-center text-xs font-semibold text-slate-600 py-1">✋ Manual</div>
              <CmpRow label="Tickets" a={auto.length} b={man.length} />
              <CmpRow label="Done" a={auto.filter(isDone).length} b={man.filter(isDone).length} />
              <CmpRow label="Avg cycle time" a={fmtCycle(agg(auto).avgCycle)} b={fmtCycle(agg(man).avgCycle)} />
              <CmpRow label="Effort" a={`${fmtH(agg(auto).agentSecs)} · $${agg(auto).cost.toFixed(2)}`} b={`${agg(man).humanHrs.toFixed(1)}h logged`} />
              <CmpRow label="Verified" a={agg(auto).verifiedPct != null ? `${agg(auto).verifiedPct}%` : '—'} b="—" />
              <CmpRow label="Rework rate" a={`${agg(auto).reworkPct}%`} b={`${agg(man).reworkPct}%`} />
            </div>
          </Card>

          {/* By type + by assignee */}
          <div className="grid md:grid-cols-2 gap-3">
            <Card title="By type">
              <HBars items={splitBy(filtered, 'type').map(([k, v]) => ({ label: k, value: v.length, hint: `${v.length}` }))} color="#6366f1" empty="No tickets." />
            </Card>
            <Card title="By assignee" hint="tickets owned (done / total)">
              <HBars items={byAssignee(filtered)} color="#4f8cff" empty="Unassigned." />
            </Card>
          </div>

          {/* Pipeline flow (global time-series) */}
          {a.pipeline && (
            <Card title="Pipeline flow" hint="Throughput, cost & stage timing across the whole pipeline" collapsible>
              <div className="grid md:grid-cols-2 gap-4">
                <Labelled t="Throughput / day"><MiniBars data={a.pipeline.throughput_by_day} color="#3fb96a" fmt={(v) => `${v} shipped`} /></Labelled>
                <Labelled t="Agent cost / day"><Spark data={a.pipeline.cost_by_day} color="#4f8cff" fmt={(v) => `$${v}`} /></Labelled>
                <Labelled t="Avg time in stage"><HBars items={a.pipeline.time_in_stage.map((s) => ({ label: s.status.replace(/_/g, ' '), value: s.avg_secs, hint: fmtDuration(s.avg_secs) }))} color="#4f8cff" /></Labelled>
                <Labelled t="Work in progress"><HBars items={Object.entries(a.pipeline.wip).filter(([, v]) => v).map(([k, v]) => ({ label: k.replace(/_/g, ' '), value: v, hint: `${v}` }))} color="#e0a83c" empty="Nothing in flight." /></Labelled>
              </div>
            </Card>
          )}

          {/* Ask clarity */}
          <Card title="Ask clarity" hint="Quality of the stories testers write (scored at submit)" collapsible>
            <div className="flex items-center gap-4">
              <div className="text-3xl font-semibold text-slate-800 w-20">{a.clarity.avg ?? '—'}<span className="text-base text-slate-400">/100</span></div>
              <div className="flex-1">
                {clarTotal === 0
                  ? <div className="text-sm text-slate-400 italic">No scored tickets yet.</div>
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

          {/* Bounced & why */}
          <Card title="Bounced & why" hint="Where asks needed clarification or failed review" collapsible defaultOpen={false}>
            {a.recently_bounced.length === 0
              ? <div className="text-sm text-slate-400 italic">Nothing bounced yet.</div>
              : <ul className="space-y-2">
                  {a.recently_bounced.map((b, i) => (
                    <li key={i} className="flex gap-2 text-sm">
                      <AlertCircle className={`w-4 h-4 mt-0.5 shrink-0 ${b.kind === 'needs_info' ? 'text-rose-500' : 'text-amber-500'}`} />
                      <div>
                        <span className="font-mono text-[11px] text-slate-400 mr-1">{b.ref}</span>
                        <span className="text-slate-700">{b.title}</span>
                        <div className="text-xs text-slate-500">{b.reason}</div>
                      </div>
                    </li>
                  ))}
                </ul>}
          </Card>
        </>
      )}
    </div>
  )
}

function fmtCycle(secs) { return secs != null ? fmtDuration(secs) : '—' }

function splitBy(rows, key) {
  const m = {}
  rows.forEach((r) => { (m[r[key] || '—'] ||= []).push(r) })
  return Object.entries(m).sort((a, b) => b[1].length - a[1].length)
}
function byAssignee(rows) {
  const m = {}
  rows.forEach((r) => { const who = r.assignee || r.created_by || 'unassigned'; (m[who] ||= []).push(r) })
  return Object.entries(m).sort((a, b) => b[1].length - a[1].length)
    .map(([who, rs]) => ({ label: who, value: rs.length, hint: `${rs.filter(isDone).length}/${rs.length}` }))
}

function StatRow({ rows }) {
  const g = agg(rows)
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Stat label="Tickets" value={g.n} sub={`${g.done} done · ${g.open} open`} />
      <Stat label="Avg cycle time" value={fmtCycle(g.avgCycle)} sub="created → done" />
      <Stat label="Agent cost" value={`$${g.cost.toFixed(2)}`} sub={`${fmtH(g.agentSecs)} compute`} />
      <Stat label="Verified" value={g.verifiedPct != null ? `${g.verifiedPct}%` : '—'} sub={`${g.reworkPct}% rework`} tone={g.verifiedPct != null && g.verifiedPct < 60 ? 'warn' : undefined} />
    </div>
  )
}

function CompareColumn({ label, f, set, assignees, rows }) {
  return (
    <div className="border border-slate-200 rounded-xl p-3 space-y-3 bg-white">
      <div className="flex items-center gap-2">
        <span className="text-xs font-bold text-slate-400">{label}</span>
        <div className="flex flex-wrap gap-1.5"><FilterControls f={f} set={set} assignees={assignees} compact /></div>
      </div>
      <StatRow rows={rows} />
      <HBars items={splitBy(rows, 'type').map(([k, v]) => ({ label: k, value: v.length, hint: `${v.length}` }))} color="#6366f1" empty="No tickets." />
    </div>
  )
}

function CmpRow({ label, a, b }) {
  return (
    <>
      <div className="text-slate-500 py-1.5 border-t border-slate-100">{label}</div>
      <div className="text-center text-slate-800 py-1.5 border-t border-slate-100 font-medium">{a}</div>
      <div className="text-center text-slate-800 py-1.5 border-t border-slate-100 font-medium">{b}</div>
    </>
  )
}

const SEL = 'text-xs bg-white border border-slate-200 rounded-lg px-2 py-1 text-slate-600 focus:outline-none focus:ring-2 focus:ring-indigo-200'
function FilterControls({ f, set, assignees, compact }) {
  const on = (k) => (e) => set({ ...f, [k]: e.target.value === 'all' ? 'all' : (k === 'days' ? Number(e.target.value) : e.target.value) })
  return (
    <>
      <select className={SEL} value={f.type} onChange={on('type')}><option value="all">All types</option><option value="feature">Feature</option><option value="bug">Bug</option></select>
      <select className={SEL} value={f.source} onChange={on('source')}><option value="all">All sources</option><option value="automated">Automated</option><option value="manual">Manual</option></select>
      <select className={SEL} value={f.status} onChange={on('status')}><option value="all">Any status</option><option value="open">Open</option><option value="done">Done</option></select>
      {!compact && (
        <select className={SEL} value={f.assignee} onChange={on('assignee')}>
          <option value="all">Anyone</option>
          {assignees.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>)}
      <select className={SEL} value={f.days} onChange={on('days')}><option value="all">All time</option><option value="7">Last 7d</option><option value="30">Last 30d</option></select>
    </>
  )
}

function Labelled({ t, children }) {
  return <div><div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">{t}</div>{children}</div>
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

function Card({ title, hint, children, collapsible, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className={`mb-3 ${collapsible ? 'cursor-pointer' : ''}`} onClick={collapsible ? () => setOpen((o) => !o) : undefined}>
        <div className="flex items-center gap-1.5">
          {collapsible && (open ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />)}
          <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
        </div>
        {hint && open && <p className="text-[11px] text-slate-400 ml-5">{hint}</p>}
      </div>
      {(!collapsible || open) && children}
    </div>
  )
}

function Seg({ n, total, cls, label }) {
  if (!n) return null
  return <div className={cls} style={{ width: `${(n / total) * 100}%` }} title={`${label}: ${n}`} />
}

function MiniBars({ data, color, fmt }) {
  if (!data || !data.length) return <Empty>No data yet.</Empty>
  const W = 320, H = 72, pad = 4
  const max = Math.max(1, ...data.map((d) => d.value))
  const bw = (W - pad * 2) / data.length
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 72 }} preserveAspectRatio="none">
      {data.map((d, i) => {
        const h = ((H - pad * 2) * d.value) / max
        return <rect key={i} x={pad + i * bw + 1} y={H - pad - h} width={Math.max(1, bw - 2)} height={h}
          rx="2" fill={color}><title>{`${d.date}: ${fmt ? fmt(d.value) : d.value}`}</title></rect>
      })}
    </svg>
  )
}

function Spark({ data, color, fmt }) {
  if (!data || !data.length) return <Empty>No data yet.</Empty>
  const W = 320, H = 72, pad = 5
  const max = Math.max(1, ...data.map((d) => d.value))
  const n = data.length
  const x = (i) => pad + (n === 1 ? (W - pad * 2) / 2 : ((W - pad * 2) * i) / (n - 1))
  const y = (v) => pad + (H - pad * 2) * (1 - v / max)
  const pts = data.map((d, i) => `${x(i)},${y(d.value)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 72 }}>
      {n > 1 && <polyline points={pts} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />}
      {data.map((d, i) => (
        <circle key={i} cx={x(i)} cy={y(d.value)} r="3" fill={color}><title>{`${d.date}: ${fmt ? fmt(d.value) : d.value}`}</title></circle>
      ))}
    </svg>
  )
}

function HBars({ items, color, empty }) {
  if (!items || !items.length) return <Empty>{empty || 'No data yet.'}</Empty>
  const max = Math.max(1, ...items.map((i) => i.value))
  return (
    <div className="space-y-1.5">
      {items.map((it) => (
        <div key={it.label} className="flex items-center gap-2 text-xs">
          <div className="w-24 shrink-0 text-slate-500 capitalize truncate">{it.label}</div>
          <div className="flex-1 bg-slate-100 rounded h-3 overflow-hidden">
            <div className="h-3 rounded" style={{ width: `${(it.value / max) * 100}%`, background: color }} title={it.hint} />
          </div>
          <div className="w-14 shrink-0 text-right text-slate-500 tabular-nums">{it.hint}</div>
        </div>
      ))}
    </div>
  )
}

function Empty({ children }) {
  return <div className="text-sm text-slate-400 italic h-[72px] flex items-center">{children}</div>
}
