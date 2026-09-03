# Duet — Cross-Model Debate for Designs, Decisions & Code

**Project Plan · v0.4 · 2026-09-03**

*v0.4: Phase A shipped and, in use, proved to be an idea-examination tool, not only a spec generator. The debate now produces the document shape that suits the idea — an implementation spec, a decision record, or a design brief (SPECS §4.2) — and the next milestone is the web UI (§6), brought forward ahead of Phase B because the terminal is the wrong surface for reading a debate.*

## 1. Vision

A tool that takes an idea — anywhere from a vague vision to a precise brief — and drives it to a well-examined document through a structured adversarial loop between models from different labs (the small model of each by default — `--profile flagship` for the frontier pair; every model is configurable). One model proposes, the others critique and counter-propose, until they converge. A human approves the result at a hard gate.

The document's shape follows the idea: an implementation spec for software to be built, a decision record for a choice to be made, a design brief for a system to design (SPECS §4.2). When the idea is software, an approved spec is the input to a second phase that writes the code (Phase B, not yet built) behind a second gate. Duet began as a coding tool and remains an excellent one, but in use it is a general instrument for reaching a decision two independent minds have stress-tested.

Why cross-lab pairing: models from different labs have decorrelated blind spots and training biases. A GPT proposal reviewed by Claude (and vice versa) catches errors that same-model self-review reliably misses, and the disagreement signal itself is informative — persistent disagreement usually marks a real design risk worth surfacing to the human.

**Two is the default, not the ceiling.** The name is the common case; the architecture is not built around it. Participants live in a roster — a configured list of `{id, provider, model}` entries — and every protocol (proposer rotation, consensus threshold, coder/reviewer assignment, event attribution, cost accounting) is defined for N and collapses to the familiar pairwise form at N = 2. Adding a third voice for cross-validation is a config entry, and adding a lab is one adapter module plus a registry call. Nothing in the orchestration counts to two.

## 2. Core Loop (two phases, same protocol)

**Phase A — Design convergence.** Idea → one participant drafts the document (its shape set by the session's template — spec, decision record, or design brief) → every other participant critiques it independently with structured objections → the next participant in the rotation revises or rebuts → repeat until enough participants emit an explicit AGREE verdict on the same draft (all of them by default; a configurable quorum for larger rosters), or max rounds hit → human arbitrates. Output: an agreed `IMPLEMENTATION.md`. **Human gate 1: approve / edit / revise / reject.**

**Phase B — Build convergence.** *(Not yet built; the design below is the plan.)* One participant takes the coder role and implements the spec task-by-task in a sandboxed workspace (new project or existing repo). Every other participant reviews each change independently: runs tests, reads diffs, files structured findings (bug / spec-violation / style). The coder fixes the union of their findings; a task advances only when every reviewer signs off and tests pass. Which participant codes is configurable and can rotate per task. **Human gate 2: approve the working prototype / feature diff.**

The two phases share one protocol: propose → critique → revise → verdict, with round caps, convergence detection, and human escalation on deadlock. This is the central abstraction of the codebase, and it is written against a roster of N — one author, everyone else adversarial — so the same engine serves a pair today and a cross-validating panel later.

## 3. Scope

**In scope (v1):**

- Python CLI (`duet`), streaming the debate live to the terminal with per-participant attribution.
- Providers: OpenAI Responses API (`gpt-5.6-sol`) and Anthropic Messages API (`claude-opus-5`), behind a registry of adapters so models are swappable via config and a new lab is an additive module.
- Greenfield projects and existing repos (repo context via file-tree map + on-demand file reads by the agents; work happens on a dedicated git branch).
- Sandboxed execution of agent-written code and tests (containerized runner).
- Full session persistence: every debate turn, verdict, diff, and test run logged as structured JSONL; sessions resumable.
- Human gates as suspend/resume event exchanges (approve / edit / reject / ask-for-another-round), with the CLI as the first client that renders them — the engine itself never prompts.
- Cost tracking with per-session budget caps.

**Rosters larger than two** sit on the boundary by design. The data model, protocols, role assignment, and logs are N-open from M0 and unit-tested at N = 2..5, so running three or more models is a config change rather than a refactor. What v1 does *not* promise is that N > 2 is tuned: prompts, the cost/latency profile (critics scale as N-1 per round), quorum defaults, and the live view's multi-pane layout are validated for the two-participant path only. Cross-validation with a larger panel is the first thing built on this foundation, not a redesign of it.

**Deferred to V2, but designed for in v1 (§6):** the web UI, the ideation phase, and MCP in both directions. These are not v1 features, but three of them constrain v1's shape — gates, streaming, and event attribution — so v1 pays those small costs up front rather than restructuring later.

**Not in v1, and expected in V2 (§6):** parallel execution — several sessions at once in the web UI, and parallel tasks within a build loop, both of which unattended cloud runs make necessary rather than merely nice; multi-user auth and hosting (W7); fine-grained per-file permissions, which start mattering once more than one person can approve a run; and autonomous cloud agents (W8). None of these are ruled out — the v1 constraints in §6.1 exist precisely to keep them cheap.

**Out of scope by decision, not by schedule:** Duet deploying, merging, or publishing the code it writes. This one is different in kind from the list above. The session branch is handed to a human to merge, `run` denies `git push` and package publishing inside the sandbox (SPECS §7.2), and DONE never auto-merges. Autonomous *execution* is a V2 goal; autonomous *release* is a boundary that would need its own argument to move, not just a milestone.

## 4. Architecture Overview

```
┌──────────┬───────────────┬─────────────────┐   clients — the CLI is one
│ duet CLI │ web UI   (V2) │ MCP server (V2) │   of several; none owns
└──────────┴───────┬───────┴─────────────────┘   the engine
┌──────────────────▼─────────────────────────────┐
│ Orchestrator (state machine)                   │
│   Phase 0: Ideation (divergent)           (V2) │
│   Phase A: DebateEngine (design)               │
│   Phase B: BuildEngine (code+review)           │
│   gates: emit event → suspend → resume         │
│   roles resolved from the roster (N ≥ 2)       │
└────────────────────────┬───────────────────────┘
┌────────────────────────▼───────────────────────┐
│ Provider registry         Workspace layer      │
│ openai · anthropic        git branch mgr       │
│ register_provider()       sandbox runner       │
│ + MCP tool sources  (V2)                       │
└────────────────────────┬───────────────────────┘
┌────────────────────────▼───────────────────────┐
│ Session store · JSONL log · costs,             │
│ keyed by participant                           │
│                                                │
│ the projection every client reads: the CLI     │
│ renders it, the web UI streams it, the MCP     │
│ server exposes it                              │
└────────────────────────────────────────────────┘
```

Key design decisions: (1) verdicts are structured JSON, not prose parsing — every critique turn ends in a machine-readable verdict block; (2) the orchestrator is a deterministic state machine, all nondeterminism lives in model calls; (3) the sandbox is the only place agent code executes; the orchestrator never `exec`s model output; (4) the roster is a list of participants, and providers are a name-keyed registry — the engines address participants by id and ask the roster for roles, so neither the count of models nor the set of labs is wired into the protocol; (5) **human gates are events, not prompts** — the engine emits a `gate_pending` event, suspends, and resumes when a decision event arrives from *any* client, so the CLI holds no privileged position and a gate may be answered minutes or days later, from another process; (6) the session log is the integration surface — every client, present and future, reads the same projection rather than calling into the engine.

## 5. Phases & Milestones (v1)

**M0 — Skeleton (foundation).** Repo layout, config loading with the participant roster, provider registry with both adapters, role resolution, cost meter, JSONL session store. *Exit: every configured participant answers a ping through one interface; tokens/cost logged per participant.* **Done.**

**M1 — Debate engine (Phase A).** Prompt templates for proposer/critic, structured verdict schema, proposer rotation, critique merging with corroboration flags, consensus threshold, round caps, deadlock → human arbitration, `IMPLEMENTATION.md` emission.

Two requirements here exist for V2's sake and are cheap only now, while there is one engine and two adapters to change:

- **Gates suspend rather than block.** Reaching GATE_1 emits `gate_pending` and returns from the engine; the decision arrives as a `gate_decision` event carrying an `actor`, and `resume` continues from it. The CLI's pager-and-`$EDITOR` flow becomes a *client* of that mechanism, not the mechanism itself. No `input()` anywhere inside `orchestrator/`.
- **Provider streaming.** `Provider.stream()` alongside `complete()`, with incremental chunks logged as events. Without it the live view has nothing to render mid-turn, and a web UI would show a blank pane followed by a wall of text.

*Exit: a vague one-line idea produces an agreed, human-approved spec within the round cap on 3 test ideas, with the state machine passing its scripted tests at rosters of both 2 and 3; a session suspended at Gate 1 resumes correctly in a separate process from the one that started it.* **Met.** Scripted tests pass at N = 2 and N = 3, cross-process resume is verified, the contract suite passes against both providers live, and the golden suite converges on all three briefs within the round cap.

Beyond the original scope, real use drove several additions now part of M1: `--context` files supplied to every participant at every stage; per-participant reference material treated as fixed, not debatable; a decision-sized summary and one-line objection headlines so a gate can be judged without reading the whole document; convergence and objection-churn reporting that distinguishes a debate making progress from one manufacturing it; `duet review` to put an edited document back for one critique round; a budget stop that suspends and resumes rather than ending; and document templates (SPECS §4.2) so the debate is not confined to software. The prompt set is at `phase-a/5`; the golden suite is the regression baseline for changing it.

Getting there took one prompt change worth recording, because it inverted an assumption. Under `phase-a/1` the *most constrained* brief was the one that failed — deadlocked at the round cap while both vaguer briefs converged in three rounds. The cause was not difficulty but scope: the proposer was told to fill every section and write concretely, with nothing pulling the other way, so it invented subsystems the brief never asked for, and each invention became surface for the critics. Critics then compounded it, since every objection was "this is underspecified", which pushes the proposer to specify *more*. `phase-a/2` adds a minimality rule to the proposer and a scope-before-detail rule to the critic — if a component is not required, the objection is that it exists, not that it is thin.

| brief | phase-a/1 | phase-a/2 |
|---|---|---|
| vague | GATE_1, 3 rounds, $0.082, 26k spec | GATE_1, 4 rounds, $0.063, 10k spec |
| moderate | GATE_1, 3 rounds, $0.059, 21k spec | GATE_1, 3 rounds, $0.042, 9k spec |
| specific | **deadlock at round 8**, $0.250 | GATE_1, 3 rounds, $0.051, 11k spec |

Specs came out roughly half the size and denser — the long versions were inflated, not richer. One caveat on reading this: it is a single run of a stochastic system, and the suite exists precisely so the next prompt change is judged the same way rather than by argument.

**M2 — Build engine (Phase B).** Task decomposition from the approved spec, sandboxed workspace (new dir or cloned repo branch), coder loop with file-edit tools, reviewer loop with test-run + diff-review tools, finding union across reviewers, per-task sign-off, Gate 2 flow. *Exit: an approved spec becomes a running prototype with passing tests, end-to-end, unattended between gates.*

**M3 — Existing-repo support + hardening.** Repo mapping, context budgeting for large codebases, resume-after-crash, budget kill-switch, retry/backoff, red-team pass on sandbox escapes. *Exit: adds a feature to a real mid-size repo; survives forced crash + resume; stays under budget cap.*

**M4 — Polish.** Session replay (`duet replay`), config profiles, docs, packaging (`pipx install duet`).

**M5 — Cross-validation panels (post-v1).** Tune what M0–M4 already permit: prompts that read well with several critics, a live view that shows N streams without becoming soup, quorum defaults measured against unanimity, cheap-screening tiers so a panel does not cost N× a pair, and measurement of whether same-lab participants produce correlated objections (SPECS §13.5) — which can only be measured here, since corroboration does not exist at N = 2. No structural work is expected here — if this milestone needs a refactor, the N-openness built in M0 failed and that is worth knowing early.

Build order, as it actually went: M1 shipped and is usable alone. Using it revealed two things — that the output is a document of many kinds, not only a spec (addressed by templates, SPECS §4.2), and that reading a ten-objection debate in a terminal is genuinely hard. So the next milestone is **the web UI (§6, W1–W2)**, brought forward ahead of Phase B: the groundwork (gates as events, log as projection, per-turn summaries and one-line objection headlines) is already in place, so it is mostly new surface rather than new engine. Phase B follows for the software case.

## 6. V2 — Web UI, Ideation, MCP

V1 is a terminal tool that turns one idea into one prototype, with a human present throughout. V2 turns Duet into a workspace: projects and ideas visible side by side, debate and build steps watchable as they happen, approvals clicked rather than typed, ideas *generated* with the models instead of supplied whole, Duet addressable over MCP in both directions — and, at the end of the arc, sessions that run unattended in the cloud and come back when they need a decision.

**The event log is the API.** Everything the UI displays is a projection of the session log (SPECS §10): the project list is a scan of session directories, the timeline is the event stream, approval status is the state machine's current state, and cost is the running sum of `cost_tick`. The web backend adds HTTP and auth around that projection; it does not become a second source of truth. This is why V2 needs no storage redesign — v1's event sourcing already is the backend.

### 6.1 What v1 must get right (and does, from M1)

| Constraint | Why it cannot wait |
|---|---|
| Gates emit an event and suspend; decisions arrive from any client | A browser approval lands hours later in a different process. Inline prompts would mean rewriting both engines. |
| `gate_decision` carries an `actor` | Multi-user approval needs attribution, and back-filling identity into an append-only log is not possible. |
| Provider exposes streaming | Otherwise the UI shows a blank pane, then a wall of text. Cheapest with two adapters. |
| Paths stay session-relative | A hosted UI means a remote sandbox; absolute local paths would leak into the log format. |
| Roles and events key on participant id | The UI attributes every turn, and the roster changes between a run and its replay. Already true as of M0. |
| Sessions record an `owner` at creation | Same argument as `actor`, one level up: a session created without an owner is anonymous forever. In v1 the owner is the local user and the filesystem makes it moot; the first shared deployment needs it and cannot invent it retroactively. Done in M1. |
| Session ids are full-length | Ids are `uuid4` hex rather than a truncation, so a session id is never the weak link if it reaches a URL, and `resume` takes any unambiguous prefix so they stay typable. Widening is free now and a breaking change later. Done in M1. |

### 6.2 Milestones

**W1 — Headless core.** Engines run with no terminal attached: a session driver callable as a library or a job, gate suspension exercised across process boundaries, structured progress emitted rather than printed. Adds the lease table and a worker loop that claims sessions, heartbeats, and releases at suspension (§6.3.6). Mostly the payoff of M1's constraints. *Exit: a full session runs to Gate 1, exits, and resumes from a different process; killing a worker mid-round loses no events and the session is picked up by another.*

**W2 — Read-only web UI.** Project and idea list, session timeline with per-participant attribution, verdicts and objection ledgers, diffs, test reports, live cost. Streams the event log over SSE. No mutation — everything here is a projection, which makes it a genuinely small first release. *Exit: a session started from the CLI is fully watchable in the browser, live.* **Shipped, as `duet web`** — a single local FastAPI service (not the Next.js split; that stays the hosted path), streaming the log over SSE with a no-build vanilla frontend. It also answers gates (core W3): a debate started in the browser runs to a gate and is approved / revised / rejected there. What remains for full W3 is auth and session ownership for shared use, and edit-in-browser / per-objection arbitration rulings.

**W3 — Gates in the browser.** Approve, edit, revise and reject from the UI, writing `gate_decision` events with an actor. Session creation moves here too, and with it the first roster decision: **starting a session picks a named profile, not individual models.** v1 already has the mechanism — `~/.duet/profiles/<name>.toml` overlays, where a profile declaring `[[participants]]` replaces the roster wholesale (SPECS §9) — so the UI offers one dropdown of profiles ("default", "cheap", "trio") and puts per-participant editing in settings, where the people who want it will look. Per-run model pickers are cheap to build and wrong to default to: they ask users to make a configuration decision at the moment they are least interested in one, and they make runs incomparable, since a disappointing result can no longer be attributed to the idea rather than the lineup. Requires the first real auth and session-ownership model. *Exit: a session runs start to finish with no terminal, gates included.*

**W4 — Ideation (Phase 0).** A new state upstream of DESIGN_DEBATE, and the largest genuinely new product surface in V2 — everything else re-presents work v1 already does. The human steers with prompts, likes, and constraints; a chosen candidate becomes a session's input brief, with the human's stated constraints carried in as `FIXED:` items (SPECS §4.2). *Exit: a user with no starting idea leaves with a spec they approved.*

**Phase 0 is explicitly not adversarial.** It reuses the roster but not the debate protocol, because the two phases apply the same premise in opposite directions: Phase A uses decorrelated blind spots to find flaws one model would have missed, while Phase 0 uses them to reach regions of the idea space one model would never have proposed. Critique serves the first and actively damages the second — a critic in the room narrows the option set before it is wide enough to be worth narrowing. Concretely:

- **Diverge independently.** Each participant generates candidates from the seed and context without seeing the others', so diversity comes from independence rather than from disagreement. Candidates are structured (`{title, premise, who_for, why_now, riskiest_assumption, rough_effort}`), which makes them comparable without ranking them.
- **Cross-pollinate constructively, if at all.** An optional second pass lets each participant extend, combine, or riff on the pooled candidates, and forbids rejecting one. "Yes-and", not "no-but".
- **Critique annotates, never eliminates.** Any feasibility note attaches to a candidate as advisory metadata the human can read past. Pruning belongs to the human here, and to Phase A after that — where adversarial pressure is exactly right and already specified.
- **Candidates are blind while being chosen, attributed once chosen.** The human sees the pooled candidates without author labels, picks on merit, and only then learns who wrote what. This keeps the halo effect out of the choice while preserving the thing attribution is actually good for — learning over time which participant suits which kind of problem. Blinding is a presentation rule and nothing more: the event log records authorship on every candidate as it always does (§6.1), so replay, cost attribution, and the reveal itself all read from the same record. It applies to ideation candidates during selection and nowhere else — debate turns, verdicts, and reviews stay attributed on sight, since there the whole point is knowing who said it.
- **The orchestrator guard inverts.** The debate's anti-sycophancy floor rejects early AGREE to force objections; Phase 0's failure mode is the mirror image — every participant converging on the same safe idea — so the equivalent guard is a *diversity floor* that rejects near-duplicate candidates and requires them to differ along stated dimensions. The mechanism carries over (orchestrator-enforced, not prompt-enforced) even though the content inverts.
- **The diversity floor uses both checks, because they catch different things.** Embedding distance is computed over every candidate pair first: it is cheap, deterministic, ungameable, and good at lexical near-duplicates, but it reads wording more than meaning. Pairs comfortably far apart are accepted and pairs nearly identical are rejected, both without a model call. Only the ambiguous middle band escalates to a model judge, which is the check that catches one idea restated in different vocabulary. Two consequences worth designing to: cost stays bounded because only borderline pairs are ever judged, and the judge must be a participant that authored neither candidate (or the `[utility]` model) — asking a model whether it converged with its peers is the circularity this guard exists to prevent. Both thresholds are config, since they will need tuning against real candidate pools.

No verdict schema, no consensus threshold, no round cap in the debate sense. Phase 0 ends when the human picks, not when the models agree.

**W5 — MCP client.** Participants gain tools from configured MCP servers (`[[mcp_servers]]`) alongside the built-in `read_file` / `run` set, resolved into the existing `ToolDef` / `ToolCall` types. Per-server trust levels, and tool results enter the loop strictly as data. *Exit: a build task completes using a tool served over MCP, sandbox guarantees unchanged.*

**W6 — MCP server.** Duet exposes its own operations as MCP tools — `list_projects`, `get_session`, `new_session`, `get_diff`, `approve_gate` — so other agents and clients can drive it. Reads are log projections; writes go through the same suspend/resume gate machinery the browser uses, which is the payoff of W1: the MCP server is simply a third client, not a parallel implementation. *Exit: an external MCP client starts a session, watches it, and approves Gate 1.*

**W7 — Hosted and multi-tenant.** Remote or per-tenant sandboxes, per-user session isolation, and the per-file permission granularity that shared runs need. Two credential paths (§6.3.4). By default users bring their own provider keys: stored encrypted under per-user keys, injected into the orchestrator process for the duration of a run, never sent to a sandbox or a browser, never logged, revocable at any time. The paid tier instead spends Duet's own provider accounts, which adds prepaid credits, server-side hard caps, usage reconciled against provider-reported figures rather than the local `[pricing]` table, and abuse controls on accounts that are now a resellable resource. The point at which the threat model genuinely changes; scoped separately once W1–W6 land.

**W8 — Autonomous cloud agents.** Sessions that run start to finish with nobody attached: launched from the UI or over MCP, executing Phase A and Phase B unattended, and surfacing a gate as a notification rather than a blocked terminal. Mostly the compounding payoff of earlier work — W1 made the engine headless, W3 made gate decisions asynchronous and attributable, W7 put the sandbox somewhere durable — plus the two genuinely new pieces: parallel execution (concurrent sessions, and parallel tasks within a build loop once `depends_on` is respected) and a supervision model covering budget caps, wall-clock stops, and thrash detection with no human watching. *Exit: a session launched from the browser runs overnight, stops itself at Gate 1, and notifies its owner.*

### 6.3 Decisions taken

Recorded with their reasoning, because these are the ones that get re-litigated later.

1. **Ideation candidates are blind while being chosen and attributed once chosen** (W4). Judging on merit matters more at the moment of choice than knowing the author; knowing the author matters more afterwards, when the useful lesson is which participant suits which kind of problem. Blinding is presentation only — the log always records authorship.
2. **The diversity floor combines both checks** (W4). A computed embedding pass runs over every pair and settles the clear cases for free; only the ambiguous band reaches a model judge, which is the check that catches one idea restated in different words. The judge must have authored neither candidate.
3. **Browser-started sessions choose a named profile, not individual models** (W3). v1's profile overlays already do this; per-run model pickers ask for a configuration decision at the worst possible moment and make runs incomparable.
4. **Both key models, with bring-your-own as the default and brokered access as a paid upgrade** (W7). Most users supply their own provider keys and pay their provider directly; users who would rather not — or who have no API keys at all — can buy metered access through Duet's own provider accounts.
5. **Sessions carry an owner from creation; identity is passwordless; the account is demanded at the ideation→debate boundary** (W3, W4). Ownership is a field on the session, written when it is created, and every multi-user question — list my ideas, resume my session, who may answer this gate — is a query against it rather than a separate access-control system. Identity itself is OAuth or an email magic link, never a password: the cheapest secret to protect is one Duet never holds, which is the same reasoning as §6.3.4's stance on key custody.

6. **Next.js for the UI, FastAPI for the engine, and runs are leased jobs rather than HTTP requests** (W1, W2). The web backend *imports* the engine the CLI already drives; it does not reimplement it. Next.js owns the UI and acts as a backend-for-frontend — auth, sessions, request shaping — and FastAPI owns session operations and the SSE event stream. Two services is the honest cost, paid because runs must outlive requests anyway.

The job model falls out of what a run actually is. A debate is not one long task: it runs for minutes, suspends at a gate, and waits indefinitely — possibly days — for a human. So the unit of work is **a lease on a session, not a task with a payload**. A worker claims a session, advances the machine until it suspends or terminates, appends events as it goes, and releases. There is no payload to carry because the event log already holds the state, and no bespoke resume logic because replay is how the machine starts anyway. A gate decision simply makes the session claimable again.

Three properties this has to preserve, all of them ones the log already gives us:

- **Single writer per session.** Exactly one worker may append to a session's log at a time; two would interleave rounds and double-spend. The lease is what enforces it, with a heartbeat and an expiry comfortably longer than the slowest single turn.
- **Crash recovery is just resumption.** A dead worker's lease expires and another picks the session up. Correct by construction, because the machine reconstructs from events and appends are atomic — this is the dividend of event sourcing, not new machinery.
- **Never blind-retry a run.** Transient API failures are already retried inside the engine (429/5xx with backoff); a *run* that crashes must resume from the log rather than restart, and a session that crashes repeatedly is parked after a few attempts rather than retried forever. Every attempt costs real money, so at-least-once delivery without a guard is a spending bug, not just a duplicate.

**Use Postgres with `SELECT … FOR UPDATE SKIP LOCKED` for the queue, not Redis or Celery.** Accounts, ownership, credits and billing need a relational store regardless, so this adds no component. More importantly it makes enqueueing transactional with the rest of the data: for the brokered tier, debiting credits and claiming a run must commit together or you bill for runs that never happen. A dedicated broker is easy to adopt later if throughput ever demands it; discovering you needed transactions after building on Redis is not. Jobs here are few, long, and expensive — the profile a database queue handles comfortably.

The CLI keeps running the engine inline with no queue at all, the way bring-your-own keys stays the reference path for credentials: the queue is a deployment concern, and the engine must never know it exists.

**On when to demand an account**, W4 supplies a natural boundary. Ideation is cheap, divergent, and pre-session; the debate is where both real spend and real durability begin. So a visitor may ideate anonymously against a device-scoped token, and is asked for an account at the moment a chosen idea becomes a session — simultaneously the first meaningful cost, the first thing worth retrieving later, and the point where they have already seen what Duet does. Anonymous ideation is claimed onto the account at that transition. This keeps the trial genuinely frictionless without pretending brokered spend can be anonymous.

The key-model decision shapes what Duet is, so its consequences are worth stating:

- **Bring-your-own stays the default, and the reference path.** It is what self-hosting and local runs use, it needs no billing, and it is what every feature is developed against. Brokered access is an alternative route to the same engine, never a richer one — no capability is reachable only by paying Duet.
- **The brokered tier removes the credential wall, not the account wall.** Two different walls are easy to conflate. The *credential* wall is needing OpenAI and Anthropic accounts with provisioned keys before anything runs at all; brokering removes it, which is what makes it a growth path rather than a revenue experiment. The *account* wall is needing an identity inside Duet so sessions are yours and findable later — brokering does not remove that, it **requires** it, since anonymous spend cannot be metered or billed. Brokered access is therefore the tier that forces the account model into existence, not the one that avoids it.
- **Two key-handling paths is the accepted cost.** Flagged when the question was first raised: cheap to design in now, expensive to retrofit. The seam is narrow — both paths end at the same place, provider credentials present in the orchestrator process for the duration of a run and nowhere else — so what differs is only where the credential came from and who gets billed for what it spends.
- **Brokered access makes the cost meter billing-critical.** In v1 the `[pricing]` table is best-effort and user-overridable, because it only ever informs the person spending their own money. For brokered runs it decides what Duet charges and what Duet absorbs, so that tier must reconcile against provider-reported usage rather than trusting a local table, and it needs prepaid credits or a hard server-side cap instead of the advisory `budget.max_usd` — a run that overshoots its cap costs the user nothing and Duet everything.
- **Key custody remains the sharpest obligation either way.** Holding users' provider keys means: encrypted at rest under per-user keys, never rendered back to a browser, never written to the session log, never passed into a sandbox (SPECS §7.2, unchanged), revocable by the user, and scoped keys where a provider supports them. Self-hosting stays first-class so that handing keys to anyone is never the only option.

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Models converge too easily (sycophantic agreement) | Critic prompt requires ≥1 concrete objection per round until round 2; verdict schema forces enumerated objections resolved-or-waived; disagreement score logged |
| Ideation converges on one safe idea — the same failure, inverted | Candidates generated independently before any participant sees another's; an orchestrator-enforced diversity floor rejects near-duplicates the way the debate's objection floor rejects early AGREE; critique in Phase 0 annotates and never eliminates (§6.2 W4) |
| Models never converge | Hard round cap → structured "disagreement report" → human arbitrates; arbitration is a first-class outcome, not a failure |
| Runaway cost | Per-session token budget; hard stop + resume; cost shown live in CLI |
| Sandbox escape / destructive code | Container with no network by default, repo mounted copy-on-write via git branch; denylist on destructive ops; human gate before anything merges |
| Reviewer rubber-stamps code | Every reviewer must run tests and cite line-level evidence in findings; sign-off schema requires test results attached; a task needs *all* assigned reviewers to sign off, so one dissenter blocks |
| Context overflow on big repos | Repo map + on-demand file reads, per-task context budget, summarized debate history after round k; verbatim history degrades to the objection ledger as N-1 critiques per round grow |
| Provider API drift | Thin adapters, contract tests per provider, model IDs only in config |
| Two-model assumptions calcify, making cross-validation a rewrite | Roster is a list from M0 with no pairwise shortcuts; roles resolve through `roles_for_task`; events key on participant id, not provider; protocol tests run at N = 2 and N = 3 with two participants sharing a provider |
| Larger rosters cost and take N× longer | Critics and reviewers scale as N-1 per round: quorum consensus, a narrowable `build.reviewers` set, per-participant model tiers, and the existing budget cap — measured in M5 before any default changes |
| Same-lab participants give correlated critiques, diluting the decorrelation premise | Corroboration flags in the objection ledger make overlap measurable — but only at N ≥ 3, since a two-participant round has a single critic and nothing to corroborate against (SPECS §4.1); cross-lab pairing stays the default roster |
| V2's web UI forces an engine rewrite | Gates suspend instead of blocking and providers stream, both from M1; the CLI is a client of the same mechanism the browser will use, so it is exercised from day one rather than discovered late |
| **MCP tool results carry prompt injection into an agent that writes files and runs commands** | Tool results enter the loop as data, never as instructions; the sandbox boundary and `run` denylist are unchanged by tool source; per-server trust levels; a coder's changes still face N-1 reviewers and a human gate before anything merges |
| A hosted UI changes the threat model from "my machine" to multi-tenant | W7 is scoped separately and after the rest; session ownership and gate actors exist from W3 |
| Custody of users' provider keys — the highest-value asset in a hosted deployment | Encrypted at rest under per-user keys, injected only into the orchestrator process for a run's duration, never sent to a sandbox or browser, never written to the log, revocable, scoped where the provider supports it; self-hosting stays first-class so handing over keys is never the only option (§6.3.4) |
| Brokered tier turns Duet's provider accounts into a resellable resource, and its float into a liability | Prepaid credits and a hard server-side cap rather than the advisory `budget.max_usd`; usage reconciled against provider-reported figures, not the local pricing table; rate limits and abuse detection on an account someone could otherwise use as a cheap API proxy; bring-your-own remains the default so the exposed surface stays small |
| Ideation (W4) quietly becomes a second product | It is one state upstream of the existing machine, feeding the same brief format `duet new` already accepts — if it needs its own engine, that is the signal to scope it separately |
| Unattended runs (W8) burn budget or thrash with nobody watching | The existing auto-stops — `budget.max_usd`, `budget.max_wall_minutes`, and the three-identical-failures thrash detector — become load-bearing rather than advisory, and every stop must leave a resumable session plus a notification; a cloud run that cannot be resumed after an auto-stop is a bug, not a budget saving |
| Parallel tasks (W8) interleave writes to one session branch | Parallelism is gated on `depends_on` being honoured, one workspace per concurrent task, and commits serialised at task sign-off — the granular per-task commit already specified in SPECS §5.4 is what makes this tractable |

## 8. Tech Stack

Python 3.12+, `uv` for env, `openai` + `anthropic` SDKs, `pydantic` for schemas/verdicts/config, `typer` + `rich` for CLI, `GitPython` for branch management, Docker (or local subprocess fallback with explicit warning) for sandbox, `pytest` for the tool's own tests.

V2 adds an ASGI backend (FastAPI) serving the log projection over SSE, a Next.js frontend and BFF handling auth, Postgres for accounts, ownership, credits and the job-lease table, the official MCP Python SDK on both the client and server side, and worker processes that lease sessions and advance them. The frontend stays deliberately thin — it renders events and posts decisions, holding no state the log does not already have. Nothing in this list changes v1's dependencies; the backend imports the same engine the CLI does.

## 9. Success Criteria

From a one-sentence idea to an approved spec in one sitting; from approved spec to a running prototype with tests, with human touch only at the two gates; a feature added to an existing repo lands as a reviewable branch diff; every session fully replayable from its log.

For V2: a session started in one place is watchable and approvable in another — terminal, browser, or MCP client — because all three read the same log and answer gates the same way; and a user arriving with no idea at all leaves with one they chose and a spec they approved.
