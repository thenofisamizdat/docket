# Docket (portable) — `docket-dev`

Install Docket — a ticket pipeline + autonomous dev agent — into **any git repo**.
It recognizes the codebase, drafts starter tickets, and works them off a queue
(assess → plan → implement → self-review → PR), never auto-merging.

## Install

```bash
pipx install ./dist/docket_dev-0.1.0-py3-none-any.whl   # or: pipx install docket-dev
```

Requires Python ≥3.11, plus the `claude` CLI (authenticated), `git`, and
optionally `msmtp` (for email notifications) on PATH.

## Use

```bash
cd ~/path/to/your/repo
docket init        # detect repo, write .docket/config.toml, init DB, recognize the codebase
docket up          # run the web UI + agent  (open http://localhost:<port>/docket)
```

`docket init` auto-detects the GitHub slug, base branch, and a free port; generates
a per-project login + JWT secret; writes `.docket/` (gitignored); then runs
**recognition**: a stored repo profile (`.docket/profile.md`, injected into the
agent's prompts), a generated `CLAUDE.md` if absent, and seeded starter tickets in
the Discussion zone.

Other commands: `docket serve` (web only), `docket agent [--once]` (agent only),
`docket recognize`, `docket seed`, `docket status`, `docket up --daemon` (write
systemd units).

## Config

Everything lives in the target repo under `.docket/`: `config.toml`, `data/docket.db`,
`data/telemetry.db`, `worktrees/`, `profile.md`. Edit `config.toml` to change the
port, base URL (for notification links), testers, model, branch/remote, GitHub
token (for real PR objects), or to toggle `[agent].writes`/`push`.

## How it works on the target repo

- Each ticket gets its own git worktree + `docket/DKT-<n>` branch off the base branch.
- The agent invokes headless Claude per phase; commits carry a detailed message
  (Why / What changed / Files / Acceptance criteria).
- Branches are pushed and a PR (or compare URL without a token) is opened — **never
  auto-merged**. When you merge it, Docket detects the merge and advances the ticket
  to User Review and notifies.
