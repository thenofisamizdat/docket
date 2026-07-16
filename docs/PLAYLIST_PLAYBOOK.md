# Playbook: Bulk Ticket Plan → Docket Playlist

You are turning a **Docket bulk-ticket plan file** (the markdown you produced
from the gap-analysis playbook — epics, stories, tasks, bugs, estimates) into
a **playlist file**: an ordered instruction document telling Docket's
autonomous pipeline which tickets to work through and in what order. Follow
this playbook exactly: the output is machine-parsed and drives real builds.

## 1. What to produce

**One markdown playlist file** (e.g. `loupe-alpha-playlist.md`) and nothing
else. Docket will apply it after (or any time after) the plan file has been
bulk-imported. On apply, Docket stamps the listed tickets with build order
1..N — they run **before** every unlisted ticket — and, in queue mode, hands
them to the pipeline in that order.

## 2. Ground rules — how items are matched

- Reference every ticket by its **exact title from the plan file**, verbatim,
  case-insensitive (`1. Persist tus upload state to disk`). At authoring time
  the tickets have no DKT numbers yet — the title IS the key, so never
  paraphrase, truncate, or "improve" a title.
- If you happen to know a ticket's DKT ref (e.g. you were given a board
  export), `1. DKT-27` also works and wins over the title.
- **List only leaf work items**: tasks, bugs, and childless stories/features.
  Do NOT list container stories (stories that have `####` children in the
  plan) — Docket builds their children, not the umbrella, and will skip them.
- Every title you list must exist in the plan file. Docket reports unmatched
  lines rather than guessing; an unmatched line is a defect in your playlist.
- You may sequence **all** leaf tickets or a **subset** (e.g. "phase 1 only").
  Unlisted tickets keep their plan order but run after the whole playlist.

## 3. How to choose the order

Order is dependency-first, then risk, then value:

1. **Hard dependencies before dependents.** Data model / schema / shared
   utilities before the endpoints that use them; endpoints before the UI that
   calls them; a bug fix before a feature that builds on the fixed behavior.
   Dependencies often cross epics — interleaving epics is correct when the
   dependency graph says so.
2. **Walking skeleton early.** Prefer an order where, after each phase, the
   product is runnable and testable end-to-end, rather than finishing one
   layer everywhere before starting the next.
3. **Risk next.** Unknowns, investigations, and gnarly integrations go early
   in their phase — if they blow up, the plan can adapt while there's slack.
4. **Bugs ride with their area.** Schedule a bug right after (or before) the
   tasks touching the same surface, so the agent has the context fresh.
5. **Polish last.** P3 / cosmetic items go in the final phase.

Within a story, keep its child tasks contiguous and in the plan's order
unless a dependency forces otherwise.

**Priority caveat:** Docket's live queue orders by priority first (P0 → P3),
then by your sequence. Your order holds exactly within a priority level; a P1
listed later will still jump ahead of an earlier P2. If strict global order
matters to you, keep the listed tickets' priorities aligned with your
sequence rather than fighting it.

## 4. Output format — EXACT contract

```markdown
# Playlist: <name>
Mode: queue

## Phase 1: <milestone name>
1. <exact ticket title>
2. DKT-14
3. <exact ticket title>

## Phase 2: <milestone name>
1. <exact ticket title>
...
```

- `Mode: queue` — apply the order AND queue every listed ticket to the
  pipeline immediately, in order. `Mode: order` — set the build order only
  (the team triggers builds later via Run Full Build or per-epic ▶ queue).
  Default to `order` unless instructed otherwise.
- `## Phase N: name` headings group items into milestones — they're for
  humans and reporting; the global order is simply top-to-bottom.
- Items are numbered (`1.`) or bulleted (`-`); one ticket per line; nothing
  else on the line except the title (or DKT ref, optionally followed by the
  title for readability: `1. DKT-14 Persist tus upload state`).
- A short prose line under a phase heading is allowed (Docket ignores prose)
  — use it to state the milestone's goal in one sentence.

## 5. Self-check before you finish

- [ ] Every line matches a plan-file title verbatim (or a real DKT ref).
- [ ] No container stories listed — leaves only.
- [ ] Every hard dependency appears before its dependents; no phase depends
      on work in a later phase.
- [ ] Each phase ends in a runnable, testable increment.
- [ ] Mode is set (and is `order` unless told to queue).
- [ ] If this is a subset playlist, say in the phase prose what was left out.

Finish by reporting (in chat, not in the file): total tickets sequenced, how
many phases, the reasoning for the phase boundaries, and any plan tickets you
deliberately left unsequenced and why.
