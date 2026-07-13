import React, { useEffect, useState } from 'react'
import {
  ArrowLeft, Trophy, TrendingUp, TrendingDown, Sparkles, Wrench,
  HeartHandshake, Package, Star, AlertTriangle, Check, ChevronDown,
} from 'lucide-react'
import { api } from '../api.js'
import { fmtDuration, relTime } from '../ui.js'

// Gamified tester profiles (Phase 5). The pitch: make the cost of vague asks —
// and the value of clear ones — personal. Every score is transparent (the
// weights come from the API) so "how do I improve?" always has an answer.

const DIM_META = {
  clarity:        { label: 'Ask clarity',        bar: 'bg-indigo-500' },
  first_time:     { label: 'First-time-through', bar: 'bg-emerald-500' },
  helpfulness:    { label: 'Helping others',     bar: 'bg-pink-500' },
  responsiveness: { label: 'Review speed',       bar: 'bg-amber-500' },
  efficiency:     { label: 'Lean asks',          bar: 'bg-sky-500' },
  impact:         { label: 'Shipped impact',     bar: 'bg-violet-500' },
}
const DIM_ORDER = ['clarity', 'first_time', 'helpfulness', 'responsiveness', 'efficiency', 'impact']
const MEDALS = ['🥇', '🥈', '🥉']

function scoreTone(s) {
  if (s == null) return 'text-slate-300'
  return s >= 75 ? 'text-emerald-600' : s >= 50 ? 'text-amber-600' : 'text-rose-600'
}

export default function Profiles({ onOpenTicket }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // username

  useEffect(() => {
    const load = () => api.profiles().then(setData).catch((e) => setErr(e.message))
    load()
    const iv = setInterval(load, 20000)
    return () => clearInterval(iv)
  }, [])

  if (err) return <div className="p-6 text-red-600 text-sm">{err}</div>
  if (!data) return <div className="p-6 text-slate-400">Loading profiles…</div>

  const profile = selected && data.profiles.find((p) => p.username === selected)
  if (profile) {
    return <ProfileDetail p={profile} weights={data.weights}
      onBack={() => setSelected(null)} onOpenTicket={onOpenTicket} />
  }

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-slate-800">Tester profiles</h2>
        <p className="text-xs text-slate-500">
          The Docket Score rewards clear asks, tickets that ship first time, helping
          other people's tickets along, fast reviews, lean agent cost, and impact after
          shipping. Click a card for the full story.
        </p>
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        {data.profiles.map((p, i) => (
          <button key={p.username} onClick={() => setSelected(p.username)}
            className="text-left rounded-xl border border-slate-200 bg-white p-4 hover:border-indigo-300 hover:shadow-sm transition">
            <div className="flex items-center gap-3 mb-2">
              <span className="text-xl">{p.rank ? (MEDALS[p.rank - 1] || `#${p.rank}`) : '·'}</span>
              <span className="font-semibold text-slate-800">{p.name}</span>
              <span className="ml-auto flex items-baseline gap-1">
                <span className={`text-2xl font-bold tabular-nums ${scoreTone(p.score)}`}>{p.score ?? '—'}</span>
                <span className="text-[10px] text-slate-400">/100</span>
              </span>
            </div>
            {p.score == null && (
              <div className="text-xs text-slate-400 italic mb-2">
                Not enough activity yet — raise a ticket to get on the board.
              </div>
            )}
            <div className="space-y-1 mb-2">
              {DIM_ORDER.map((k) => (
                <MiniBar key={k} label={DIM_META[k].label} cls={DIM_META[k].bar} value={p.dims[k]} />
              ))}
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <span>{p.stats.tickets} tickets · {p.stats.done} shipped · {p.stats.assists} assists</span>
              <span className="ml-auto text-sm">
                {p.badges.filter((b) => b.earned).map((b) => (
                  <span key={b.id} title={`${b.name} — ${b.desc}`}>{b.emoji}</span>
                ))}
              </span>
            </div>
          </button>
        ))}
      </div>

      {data.hall_of_fame.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-center gap-2 mb-2">
            <Trophy className="w-4 h-4 text-amber-600" />
            <h3 className="text-sm font-semibold text-amber-800">Hall of fame — what a good ask looks like</h3>
          </div>
          <div className="grid md:grid-cols-3 gap-2">
            {data.hall_of_fame.map((h) => (
              <button key={h.ref} onClick={() => onOpenTicket && onOpenTicket(h.id)}
                className="text-left bg-white rounded-lg border border-amber-100 p-3 hover:border-amber-300">
                <div className="flex items-center gap-1.5 text-[11px] text-slate-400 mb-1">
                  <span className="font-mono">{h.ref}</span>
                  <span className="ml-auto font-semibold text-emerald-600">{h.score}/100</span>
                </div>
                <div className="text-sm text-slate-700 leading-snug mb-1.5">{h.title}</div>
                <div className="flex flex-wrap gap-1">
                  {h.strengths.map((s) => (
                    <span key={s} className="text-[10px] bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-1.5 py-0.5">{s}</span>
                  ))}
                </div>
                <div className="text-[11px] text-slate-400 mt-1.5">
                  by {h.author}{h.cost != null && <> · ${h.cost.toFixed(2)} agent cost</>}{h.iterations === 0 && ' · first time through'}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      <HowScoring weights={data.weights} />
    </div>
  )
}

function ProfileDetail({ p, weights, onBack, onOpenTicket }) {
  const s = p.stats
  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      <button onClick={onBack} className="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700">
        <ArrowLeft className="w-4 h-4" /> All profiles
      </button>

      {/* Header: score + badges */}
      <div className="rounded-xl border border-slate-200 bg-white p-4 flex flex-wrap items-center gap-4">
        <div>
          <div className="text-lg font-semibold text-slate-800">{p.name}</div>
          <div className="text-xs text-slate-400">
            {p.rank ? `Ranked ${MEDALS[p.rank - 1] || `#${p.rank}`} on the team` : 'Not ranked yet'}
          </div>
        </div>
        <div className="flex items-baseline gap-1">
          <span className={`text-4xl font-bold tabular-nums ${scoreTone(p.score)}`}>{p.score ?? '—'}</span>
          <span className="text-xs text-slate-400">/100 Docket Score</span>
        </div>
        <div className="ml-auto flex gap-1.5 text-xl">
          {p.badges.filter((b) => b.earned).map((b) => (
            <span key={b.id} title={`${b.name} — ${b.desc}`}>{b.emoji}</span>
          ))}
        </div>
      </div>

      {/* Dimensions */}
      <Card title="Score breakdown" hint="Every dimension is 0–100; the weights are fixed and public">
        <div className="space-y-2.5">
          {DIM_ORDER.map((k) => (
            <div key={k} className="flex items-center gap-3">
              <span className="w-36 text-xs text-slate-600">{DIM_META[k].label}
                <span className="text-slate-300"> · {Math.round(weights[k] * 100)}%</span>
              </span>
              <div className="flex-1 h-2.5 bg-slate-100 rounded-full overflow-hidden">
                {p.dims[k] != null && <div className={`h-full ${DIM_META[k].bar}`} style={{ width: `${p.dims[k]}%` }} />}
              </div>
              <span className="w-16 text-right text-xs tabular-nums text-slate-600">{p.dims[k] ?? 'no data'}</span>
              <span className="w-44 text-[11px] text-slate-400">{dimDetail(k, s)}</span>
            </div>
          ))}
        </div>
      </Card>

      {/* Clarity journey + clarity pays */}
      <div className="grid md:grid-cols-2 gap-3">
        <Card title="Clarity journey" hint="Score of each ask, in the order you raised them">
          {p.clarity_series.length >= 2 ? (
            <div className="flex items-center gap-3">
              <Sparkline points={p.clarity_series} />
              {s.trend != null && (
                <span className={`flex items-center gap-1 text-sm font-medium ${s.trend >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {s.trend >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                  {s.trend > 0 ? '+' : ''}{s.trend}
                </span>
              )}
            </div>
          ) : <Empty>Raise a couple of tickets to see your trend.</Empty>}
        </Card>

        <Card title="Clarity pays" hint="Your own tickets: what clear vs unclear asks cost the agent">
          {p.clear_vs_unclear ? (
            <div className="grid grid-cols-2 gap-2 text-center">
              <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-2">
                <div className="text-[11px] text-emerald-700 font-medium">Clear asks (70+)</div>
                <div className="text-lg font-semibold text-emerald-700">${p.clear_vs_unclear.clear.avg_cost}</div>
                <div className="text-[11px] text-slate-500">{fmtDuration(p.clear_vs_unclear.clear.avg_secs)} · {p.clear_vs_unclear.clear.n} tickets</div>
              </div>
              <div className="rounded-lg bg-rose-50 border border-rose-200 p-2">
                <div className="text-[11px] text-rose-700 font-medium">Unclear asks</div>
                <div className="text-lg font-semibold text-rose-700">${p.clear_vs_unclear.unclear.avg_cost}</div>
                <div className="text-[11px] text-slate-500">{fmtDuration(p.clear_vs_unclear.unclear.avg_secs)} · {p.clear_vs_unclear.unclear.n} tickets</div>
              </div>
            </div>
          ) : <Empty>Once both clear and unclear asks of yours have been worked, the cost difference shows here.</Empty>}
        </Card>
      </div>

      {/* Best vs needs work */}
      <div className="grid md:grid-cols-2 gap-3">
        <Card title="Best asks" icon={<Sparkles className="w-4 h-4 text-emerald-600" />}
          hint="Your clearest stories — keep doing this">
          {p.best.length === 0 ? <Empty>No standout asks yet — acceptance criteria are the fastest win.</Empty> :
            p.best.map((e) => (
              <Example key={e.ref} e={e} good onOpen={onOpenTicket}>
                <div className="flex flex-wrap gap-1 mt-1">
                  {e.strengths.map((x) => (
                    <span key={x} className="text-[10px] bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-1.5 py-0.5">{x}</span>
                  ))}
                </div>
              </Example>
            ))}
        </Card>
        <Card title="Needs work" icon={<Wrench className="w-4 h-4 text-rose-500" />}
          hint="Low-clarity asks and what they cost downstream">
          {p.worst.length === 0 ? <Empty>Nothing to fix — your asks are landing well.</Empty> :
            p.worst.map((e) => (
              <Example key={e.ref} e={e} onOpen={onOpenTicket}>
                <ul className="mt-1 space-y-0.5">
                  {(e.suggestions || []).map((x, i) => (
                    <li key={i} className="text-[11px] text-rose-600">→ {x}</li>
                  ))}
                </ul>
              </Example>
            ))}
        </Card>
      </div>

      {/* Assists + shipped impact */}
      <div className="grid md:grid-cols-2 gap-3">
        <Card title="Helping hand" icon={<HeartHandshake className="w-4 h-4 text-pink-500" />}
          hint="Your comments on other people's tickets">
          {p.assist_feed.length === 0
            ? <Empty>No assists yet — answering a Needs-Info question or adding repro steps to someone's bug counts.</Empty>
            : <ul className="space-y-2">
                {p.assist_feed.map((a, i) => (
                  <li key={i} className="text-sm">
                    <span className="font-mono text-[11px] text-slate-400 mr-1">{a.ref}</span>
                    <span className="text-slate-700">{a.title}</span>
                    {a.helped && <span className="ml-1.5 text-[10px] bg-pink-50 text-pink-700 border border-pink-200 rounded px-1 py-0.5">helped it move</span>}
                    <div className="text-xs text-slate-500 truncate">“{a.snippet}” · {relTime(a.ts)}</div>
                  </li>
                ))}
              </ul>}
        </Card>

        <Card title="Badges" hint="Earned in colour; the bar shows how close you are">
          <div className="grid grid-cols-2 gap-2">
            {p.badges.map((b) => (
              <div key={b.id} title={b.hint || b.desc}
                className={`rounded-lg border p-2.5 ${b.earned ? 'border-amber-200 bg-amber-50' : 'border-slate-200 bg-slate-50 opacity-70'}`}>
                <div className="text-xl mb-0.5">{b.emoji}</div>
                <div className="text-xs font-semibold text-slate-700">{b.name}</div>
                <div className="text-[10px] text-slate-500 leading-snug mb-1.5">{b.desc}</div>
                {b.earned
                  ? <div className="text-[10px] font-medium text-amber-700">Earned ✓</div>
                  : <>
                      <div className="h-1.5 bg-slate-200 rounded-full overflow-hidden">
                        <div className="h-full bg-indigo-400" style={{ width: `${Math.round(b.progress * 100)}%` }} />
                      </div>
                      <div className="text-[10px] text-slate-400 mt-0.5">{b.n}/{b.target}</div>
                    </>}
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* Full ticket history with real platform performance */}
      <Card title="Ticket history & platform performance"
        icon={<Package className="w-4 h-4 text-violet-500" />}
        hint="Every ask you've raised — and, once shipped, how it's actually doing in the platform (traffic, errors, follow-ups)">
        {p.history.length === 0 ? <Empty>No tickets yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase text-slate-400">
                  <th className="py-1 pr-2">Ticket</th>
                  <th className="py-1 pr-2">Status</th>
                  <th className="py-1 pr-2 text-right">Clarity</th>
                  <th className="py-1 pr-2 text-right">Retries</th>
                  <th className="py-1 pr-2 text-right">Cost</th>
                  <th className="py-1">Post-ship</th>
                </tr>
              </thead>
              <tbody>
                {p.history.map((h) => (
                  <tr key={h.ref} className="border-t border-slate-100 align-top">
                    <td className="py-1.5 pr-2 max-w-[18rem]">
                      <button className="text-left hover:underline" onClick={() => onOpenTicket && onOpenTicket(h.id)}>
                        <span className="font-mono text-[11px] text-slate-400 mr-1">{h.ref}</span>
                        <span className="text-slate-700">{h.title}</span>
                      </button>
                    </td>
                    <td className="py-1.5 pr-2 text-xs text-slate-500 whitespace-nowrap">{h.status.replace(/_/g, ' ')}</td>
                    <td className={`py-1.5 pr-2 text-right tabular-nums ${h.clarity == null ? 'text-slate-300' : h.clarity >= 70 ? 'text-emerald-600' : h.clarity >= 40 ? 'text-amber-600' : 'text-rose-600'}`}>
                      {h.clarity ?? '—'}
                    </td>
                    <td className={`py-1.5 pr-2 text-right tabular-nums ${h.iterations + h.bounced ? 'text-amber-600' : 'text-slate-400'}`}>
                      {h.iterations + h.bounced || '—'}
                    </td>
                    <td className="py-1.5 pr-2 text-right tabular-nums text-slate-500">
                      {h.cost != null ? `$${h.cost.toFixed(2)}` : '—'}
                    </td>
                    <td className="py-1.5"><PostShip h={h} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

const PERF_CHIP = {
  healthy: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  degraded: 'bg-rose-50 text-rose-700 border-rose-200',
  watch: 'bg-amber-50 text-amber-700 border-amber-200',
  no_traffic: 'bg-slate-50 text-slate-500 border-slate-200',
}

// The post-ship story of one history row: telemetry verdict + traffic, star
// rating if anyone rated it, confirmed follow-ups (negative), unconfirmed count.
function PostShip({ h }) {
  if (!h.done_ts) return <span className="text-xs text-slate-300">—</span>
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      {h.perf
        ? <>
            <span className={`px-1.5 py-0.5 rounded border text-[10px] font-medium ${PERF_CHIP[h.perf.verdict] || ''}`}>
              {h.perf.verdict === 'no_traffic' ? 'no traffic yet' : h.perf.verdict}
            </span>
            {h.perf.hits > 0 && (
              <span className="text-slate-500">
                {h.perf.hits} req · {h.perf.errors} err
              </span>
            )}
          </>
        : <span className="text-slate-400 italic">no route map</span>}
      {h.rating_avg != null && (
        <span className="text-amber-600 flex items-center gap-0.5">
          <Star className="w-3 h-3 fill-amber-400 text-amber-400" />{h.rating_avg}
        </span>
      )}
      {h.regressions.length > 0 && (
        <span className="flex items-center gap-0.5 text-rose-600">
          <AlertTriangle className="w-3 h-3" />{h.regressions.join(', ')}
        </span>
      )}
      {h.suspected > 0 && (
        <span className="text-slate-400">{h.suspected} unconfirmed</span>
      )}
      {h.perf && h.perf.verdict === 'healthy' && h.regressions.length === 0 && (
        <Check className="w-3 h-3 text-emerald-500" />
      )}
    </div>
  )
}

// Plain-words annotation next to each dimension bar.
function dimDetail(k, s) {
  switch (k) {
    case 'clarity': return s.avg_clarity != null ? `avg ${s.avg_clarity}/100 over ${s.n_scored} asks` : 'no scored asks yet'
    case 'first_time': return s.done ? `${s.ftt_count} of ${s.done} shipped clean` : 'nothing shipped yet'
    case 'helpfulness': return `${s.assists} assists · ${s.comments_on_others} comments on others`
    case 'responsiveness': return s.avg_resp_h != null ? `avg ${s.avg_resp_h}h to test (${s.n_resp})` : 'no reviews handled yet'
    case 'efficiency': return s.avg_cost != null ? `$${s.avg_cost} avg agent cost` : 'no worked tickets yet'
    case 'impact': return s.rating_avg != null
      ? `★${s.rating_avg} avg · ${s.healthy_done}/${s.done} healthy`
      : (s.done ? `${s.healthy_done}/${s.done} healthy` : 'ship something first')
    default: return ''
  }
}

function Example({ e, good, children, onOpen }) {
  return (
    <div className={`rounded-lg border p-2.5 mb-2 last:mb-0 ${good ? 'border-emerald-100 bg-emerald-50/40' : 'border-rose-100 bg-rose-50/40'}`}>
      <button className="text-left w-full" onClick={() => onOpen && onOpen(e.id)}>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] text-slate-400">{e.ref}</span>
          <span className={`text-xs font-semibold ${good ? 'text-emerald-600' : 'text-rose-600'}`}>{e.score}/100</span>
          <span className="ml-auto text-[11px] text-slate-400">
            {e.bounced > 0 && `bounced ×${e.bounced} · `}
            {e.iterations > 0 && `retries ×${e.iterations} · `}
            {e.cost != null && `$${e.cost.toFixed(2)}`}
          </span>
        </div>
        <div className="text-sm text-slate-700 leading-snug">{e.title}</div>
      </button>
      {children}
    </div>
  )
}

function Sparkline({ points }) {
  const w = 240, h = 56, pad = 6
  const xs = points.map((_, i) => pad + i * (w - 2 * pad) / (points.length - 1))
  const ys = points.map((p) => h - pad - (p.score / 100) * (h - 2 * pad))
  return (
    <svg width={w} height={h} className="shrink-0">
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="#e2e8f0" />
      <polyline points={xs.map((x, i) => `${x},${ys[i]}`).join(' ')}
        fill="none" stroke="#6366f1" strokeWidth="2" strokeLinejoin="round" />
      {xs.map((x, i) => (
        <circle key={i} cx={x} cy={ys[i]} r="2.5" fill="#6366f1">
          <title>{points[i].ref}: {points[i].score}</title>
        </circle>
      ))}
    </svg>
  )
}

function HowScoring({ weights }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <button onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 p-3 text-sm text-slate-600">
        <ChevronDown className={`w-4 h-4 transition-transform ${open ? 'rotate-180' : ''}`} />
        How the Docket Score works
      </button>
      {open && (
        <div className="px-4 pb-4 text-xs text-slate-500 space-y-1.5">
          <p><b>Ask clarity ({Math.round(weights.clarity * 100)}%)</b> — the average clarity score of your tickets at submit. Acceptance criteria, concrete behaviour, and specific titles push it up.</p>
          <p><b>First-time-through ({Math.round(weights.first_time * 100)}%)</b> — the share of your shipped tickets that never bounced to Needs Info or came back for a retry. Bounces and resubmits pull this down.</p>
          <p><b>Helping others ({Math.round(weights.helpfulness * 100)}%)</b> — comments you leave on other people's tickets that are followed by the ticket moving forward.</p>
          <p><b>Review speed ({Math.round(weights.responsiveness * 100)}%)</b> — how quickly you test tickets that land in User Review for you.</p>
          <p><b>Lean asks ({Math.round(weights.efficiency * 100)}%)</b> — average agent cost of your tickets, anchored to the team's best. Clear, scoped asks burn fewer tokens.</p>
          <p><b>Shipped impact ({Math.round(weights.impact * 100)}%)</b> — whether your shipped tickets stayed healthy in the real platform: no confirmed follow-up ticket against them, and no error-rate regression on the routes the change touched (measured automatically from live traffic). Star-ratings, when people leave them, count too.</p>
          <p className="text-slate-400">Dimensions with no data yet are skipped and the weights renormalise — nobody is penalised for being new.</p>
        </div>
      )}
    </div>
  )
}

function Card({ title, hint, icon, children }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">{icon}{title}</h3>
        {hint && <p className="text-[11px] text-slate-400">{hint}</p>}
      </div>
      {children}
    </div>
  )
}

function MiniBar({ label, cls, value }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-32 text-[10px] text-slate-400">{label}</span>
      <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
        {value != null && <div className={`h-full ${cls}`} style={{ width: `${value}%` }} />}
      </div>
      <span className="w-7 text-right text-[10px] tabular-nums text-slate-400">{value ?? '—'}</span>
    </div>
  )
}

function Empty({ children }) {
  return <div className="text-sm text-slate-400 italic">{children}</div>
}
