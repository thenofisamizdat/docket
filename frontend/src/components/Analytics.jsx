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
  const pipe = a.pipeline
  const vTotal = pipe ? pipe.verified + pipe.unverified : 0
  const verifiedPct = vTotal ? Math.round((pipe.verified / vTotal) * 100) : 0
  const pretty = (s) => (s || '').replace(/_/g, ' ')

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      {/* Top metric cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Tickets" value={a.totals.total} sub={`${a.totals.done} done · ${a.totals.open} open`} />
        <Stat label="Bounce rate" value={`${a.quality.bounce_rate}%`} sub={`${a.quality.bounced_tickets} needed clarification`} tone={a.quality.bounce_rate > 25 ? 'warn' : 'ok'} />
        <Stat label="Avg effort / ticket" value={fmtDuration(a.effort.avg_secs) || '—'} sub={`$${a.effort.avg_cost_usd} avg`} />
        <Stat label="Total agent cost" value={`$${a.effort.total_cost_usd}`} sub={`${fmtDuration(a.effort.total_secs)} compute · ${a.quality.resubmits} resubmits`} />
      </div>

      {/* Pipeline analytics */}
      {pipe && (
        <Card title="Pipeline" hint="Automated development — throughput, cost, cycle time, quality & flow">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <Stat label="Shipped" value={pipe.cycle_count} sub="done by the agent" />
            <Stat label="Avg cycle time" value={fmtDuration(pipe.avg_cycle_secs) || '—'} sub="created → done" />
            <Stat label="Verified" value={`${verifiedPct}%`} sub={`${pipe.verified} verified · ${pipe.unverified} not`} tone={vTotal && verifiedPct < 60 ? 'warn' : undefined} />
            <Stat label="Rework rate" value={`${pipe.rework_rate}%`} sub={`${a.quality.failed_review} needed a redo`} tone={pipe.rework_rate > 25 ? 'warn' : undefined} />
          </div>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Throughput / day</div>
              <MiniBars data={pipe.throughput_by_day} color="#3fb96a" fmt={(v) => `${v} shipped`} />
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Agent cost / day</div>
              <Spark data={pipe.cost_by_day} color="#4f8cff" fmt={(v) => `$${v}`} />
            </div>
          </div>
          <div className="grid md:grid-cols-2 gap-4 mt-4">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Avg time in stage</div>
              <HBars items={pipe.time_in_stage.map((s) => ({ label: pretty(s.status), value: s.avg_secs, hint: fmtDuration(s.avg_secs) }))} color="#4f8cff" />
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Work in progress</div>
              <HBars items={Object.entries(pipe.wip).filter(([, v]) => v).map(([k, v]) => ({ label: pretty(k), value: v, hint: `${v}` }))} color="#e0a83c" empty="Nothing in the pipeline right now." />
            </div>
          </div>
          {pipe.estimate_vs_actual.length > 0 && (
            <div className="mt-4">
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Estimate vs actual (pipeline-built)</div>
              <table className="w-full text-sm">
                <tbody>
                  {pipe.estimate_vs_actual.slice(0, 8).map((e) => (
                    <tr key={e.ref} className="border-t border-slate-100">
                      <td className="py-1 font-mono text-[11px] text-slate-400">{e.ref}</td>
                      <td className="py-1 text-right text-slate-500">est {e.estimate}h</td>
                      <td className={`py-1 text-right ${e.actual > e.estimate ? 'text-rose-600' : 'text-emerald-600'}`}>actual {e.actual}h</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

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

// Daily bars (throughput). Inline SVG, no chart lib — matches Profiles.jsx idiom.
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

// Daily line (cost). Inline SVG polyline + point tooltips.
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
        <circle key={i} cx={x(i)} cy={y(d.value)} r="3" fill={color}>
          <title>{`${d.date}: ${fmt ? fmt(d.value) : d.value}`}</title>
        </circle>
      ))}
    </svg>
  )
}

// Horizontal magnitude bars (time-in-stage, WIP).
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
