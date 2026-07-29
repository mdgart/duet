# Duet — Cross-Model Prototyping & Coding Agent Loop

**Project Plan · v0.1 · 2026-07-26**

## 1. Vision

A CLI tool that takes an idea — anywhere from a vague vision to a precise implementation brief — and drives it to a working prototype or complete feature through a structured adversarial loop between two frontier models from different labs (OpenAI GPT-5.6 Sol and Anthropic Claude Opus 5 by default, both configurable). One model proposes, the other critiques and counter-proposes, until they converge. A human approves at two hard gates: the agreed implementation spec, and the final code.

Why cross-lab pairing: models from different labs have decorrelated blind spots and training biases. A GPT proposal reviewed by Claude (and vice versa) catches errors that same-model self-review reliably misses, and the disagreement signal itself is informative — persistent disagreement usually marks a real design risk worth surfacing to the human.

## 2. Core Loop (two phases, same protocol)

**Phase A — Design convergence.** Idea → Model 1 drafts an implementation spec → Model 2 critiques with structured objections → Model 1 revises or rebuts → repeat until both models emit an explicit AGREE verdict (or max rounds hit → human arbitrates). Output: an agreed `IMPLEMENTATION.md`. **Human gate 1: approve / edit / reject the spec.**

**Phase B — Build convergence.** Coder model implements the spec task-by-task in a sandboxed workspace (new project or existing repo). Reviewer model evaluates each change: runs tests, reads diffs, files structured findings (bug / spec-violation / style). Coder fixes; roles are fixed per run but which lab plays which role is configurable and can be swapped per task. Repeat per task until the reviewer signs off and tests pass. **Human gate 2: approve the working prototype / feature diff.**

The two phases share one protocol: propose → critique → revise → verdict, with round caps, convergence detection, and human escalation on deadlock. This is the central abstraction of the codebase.

## 3. Scope

**In scope (v1):**

- Python CLI (`duet`), streaming the debate live to the terminal with per-model attribution.
- Providers: OpenAI Responses API (`gpt-5.6-sol`) and Anthropic Messages API (`claude-opus-5`), behind a provider interface so models are swappable via config.
- Greenfield projects and existing repos (repo context via file-tree map + on-demand file reads by the agents; work happens on a dedicated git branch).
- Sandboxed execution of agent-written code and tests (containerized runner).
- Full session persistence: every debate turn, verdict, diff, and test run logged as structured JSONL; sessions resumable.
- Human gates via interactive CLI prompts (approve / edit / reject / ask-for-another-round).
- Cost tracking with per-session budget caps.

**Out of scope (v1, designed-for later):** web UI, more than two models per debate, parallel task execution, autonomous deployment, fine-grained per-file permissions.

## 4. Architecture Overview

```
┌────────────┐   ┌───────────────────────────────────────┐
│  duet CLI   │──▶│ Orchestrator (state machine)          │
└────────────┘   │  Phase A: DebateEngine (design)       │
                 │  Phase B: BuildEngine (code+review)   │
                 └──────┬──────────────┬─────────────────┘
                        │              │
              ┌─────────▼────┐  ┌──────▼─────────┐
              │ Provider layer│  │ Workspace layer│
              │ openai/       │  │ git branch mgr │
              │ anthropic     │  │ sandbox runner │
              └───────────────┘  └────────────────┘
                        │
              ┌─────────▼──────────┐
              │ Session store       │
              │ JSONL log · costs   │
              │ artifacts · resume  │
              └─────────────────────┘
```

Key design decisions: (1) verdicts are structured JSON, not prose parsing — every critique turn ends in a machine-readable verdict block; (2) the orchestrator is a deterministic state machine, all nondeterminism lives in model calls; (3) the sandbox is the only place agent code executes; the orchestrator never `exec`s model output.

## 5. Phases & Milestones

**M0 — Skeleton (foundation).** Repo layout, config loading, provider interface with both adapters, cost meter, JSONL session store. *Exit: both models answer a ping through one interface; tokens/cost logged.*

**M1 — Debate engine (Phase A).** Prompt templates for proposer/critic, structured verdict schema, convergence detection, round caps, deadlock → human arbitration, Gate 1 CLI flow, `IMPLEMENTATION.md` emission. *Exit: a vague one-line idea produces an agreed, human-approved spec in ≤ N rounds on 3 test ideas.*

**M2 — Build engine (Phase B).** Task decomposition from the approved spec, sandboxed workspace (new dir or cloned repo branch), coder loop with file-edit tools, reviewer loop with test-run + diff-review tools, per-task sign-off, Gate 2 flow. *Exit: an approved spec becomes a running prototype with passing tests, end-to-end, unattended between gates.*

**M3 — Existing-repo support + hardening.** Repo mapping, context budgeting for large codebases, resume-after-crash, budget kill-switch, retry/backoff, red-team pass on sandbox escapes. *Exit: adds a feature to a real mid-size repo; survives forced crash + resume; stays under budget cap.*

**M4 — Polish.** Session replay (`duet replay`), config profiles, docs, packaging (`pipx install duet`).

Build order note: M1 is usable alone (spec generator) — ship value before M2 exists.

## 6. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Models converge too easily (sycophantic agreement) | Critic prompt requires ≥1 concrete objection per round until round 2; verdict schema forces enumerated objections resolved-or-waived; disagreement score logged |
| Models never converge | Hard round cap → structured "disagreement report" → human arbitrates; arbitration is a first-class outcome, not a failure |
| Runaway cost | Per-session token budget; hard stop + resume; cost shown live in CLI |
| Sandbox escape / destructive code | Container with no network by default, repo mounted copy-on-write via git branch; denylist on destructive ops; human gate before anything merges |
| Reviewer rubber-stamps code | Reviewer must run tests and cite line-level evidence in findings; sign-off schema requires test results attached |
| Context overflow on big repos | Repo map + on-demand file reads, per-task context budget, summarized debate history after round k |
| Provider API drift | Thin adapters, contract tests per provider, model IDs only in config |

## 7. Tech Stack

Python 3.12+, `uv` for env, `openai` + `anthropic` SDKs, `pydantic` for schemas/verdicts/config, `typer` + `rich` for CLI, `GitPython` for branch management, Docker (or local subprocess fallback with explicit warning) for sandbox, `pytest` for the tool's own tests.

## 8. Success Criteria

From a one-sentence idea to an approved spec in one sitting; from approved spec to a running prototype with tests, with human touch only at the two gates; a feature added to an existing repo lands as a reviewable branch diff; every session fully replayable from its log.
