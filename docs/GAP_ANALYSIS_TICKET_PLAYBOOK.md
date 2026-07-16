# Playbook: Gap Analysis → Docket Bulk-Ticket File

You are turning one or more **gap-analysis documents (PDFs)** into a single
**markdown ticket file** that will be bulk-imported into Docket, the project's
ticket & roadmap system. Your output fleshes out the whole project — epics,
stories, tasks, bugs, estimates — so the team can start executing immediately.
Follow this playbook exactly: the output format is machine-parsed.

## 1. What to produce

**One markdown file** (e.g. `loupe-alpha-plan.md`) and nothing else. Every
work item in it becomes a real ticket on import, so the file must be complete,
correctly nested, and estimated. Do not include commentary, preamble, or
analysis outside the format below — a short intro paragraph before the first
`## Epic:` heading is ignored by the importer and is allowed, but keep it to a
few lines at most.

## 2. Read the source documents first — properly

1. Read **every page** of each PDF before writing anything. Gap analyses
   usually mix narrative, tables, and lists — gaps hide in all three.
2. Build yourself a flat list of every distinct gap/deficiency/missing
   capability mentioned. One gap = one candidate ticket. Do not merge distinct
   gaps to save space, and do not invent work the documents don't support.
3. De-duplicate across documents: if both PDFs describe the same gap, produce
   ONE ticket whose description cites both.
4. If the documents rank or prioritize (e.g. "blocking for alpha",
   "nice-to-have"), preserve that signal in the ticket priorities.

## 3. How to organize the work

### Hierarchy (exactly three levels)

```
Epic  →  Story  →  Task / Bug
```

- **Epic** — a major functional area or theme (e.g. "Ingestion Reliability",
  "Comms Center", "Timeline", "Search", "Performance & Stability",
  "Alpha Polish"). Aim for **5–9 epics**; if you have more than 10, your epics
  are too fine-grained. Every ticket must live under an epic.
- **Story** — a user-visible capability or coherent chunk of value inside an
  epic, phrased from the user's point of view ("Investigator can filter the
  timeline by contact"). A story typically decomposes into 2–6 child items.
- **Task** — a concrete engineering step. Nest it under its story with a
  `####` heading. A self-contained piece of work that serves the epic directly
  (no story needed) may sit at `###` level as a Task.
- **Bug** — something that exists but is broken. Same nesting rules as Task.
- **Feature** — allowed at `###` level for small standalone additions that
  don't warrant a story; prefer Story + Tasks for anything bigger than a day.
- **Decision** — an open question a PERSON must answer before sibling work can
  proceed: product/design choices, contract definitions, approvals, naming,
  user testing, legal/branding clearance. Use a `#### Decision:` heading under
  the story. Decision tickets import **human-owned**: the build agent never
  picks them up, and every implementation sibling in the same story is
  automatically held in the queue until the decision ticket is closed. This
  matters — a gap analysis naturally produces "decide X, then build against X"
  pairs, and typing the first half as a Task sends an unanswerable question
  into the automated pipeline.

  **Phrasing rule:** if a Task/Feature title *starts with* Define, Confirm,
  Choose, Decide, Select, Approve, or User-test, the importer treats it as a
  Decision anyway (with a dry-run warning). If the item is genuinely buildable,
  rephrase it with an implementation verb ("Implement the approved section
  order" not "Approve and implement the section order" — and put the approval
  in its own `#### Decision:`).

Only ONE level of nesting is supported below a story: a `####` item cannot
have children of its own. If a task feels like it needs subtasks, it's a
story — promote it.

### Ordering

Order epics by build priority (foundations before polish), and order items
within an epic in sensible build order — the importer records document order
as the build sequence, and "Run Full Build" executes top-down.

## 4. How to estimate

Every **Task, Bug, and standalone Story/Feature** gets an `Estimate:` in
**hours of focused engineering work** (the roadmap burns down in hours):

| Size | Hours | Feel |
|------|-------|------|
| XS | 1–2h | Config change, copy fix, small guard |
| S | 3–6h | One well-understood change in one layer |
| M | 8–16h | Multi-layer change (API + UI), or one gnarly layer |
| L | 20–32h | New sub-system, migration, or heavy unknowns |

Rules:
- A story **with children does NOT get its own estimate** — its effort is the
  sum of its children. A story **without children must** have one.
- If an estimate would exceed ~32h, decompose further instead.
- Unknowns are real work: if the gap analysis says "investigate/audit X",
  create a task for the investigation itself (S/M) rather than padding other
  estimates.
- When a document hints at effort ("substantial rework", "quick win"), let it
  pull your estimate up or down one size.

## 5. Description quality bar

Every ticket description must let an engineer (or an autonomous coding agent)
start work **without asking questions**. Include:

1. **Context** — what part of the product this touches and the current
   behavior/gap, citing the source ("Gap analysis §3.2: …").
2. **What to build/fix** — the concrete change, named surfaces ("the
   `/api/comms` endpoint", "the Timeline flyout"), not vague verbs like
   "improve".
3. **Why it matters for alpha** — one line tying it to the release.

Then **acceptance criteria** as observable outcomes, each independently
checkable — "When X, the user sees Y", "Endpoint returns Z". 2–5 bullets.
Never write "works correctly" — say what *correct* looks like.

Priorities: `P0` = alpha-blocking, `P1` = needed for a credible alpha,
`P2` = should-have (default), `P3` = post-alpha polish that still made the cut.

## 6. Output format — EXACT contract

The importer parses headings and metadata lines. Heading grammar:
`## Epic: <name>` · `### Story: <title>` · `### Task|Bug|Feature: <title>` ·
`#### Task|Bug: <title>` (child of the story above it).

Metadata lines (each on its own line, directly under the heading):
- `Priority: P0|P1|P2|P3` — tickets only; omit for P2.
- `Estimate: <hours>h` — e.g. `Estimate: 12h` or `Estimate: 2.5h`.
- `Color: #rrggbb` — epics only, optional (auto-assigned otherwise).
- `Acceptance criteria:` on its own line starts the criteria block; everything
  after it (until the next heading) is the criteria list.

Everything else under a heading is the description (markdown allowed).

### Template

```markdown
# Loupe Alpha Plan
Source: <PDF names + dates>. One-line scope summary.

## Epic: Ingestion Reliability
Color: #10b981
Every supported extraction format ingests completely or fails loudly with an
actionable error. Alpha testers will upload their own data on day one.

### Story: Investigator can trust upload progress
Priority: P1
Uploads are the first thing an alpha tester touches. Gap analysis §2.1 notes
progress is lost on restart and silently misreports on reconnect.

Acceptance criteria:
- An interrupted upload resumes from its last completed chunk after a backend restart
- The progress bar never moves backwards during a session

#### Task: Persist upload state to disk
Priority: P1
Estimate: 6h
The tus upload registry lives in memory (§2.1). Move it to a disk-backed
store keyed by upload id so resumability survives restarts.

Acceptance criteria:
- Killing and restarting the backend mid-upload preserves offsets
- Stale upload state older than 7 days is garbage-collected

#### Bug: Progress bar resets to 0% on reconnect
Estimate: 3h
§2.1: on websocket reconnect the Uppy progress indicator resets to zero even
though chunks are preserved server-side. Re-seed the client from the server
offset on reconnect.

Acceptance criteria:
- After a network blip, the bar resumes from the true offset

### Task: Reconciliation report per ingested file
Estimate: 8h
Standalone epic-level work item (§2.4): after ingest, show counted-vs-written
totals per artifact type so silent drops are visible.

Acceptance criteria:
- Every completed ingest shows a per-type reconciliation table
- A mismatch >0 renders as a visible warning, never hidden

## Epic: Comms Center
...
```

## 7. Self-check before you finish

- [ ] Every gap in the PDFs maps to at least one ticket (or is explicitly out
      of scope in the intro line); nothing invented.
- [ ] 5–9 epics; every ticket under an epic; nesting never deeper than
      Epic → Story → Task/Bug.
- [ ] Every leaf item has an `Estimate:`; stories with children have none.
- [ ] Every ticket has context + concrete change + acceptance criteria.
- [ ] Priorities reflect the documents' alpha-blocking signals.
- [ ] Headings match the grammar EXACTLY (`## Epic:`, `### Story:`,
      `#### Task:`, `#### Bug:`) — the importer is heading-driven.
- [ ] Totals sanity: sum the estimates; if the total is wildly out of line
      with the documents' own effort framing, revisit your sizing.

Finish by reporting (in chat, not in the file): epic count, ticket count by
type, total estimated hours, and any gaps you deliberately excluded and why.
