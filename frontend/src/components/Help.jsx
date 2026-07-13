import React from 'react'
import {
  ListChecks, LayoutGrid, Plus, Bot, Mail, BarChart3, HelpCircle,
  Check, X as XIcon, Ban, ArrowRight, Sparkles, Bug,
} from 'lucide-react'

// The user guide. Written for testers (Alex!) in plain language — what each
// surface is for, how the two relate, and what Docket expects from a human at
// each step. Keep it honest about who does what: the agent codes, people decide.
export default function Help() {
  return (
    <div className="max-w-3xl mx-auto p-4 pb-16 space-y-4">

      <Card icon={HelpCircle} title="What is Docket?">
        <p>
          Docket is where your testing and your requests turn into shipped changes —
          and where you can <em>watch it happen</em>. You write up what you found or what
          you need; an AI developer picks it up, writes the code, and opens it for review;
          Neil approves the code; <strong>you confirm it actually works</strong>. Nothing
          ships without a human saying yes — twice.
        </p>
        <p className="mt-2">There are two main surfaces, and they do different jobs:</p>
        <div className="mt-3 grid sm:grid-cols-2 gap-3">
          <MiniPanel icon={ListChecks} title="Checklist" sub="Verify what was already built">
            A curated list of behaviours that have <em>already shipped</em> in the main app,
            each with steps for how to try it. You mark each one pass / fail / blocked.
            Think of it as the QA test script.
          </MiniPanel>
          <MiniPanel icon={LayoutGrid} title="Board" sub="Ask for something new (or broken) to be fixed">
            Tickets — bugs you found, features you want. Each ticket is a card that
            physically moves across the board as it gets worked, like a production line.
          </MiniPanel>
        </div>
        <p className="mt-3 text-slate-500">
          The two connect in one place: when a checklist item <strong>fails</strong>, you can turn
          that failure into a ticket with one click (more below).
        </p>
      </Card>

      <Card icon={ListChecks} title="The Checklist — verifying shipped work">
        <p>
          Items on the checklist are <strong>put there by the dev side</strong> when work ships —
          you don't add items yourself. Each item describes one specific behaviour the app
          should now have, with a <em>"how to test"</em> recipe. Your job is to actually try it
          in the main app and record what happened:
        </p>
        <ul className="mt-2 space-y-1.5">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 inline-flex items-center gap-1 text-xs font-semibold px-1.5 py-0.5 rounded bg-emerald-600 text-white shrink-0"><Check className="w-3 h-3" />Pass</span>
            <span>You tried it and it works as described.</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 inline-flex items-center gap-1 text-xs font-semibold px-1.5 py-0.5 rounded bg-rose-600 text-white shrink-0"><XIcon className="w-3 h-3" />Fail</span>
            <span>You tried it and it doesn't do what the item says. Add a note saying what you saw instead — that note is gold.</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 inline-flex items-center gap-1 text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-500 text-white shrink-0"><Ban className="w-3 h-3" />Blocked</span>
            <span>You couldn't even try it (e.g. the page won't load, or you need data you don't have). Say why in a note.</span>
          </li>
        </ul>
        <p className="mt-3">
          Every tester records their own verdicts — you'll see dots showing how the others
          got on with the same item. An item counts as "passing" once anyone has passed it.
        </p>
        <p className="mt-2">
          Each item also shows <strong>what the feature is</strong>, the <strong>how-to-test</strong> recipe,
          and an effort tag: <em>test properly</em> (new behaviour — exercise it for real) vs{' '}
          <em>visual check</em> (the logic's already verified — just confirm it looks right).
          Items can be <strong>assigned</strong> to a tester (default "anyone" — use the{' '}
          <em>Mine</em> filter to see your list), and every item has a small{' '}
          <strong>discussion thread</strong> for "is this actually broken?" triage before it
          becomes a ticket. Use the filters at the top — <em>Needs my verdict</em> is your
          to-do list.
        </p>
        <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50 p-3">
          <p className="font-medium text-indigo-900 flex items-center gap-1.5">
            <Plus className="w-4 h-4" /> The bridge: "Raise ticket"
          </p>
          <p className="mt-1 text-indigo-900/80">
            When an item fails, click <strong>Raise ticket</strong> on it. That opens the
            new-ticket form <em>pre-filled</em> from the checklist item, so the developer knows
            exactly which behaviour broke. Add what you saw, and it becomes a card on the
            Board like any other ticket. That's how something moves from "checklist failure"
            to "work item".
          </p>
        </div>
      </Card>

      <Card icon={Plus} title="Raising a good ticket">
        <p>
          Click <strong>New ticket</strong> (top right) or <strong>Raise ticket</strong> from a failing
          checklist item. Two kinds:
        </p>
        <ul className="mt-2 space-y-1">
          <li className="flex items-center gap-2"><Bug className="w-4 h-4 text-rose-500 shrink-0" /> <strong>Bug</strong> — something that should work but doesn't.</li>
          <li className="flex items-center gap-2"><Sparkles className="w-4 h-4 text-indigo-500 shrink-0" /> <strong>Feature</strong> — something new you want the app to do.</li>
        </ul>
        <p className="mt-3">
          The single most useful thing you can write is the <strong>acceptance criteria</strong> —
          a sentence or two describing what "done" looks like, as something you could
          actually check:
        </p>
        <div className="mt-2 grid sm:grid-cols-2 gap-3">
          <Example bad title='"fix the timeline"'>
            The developer has to guess what's wrong, where, and how you'd know it was fixed.
            Tickets like this get bounced back with questions (and that's a round trip that
            costs a day).
          </Example>
          <Example title='"Timeline rows for C5 should show the sender name as that phone saved it — e.g. Pefro, not Trabajo 444"'>
            Specific thing, specific place, checkable result. The agent can verify its own
            work against this before it ever reaches you.
          </Example>
        </div>
        <p className="mt-3">
          As you type, the <strong>clarity meter</strong> scores your ask out of 100 and suggests
          what's missing. It's not a gate — you can submit anything — but high-clarity tickets
          go through in one pass, and vague ones come back with questions.
        </p>
        <p className="mt-2 text-slate-500">
          New tickets land in <strong>Discussion</strong> — a holding area where anyone can comment,
          refine the wording, or change the priority. Nothing happens until someone clicks{' '}
          <strong>Submit for Processing</strong>, which puts it in the queue for the developer.
        </p>
      </Card>

      <Card icon={Bot} title="The life of a ticket — who does what">
        <p>
          Once submitted, the ticket joins the <strong>queue</strong> (higher priority = picked up
          sooner; the card shows "Position #N"). From there an <strong>AI developer agent</strong>{' '}
          works it through the line, and you can watch every step live on the card:
        </p>
        <ol className="mt-3 space-y-2">
          <Step n="1" who="agent" name="Assessment">
            It reads your ticket and the codebase. If the ask is clear, it proceeds. If it's
            vague and important, it bounces to <Badge tone="rose">Needs Info</Badge> with a
            specific question for you — answer and resubmit, and it rejoins the queue.
          </Step>
          <Step n="2" who="agent" name="Planning">
            It writes an implementation plan (you can read it on the ticket's timeline).
          </Step>
          <Step n="3" who="agent" name="In Development">
            It writes the actual code — on its own copy of the app, never the live one.
            The ticker on the card shows what it's doing right now.
          </Step>
          <Step n="4" who="agent" name="Self-Review">
            A fresh pass checks the work against your acceptance criteria. If it finds
            problems, it loops back and fixes them first.
          </Step>
          <Step n="5" who="human" name="PR — Awaiting OK">
            The code change is packaged up for <strong>Neil</strong> to review (he gets an email).
            This is the first human gate — nothing the agent writes can reach the app
            without this approval.
          </Step>
          <Step n="6" who="you" name="User Review">
            <strong>This is your step.</strong> You get an email; the ticket shows a{' '}
            <em>"Ready for you to test"</em> panel with plain-language instructions the agent
            wrote for you. Try it in the app, then click <strong>It works</strong> (→ Done) or{' '}
            <strong>Send back</strong> with what's still wrong — the ticket rejoins the queue for
            another round, and the card counts the iterations (×2, ×3…).
          </Step>
        </ol>
        <p className="mt-3 text-slate-500">
          Side lanes you'll occasionally see: <Badge tone="rose">Needs Info</Badge> (waiting on
          the ticket's author), <Badge tone="amber">Stalled</Badge> (the agent hit a problem —
          Neil is notified), <Badge tone="amber">Changes Requested</Badge> (Neil bounced the
          code). Cards in these lanes are waiting on a person, not the machine.
        </p>
        <p className="mt-2 text-slate-500">
          Each card also shows the <strong>effort so far</strong> (⏱ time · $ cost) — that's real:
          it's what the AI developer's time on your ticket actually cost.
        </p>
      </Card>

      <Card icon={Mail} title="Emails Docket sends">
        <table className="w-full text-sm mt-1">
          <thead>
            <tr className="text-left text-[11px] uppercase text-slate-400">
              <th className="py-1">When</th><th className="py-1">Who gets it</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            <tr><td className="py-1.5">A ticket needs clarification (Needs Info)</td><td>Whoever raised it</td></tr>
            <tr><td className="py-1.5">Code is ready for review (PR)</td><td>Neil</td></tr>
            <tr><td className="py-1.5">A ticket is ready for you to test (User Review)</td><td>The assignee (or whoever raised it)</td></tr>
            <tr><td className="py-1.5">The agent got stuck (Stalled / failed)</td><td>Neil</td></tr>
          </tbody>
        </table>
        <p className="mt-2 text-slate-500">
          They come from <em>Docket</em>. You never need to act on an email that isn't
          addressed to your step.
        </p>
      </Card>

      <Card icon={BarChart3} title="Analytics — what's measured and why">
        <p>
          The Analytics tab shows throughput (tickets in/done), what the AI developer's work
          costs in time and money, and how often tickets bounce for clarification or fail
          review. The point isn't to grade anyone — it's to make visible{' '}
          <strong>what a clear ask saves</strong>: a high-clarity ticket goes ask → tested code in
          one pass; a vague one pays for every round trip. "Bounced &amp; why" is the best
          place to learn what was missing from asks that stalled.
        </p>
      </Card>

      <Card icon={HelpCircle} title="FAQ">
        <Faq q="Can the AI break the live app?">
          No. It works on its own copy and the result is a proposed change that Neil must
          approve before it goes anywhere near the app you use.
        </Faq>
        <Faq q="The checklist and the board both have my problem — which do I use?">
          Record the <strong>fail on the checklist item</strong> (so the verification record is
          accurate), then click <strong>Raise ticket</strong> on it — you get both for the price
          of one.
        </Faq>
        <Faq q="I'm not sure if it's a bug or a feature.">
          Pick your best guess — it doesn't change how it's handled. "Should work but
          doesn't" = bug; "doesn't exist yet" = feature.
        </Faq>
        <Faq q="What do the priorities mean?">
          P0 is "drop everything", P3 is "when there's time". The queue is ordered by
          priority, then by age. Be honest — if everything is P0, nothing is.
        </Faq>
        <Faq q="My ticket came back with a question (Needs Info). Now what?">
          Open it, read the question on the timeline, then use <strong>Resubmit</strong> to answer
          and (optionally) sharpen the description. It rejoins the queue automatically.
        </Faq>
        <Faq q="I clicked Send back — did I do something wrong?">
          Not at all — that's the system working. Say what's still wrong, and the agent gets
          another round with your feedback attached. The iteration counter just shows how
          many rounds it took.
        </Faq>
      </Card>

    </div>
  )
}

/* ---------- little layout helpers (match the app's card language) ---------- */

function Card({ icon: Icon, title, children }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-700 leading-relaxed">
      <h2 className="flex items-center gap-2 text-base font-semibold text-slate-800 mb-2">
        <Icon className="w-4 h-4 text-indigo-600" /> {title}
      </h2>
      {children}
    </div>
  )
}

function MiniPanel({ icon: Icon, title, sub, children }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-center gap-1.5 font-medium text-slate-800">
        <Icon className="w-4 h-4 text-indigo-600" /> {title}
      </div>
      <div className="text-[11px] uppercase tracking-wide text-slate-400 mt-0.5">{sub}</div>
      <p className="mt-1.5 text-slate-600">{children}</p>
    </div>
  )
}

function Example({ bad, title, children }) {
  return (
    <div className={`rounded-lg border p-3 ${bad ? 'border-rose-200 bg-rose-50/60' : 'border-emerald-200 bg-emerald-50/60'}`}>
      <div className={`text-xs font-semibold uppercase tracking-wide ${bad ? 'text-rose-600' : 'text-emerald-700'}`}>
        {bad ? 'Vague' : 'Clear'}
      </div>
      <div className="mt-1 font-medium text-slate-800">{title}</div>
      <p className="mt-1 text-xs text-slate-600">{children}</p>
    </div>
  )
}

function Step({ n, who, name, children }) {
  const whoBadge = who === 'agent'
    ? <Badge tone="indigo">AI</Badge>
    : who === 'you' ? <Badge tone="emerald">You</Badge> : <Badge tone="slate">Neil</Badge>
  return (
    <li className="flex gap-3">
      <span className="shrink-0 w-6 h-6 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold flex items-center justify-center mt-0.5">{n}</span>
      <div>
        <div className="font-medium text-slate-800 flex items-center gap-2">
          {name} {whoBadge} <ArrowRight className="w-3 h-3 text-slate-300" />
        </div>
        <p className="text-slate-600">{children}</p>
      </div>
    </li>
  )
}

function Badge({ tone, children }) {
  const cls = {
    indigo:  'bg-indigo-100 text-indigo-700',
    emerald: 'bg-emerald-100 text-emerald-700',
    slate:   'bg-slate-200 text-slate-600',
    rose:    'bg-rose-100 text-rose-700',
    amber:   'bg-amber-100 text-amber-700',
  }[tone] || 'bg-slate-200 text-slate-600'
  return <span className={`inline-block text-[10px] font-semibold px-1.5 py-0.5 rounded ${cls}`}>{children}</span>
}

function Faq({ q, children }) {
  return (
    <div className="py-2 border-b border-slate-100 last:border-0">
      <div className="font-medium text-slate-800">{q}</div>
      <p className="mt-0.5 text-slate-600">{children}</p>
    </div>
  )
}
