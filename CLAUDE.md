## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues (repo: austinbreunig/conservation-dashboard), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — `CONTEXT.md` + `docs/adr/` at the repo root, created lazily. See `docs/agents/domain.md`.

## Style

Terse, plain language. No padding, no restating the obvious. Match response
length to the question, not to how thorough a response could theoretically be.

## Core Responsibilities for this project
* PM invokes the mattpocock skills directly per `playbook/phases.md` (Discovery → POC → Tracer → MVP → Refinement)
* Enforce Wu Wei design via `.claude/skills/wu-wei/` at each phase gate

## Delegation Rules
**mentor** — plain-language translation (ad hoc) or `/teach` (explicit curriculum), personal knowledge base. The only named agent besides PM — see root `agents/pm.md` and `playbook/phases.md`.
---