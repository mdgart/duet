# Duet — Technical Specification

**v0.3 · 2026-08-29 · companion to PLAN.md**

*v0.2: the two-model pair generalised to an N-participant roster — participants, provider registry, proposer rotation, consensus thresholds, reviewer sets (§1, §3, §4.1, §4.3, §5.3, §5.6, §9).*
*v0.3: gates become a suspend/resume event exchange and providers gain streaming, so the CLI is one gate client among several (§2, §3, §6.0, §10) — groundwork for the V2 web UI and MCP server in PLAN §6.*

## 1. Definitions

- **Session**: one end-to-end run, idea → prototype. Has a UUID, a directory under `~/.duet/sessions/<id>/`, and a JSONL event log.
- **Provider**: an API adapter for one lab (`openai`, `anthropic`, …). Adapters live in a registry; adding a lab is one module plus one `register_provider` call.
- **Participant**: one voice in the loop — a unique `id` bound to a provider, a model id, and a CLI badge. Roles, events, and verdicts are keyed by participant id, never by provider name, so two participants may share a provider on different models.
- **Roster**: the ordered list of participants (`[[participants]]` in config). Length ≥ 2; two is the default, not a structural limit. Every protocol below is defined for N and degenerates to the familiar pairwise form at N = 2.
- **Debate**: a bounded propose/critique/revise exchange among the roster, ending in consensus, deadlock, or budget stop.
- **Verdict**: a structured JSON block every critique turn must end with (schema §4.3).
- **Gate**: a blocking human decision point. Gate 1 = spec approval, Gate 2 = code approval. Nothing crosses a gate without explicit human input.
- **Coder / Reviewers**: Phase B roles. One coder per task; every other participant reviews unless `build.reviewers` narrows the set. Assignment is configurable and optionally rotates per task.

## 2. State Machine

```
INIT → DESIGN_DEBATE → GATE_1 → TASK_PLANNING → BUILD_LOOP → GATE_2 → DONE
         │                │            │             │           │
         ├─ DEADLOCK ─────┤ (human     │             ├─ DEADLOCK ┤
         └─ BUDGET_STOP    arbitrates) └───────── BUDGET_STOP / FAILED
```

Transitions are logged events. Every state is resumable: `duet resume <session-id>` reconstructs state from the event log (event-sourced; no separate mutable state file).

The gate states are *suspension points*, not pauses: on reaching GATE_1 or GATE_2 the engine emits `gate_pending` and returns. The decision arrives later as a `gate_decision` event — from the CLI, or in V2 from a browser or an MCP client — and `resume` continues from it. The engine therefore contains no interactive prompts; a gate may be answered days later, by a different person, in a different process.

- **INIT**: load config, validate API keys, create session dir, snapshot repo (if existing-repo mode: create branch `duet/<session-id>` from current HEAD).
- **DESIGN_DEBATE**: Phase A protocol (§4). Exit on consensus → GATE_1; on round cap → DEADLOCK → human arbitration (§6.3).
- **GATE_1**: human approves/edits/rejects `IMPLEMENTATION.md` (§6.1).
- **TASK_PLANNING**: the roster jointly decomposes the approved spec into an ordered task list (mini-debate, max 2 rounds), each task with acceptance criteria. Human may reorder/trim (soft gate: default-accept after display).
- **BUILD_LOOP**: per-task coder/reviewers loop (§5). All tasks signed off → GATE_2.
- **GATE_2**: human reviews final diff + test report (§6.2).
- **DONE**: session summary written; branch left for human to merge (never auto-merged).

## 3. Provider Layer

```python
class Provider(Protocol):
    name: str                     # registry key: "openai", "anthropic", …
    model_id: str
    def complete(self, req: ModelRequest) -> ModelResponse: ...
    def stream(self, req: ModelRequest) -> Iterator[Chunk]: ...  # M1

class ModelRequest(BaseModel):
    system: str
    messages: list[Turn]          # role: "user" | "assistant"
    tools: list[ToolDef] | None   # Phase B only
    response_schema: type[BaseModel] | None  # forces structured output
    max_output_tokens: int

class ModelResponse(BaseModel):
    text: str
    parsed: BaseModel | None      # validated against response_schema
    tool_calls: list[ToolCall]
    usage: Usage                  # input/output tokens, cached tokens, cost USD
```

Adapters are registered by name in a dict, not selected by `if/elif`:

```python
ProviderFactory = Callable[[ModelSpec, DuetConfig], Provider]
register_provider(name: str, factory: ProviderFactory) -> None
build_provider(spec: ModelSpec, config: DuetConfig) -> Provider   # participant or utility model
provider_for(config: DuetConfig, participant_id: str) -> Provider
```

Shipped adapters: `OpenAIProvider` (Responses API) and `AnthropicProvider` (Messages API); SDK imports are lazy, so an unused lab's SDK is never loaded. `build_provider` takes a `ModelSpec {provider, model}` — a `Participant` is a `ModelSpec` with an id and badge, and utility models (§9 `[utility]`) reuse the same path. An unregistered provider name raises with the registered names listed.

`stream()` yields incremental chunks and logs them as events, so any client — the CLI live view, and later a browser or MCP client — can render a turn as it happens rather than after it completes; `complete()` is defined as draining `stream()`. Adapters that cannot stream a given request (structured-output modes on some APIs) may fall back to emitting one terminal chunk, and say so in the event.

Two adapter-level failures are surfaced as their own types rather than left to fail obscurely downstream. **Truncation** — the model hitting its output cap mid-answer — reaches the parser as an "unterminated string" that reads like a broken model rather than a limit to raise, so adapters detect it (OpenAI `status: incomplete`, Anthropic `stop_reason: max_tokens`) and raise `OutputTruncated`; the repair round then asks for a *shorter* answer, since re-emitting the same content at the same cap truncates identically. **Schema dialect** — OpenAI strict mode accepts a narrower JSON Schema than pydantic emits, requiring `additionalProperties: false` and every property in `required`, and rejecting the numeric and string constraint keywords; the adapter rewrites the schema on the way out, losing nothing because pydantic still enforces the constraints when validating the reply.

Structured outputs use each API's native JSON-schema mode — for OpenAI the Responses `json_schema` format, for Anthropic a single forced tool whose `input_schema` is the target schema, so the model fills a tool input instead of writing JSON as prose; on validation failure, one automatic repair round ("your output failed validation: <errors>; re-emit"), then hard error. Retries: exponential backoff, 5 attempts, on 429/5xx. Every call and response is an event in the session log, tagged with the participant id (prompts included — full replayability).

## 4. Phase A — Design Debate

### 4.1 Roles and turn order

Round r: one **proposer** emits/revises the spec draft → every **other participant** critiques it independently, each emitting a critique + verdict. The proposer rotates round-robin through the roster: `roster.proposer_for_round(r)` = `participants[(r - 1) % N]`. Rotation prevents ownership bias and makes "agreement" mean every participant has authored the document at least once.

At N = 2 this is exactly the alternating propose/critique protocol (r1: A proposes, B critiques; r2: B proposes the revision, A critiques). At N > 2 the critics of a round are independent — each sees the draft and the objection ledger, not each other's in-flight critique, so their objections stay decorrelated. The orchestrator merges the round's critiques into one ledger, deduplicating by `(section, claim)` and keeping the highest severity when several critics raise the same point; a point raised independently by multiple participants is flagged as corroborated in the ledger and surfaced at Gate 1.

**Corroboration measures agreement, not verification.** Two participants reaching the same objection have concurred; they have not checked each other. Observed live: a corroborated objection carried a materially wrong physical constant, restated unchecked by the second model, and the flag presented that shared error as strength. Corroboration is therefore a signal about *salience* — this point occurred to more than one reader — and never evidence that the point is right. Gate 1 should present it that way, and a human arbitrating a corroborated objection is checking a claim, not ratifying a vote.

**Corroboration needs N ≥ 3, and the default roster is N = 2.** A round has exactly one proposer and N-1 critics, so at N = 2 there is a single critic per round and no objection can be corroborated within a round at all. It remains possible across rounds — the participants swap roles, so the former proposer may later raise a point the other already made — but only on an exact match of the normalised `(section, claim)` key, which real critics do not produce: in an observed 8-round two-participant debate, 32 objections yielded 32 distinct keys and zero corroborations.

Two consequences worth stating plainly rather than discovering later. Corroboration is a **panel feature**: it is one of the concrete reasons to run three or more participants, not a property of the default pair, and Gate 1 on a two-participant session should be expected to show no corroborated objections rather than treated as a bug. And any analysis that depends on corroboration counts — notably the same-lab correlation question in §13.5 — cannot be run on a two-participant roster at all, because the measurement it needs is structurally unavailable there.

### 4.2 Proposer output — spec draft

Markdown, filling the section skeleton of the session's **template** (enforced by structured output). The template is chosen at `duet new --template` and fixed for the session; `software` (the default) is the implementation-spec skeleton below, and `decision` and `design` supply their own sections for non-code debates. The debate protocol is identical across templates — only the section set and the document's noun change. The structured-output model is generated per template, so every section is a required field the proposer cannot silently drop.

The software skeleton:

`Problem statement · Goals / Non-goals · User-facing behavior · Architecture & key components · Data model · External dependencies · Task-level milestones · Acceptance criteria · Open questions`

For vague ideas, the proposer must fill gaps with explicit stated assumptions (marked `ASSUMPTION:`) rather than asking the human; assumptions are surfaced at Gate 1. For highly specific input briefs, the proposer must preserve the human's stated decisions verbatim and mark them `FIXED:` — the critic may flag but not overturn them; only the human can change a FIXED item at Gate 1.

### 4.3 Critic output — verdict schema

```json
{
  "verdict": "AGREE" | "REVISE" | "OBJECT",
  "objections": [
    {
      "id": "OBJ-3",
      "severity": "blocking" | "major" | "minor",
      "headline": "one line, ~12 words — what a human scans",
      "section": "Data model",
      "claim": "...",
      "evidence": "...",
      "proposed_change": "..."
    }
  ],
  "resolved_objections": ["OBJ-1"],
  "waived_objections": [{"id": "OBJ-2", "reason": "..."}],
  "agreement_score": 0.0-1.0
}
```

Rules enforced by the orchestrator, not the prompt alone:

- Rounds 1–2 (`debate.min_objection_rounds`): verdict AGREE is rejected by the orchestrator (anti-sycophancy floor); each critic must produce ≥1 objection with concrete evidence.
- AGREE is valid only when every previously raised `blocking` and `major` objection appears in `resolved_objections` or `waived_objections`.
- **Consensus** = enough participants have issued AGREE on the *same draft hash*. The threshold is `debate.required_agreements(N)`: N under `consensus = "unanimous"` (default), else `max(2, ceil(quorum × N))` capped at N. Because a draft hash pins the text, an AGREE is invalidated by any later revision; participants who have not yet voted on the current hash get a confirmation turn on the unchanged draft.
- Non-unanimous consensus never silently buries dissent: any participant still holding a `blocking` objection when a quorum closes the debate has it recorded in the spec's minority-report section and shown at Gate 1.
- **Deadlock** = round cap hit (default 8); the same objection ID reopened `reopen_limit` times (default 3); or no progress for `stall_rounds` rounds (default 4).
- The two stall detectors catch different failures and neither subsumes the other. The reopen limit catches a debate stuck arguing one point. `stall_rounds` catches the aggregate case — every round closes real objections and raises equally many new ones, so nothing ever repeats, the ledger grows without bound, and the reopen limit never fires. Progress is defined as driving open `blocking`/`major` objections to a new low; a debate that cannot manage that within the window is not converging, however busy each round looks. Observed live before this existed: 32 objections over 8 rounds, 32 distinct dedup keys, zero reopens.
- The per-round count of open blocking/major objections is retained as the convergence trace and rendered in the disagreement report, because it distinguishes the two outcomes arbitration must choose between: a falling trend means the cap was too low and more rounds will likely finish, a flat or rising one means more rounds buy nothing.

### 4.4 Context management

Full draft always in context. Debate history: last 2 rounds verbatim; older rounds replaced by an objection-ledger summary (id, status, raised-by, one-line resolution) maintained deterministically by the orchestrator from verdict JSON — never by model summarization.

Verbatim history is per-round, not per-participant, so it grows with N: at N > 2 the "last 2 rounds" window holds 2 × (N-1) critiques. When that window exceeds `build.context_budget_tokens`, the orchestrator drops to last-1-round verbatim and falls back to ledger-only, oldest first — the ledger is always retained, since it is the deterministic record.

## 5. Phase B — Build Loop

### 5.1 Task structure

From TASK_PLANNING: `Task {id, title, description, acceptance_criteria[], depends_on[], status}`. Executed sequentially in dependency order (v1: no parallelism).

### 5.2 Coder turn

Coder receives: approved spec, current task, repo map (§5.5), prior review findings. Tools (function-calling): `read_file(path)`, `write_file(path, content)`, `apply_patch(diff)`, `list_dir(path)`, `run(cmd)` — all executed **in the sandbox only** (§7). Turn ends when the coder emits `TASK_READY {summary, files_touched[], how_to_test}`. Guardrails: max 25 tool calls per turn; `run` commands pass a denylist filter (§7.2) even inside the sandbox.

### 5.3 Reviewer turns

Every participant that is not the coder reviews the task, independently and in parallel — this is the cross-validation step, and at N = 2 it is a single reviewer. Each reviewer receives: task + acceptance criteria, unified diff since last sign-off, `how_to_test`. Each **must** call `run` for the test suite at least once — a sign-off without an attached test-run event is rejected by the orchestrator. Read-only tools plus `run`; reviewers cannot edit files. Reviewers do not see each other's findings for the same cycle. Output schema, one per reviewer:

```json
{
  "verdict": "SIGN_OFF" | "REJECT",
  "findings": [
    {"id": "F-2", "type": "bug|spec_violation|test_gap|style",
     "severity": "blocking|major|minor",
     "file": "...", "line": 42, "detail": "...", "suggested_fix": "..."}
  ],
  "tests": {"command": "...", "passed": 17, "failed": 0, "log_ref": "evt-1234"}
}
```

A reviewer's SIGN_OFF requires: zero blocking/major findings open from that reviewer, tests passing, acceptance criteria checklist all true. **A task is signed off only when every assigned reviewer signs off** — one REJECT sends the task back, regardless of how many others approved. Unlike debate consensus, this is unanimous by construction and not quorum-configurable: a reviewer holding a blocking finding is exactly the signal the loop exists to surface.

### 5.4 Loop control per task

Any REJECT → the union of all reviewers' findings goes back to the coder in one fix turn → re-review by the same reviewer set. Findings are deduplicated across reviewers by `(file, line, type)`; a finding raised independently by several reviewers is marked corroborated and ordered first. Caps: `max_review_cycles` per task (default 6) or the same finding ID reopened `reopen_limit` times → task DEADLOCK → human arbitration (options: give manual instruction, reassign the coder role to another participant, edit the task, skip task). After every task sign-off, the orchestrator commits to the session branch: `duet(task-3): <title>` — granular, bisectable history.

### 5.5 Existing-repo support

At INIT: build repo map (file tree + per-file one-line summaries for source files, generated once by the cheaper configured model, cached by content hash). Agents pull file contents on demand via `read_file`; per-turn context budget (default 60k tokens input) enforced by the orchestrator — oldest file reads evicted first. The session branch is the only thing ever written; the working tree of the user's checkout is never touched.

### 5.6 Role assignment

Roles name participant ids, not providers. `build.coder` (unset → first in roster) picks the coder; `build.reviewers` (unset → every other participant) narrows the reviewer set when reviewing with all N-1 is too slow or too expensive. `build.alternate_roles: true` rotates the coder round-robin per task, `participants[(base + task_index) % N]` (default: false). Resolution is one function:

```python
coder, reviewers = config.roles_for_task(task_index)
```

A configuration where rotation would leave a task with no reviewer is rejected at that task rather than silently running unreviewed.

## 6. Human Gates

### 6.0 Mechanism

A gate is an event exchange, not a prompt. The engine emits `gate_pending {gate, artifacts[], options[]}` and returns; a client presents it however it likes; the decision comes back as `gate_decision {gate, action, actor, feedback?}` and `resume` continues from there. Consequences that the rest of this section assumes:

- Nothing in `orchestrator/` reads from stdin or opens an editor. The pager-and-`$EDITOR` flows below are the **CLI's** implementation of a gate client; V2's browser and MCP server are two more, with no privileged position among them.
- `actor` is required on every gate decision. In v1 it is the local user; multi-user attribution later needs no format change, and cannot be back-filled if omitted now.
- A gate decision is valid only against the artifact hash named in its `gate_pending`. A spec revised after the gate opened invalidates a pending decision rather than silently applying it to different text.

### 6.1 Gate 1 — spec approval

CLI renders `IMPLEMENTATION.md` (pager) + assumption list + waived-objections list + minority report (§4.3, when consensus was non-unanimous) + disagreement report if arbitrated. Objections that several participants raised independently are marked corroborated. Actions: **approve** · **edit** (opens `$EDITOR`; edited spec becomes canonical, no re-debate unless requested) · **revise** (human writes feedback → one more debate round cycle) · **reject** (session ends, log retained).

### 6.2 Gate 2 — code approval

CLI shows: full diff (`git diff main...duet/<id>` via pager), final test report, per-task summary table, total cost. Actions: **approve** (branch finalized; printed merge instructions — Duet never merges) · **request changes** (human feedback becomes a synthetic blocking finding → build loop reopens) · **reject**.

### 6.3 Deadlock arbitration

Orchestrator emits a disagreement report: each unresolved objection with every participant's final position on it, in roster order, plus each participant's one-paragraph closing statement — a column per participant, so the report widens rather than changes shape as the roster grows. Where participants split, the tally is shown (e.g. `2 uphold / 1 waive`). Human picks a position per objection, supplies their own resolution, or aborts. Resolutions are injected as `FIXED:` items and the debate resumes with cap +2 rounds.

## 7. Sandbox & Safety

### 7.1 Execution environment

Docker container per session: repo branch checkout mounted at `/work`, resource-limited (2 CPU, 4 GB, 512 MB tmp), **network disabled by default** (`sandbox.network: allowlist` may open specific hosts, e.g. package registries; enabling it is printed loudly at INIT). Fallback when Docker is unavailable: subprocess runner in a temp clone with denylist + explicit `--unsafe-local` flag required.

### 7.2 Hard rules

- Model output is never executed by the orchestrator process; only inside the sandbox via `run`.
- `run` denylist (both runners): `git push`, `curl|wget` (unless network allowlisted), `rm -rf /`-class patterns, `sudo`, package publishing commands, anything touching paths outside `/work`.
- Secrets: provider API keys live only in the orchestrator env; never mounted into the sandbox; sandbox env is scrubbed.
- Auto-stop: session halts (resumable) at `budget.max_usd` (default 25), `budget.max_wall_minutes` (default 90), or 3 consecutive identical failing test runs (thrash detector).

## 8. CLI

```
duet new "idea text or @brief.md" [--repo PATH] [--profile NAME]
duet resume <session-id>
duet sessions                      # list, with state + cost
duet replay <session-id>           # re-render debate + build log
duet roster                        # participants, resolved roles, per-model price
duet config [--edit]
duet ping                          # connectivity + cost check across the roster
```

Live view (`rich`): left-gutter participant badges from the roster (`[SOL]` cyan, `[OPUS]` orange, and whatever else is configured — `badge` and `color` are per-participant config, defaulting to the uppercased id), streaming text, verdict blocks rendered as tables, persistent status bar: `state · round · proposer · task k/n · $cost / $cap`. Concurrent critic and reviewer turns stream into per-participant panes.

`duet ping` and `duet roster` are the pre-flight pair for a new roster: `roster` shows who is in the loop, which role each takes, and what each costs per Mtok; `ping` proves every participant's credentials and adapter work, reporting per-participant success or failure rather than aborting on the first error.

## 9. Configuration (`duet.toml`)

The roster is an array of tables, so adding a voice is an entry, not a schema change. See `duet.example.toml` for the annotated version.

```toml
[[participants]]
id = "mini"                      # unique; roles, events and verdicts key off this
provider = "openai"              # registry key (§3)
model = "gpt-5.4-mini"
badge = "MINI"                   # optional, defaults to uppercased id
color = "cyan"

[[participants]]
id = "haiku"
provider = "anthropic"
model = "claude-haiku-4-5-20251001"
badge = "HAIKU"
color = "dark_orange"

# A third voice needs no other change; two participants may share a provider.
# [[participants]]
# id = "haiku"
# provider = "anthropic"
# model = "claude-haiku-4-5"

[debate]
max_rounds = 8
min_objection_rounds = 2
reopen_limit = 3
stall_rounds = 4                 # rounds without a new low before calling it stuck
consensus = "unanimous"          # unanimous | quorum
quorum = 1.0                     # fraction that must AGREE when consensus = "quorum"
max_output_tokens = 32768        # per-turn output cap

[build]
# coder = "opus"                 # participant id; unset -> first in roster
# reviewers = ["sol", "haiku"]   # unset -> every participant but the coder
alternate_roles = false          # true -> coder rotates round-robin per task
max_review_cycles = 6
max_tool_calls_per_turn = 25
context_budget_tokens = 60000

[budget]
max_usd = 25.0
max_wall_minutes = 90

[sandbox]
runner = "docker"                # docker | local-unsafe
network = "none"                 # none | allowlist
network_allowlist = ["pypi.org", "registry.npmjs.org"]

[utility.mapper]                 # cheap non-debate model for repo mapping
provider = "anthropic"
model = "claude-haiku-4-5"
```

The shipped roster is cross-lab but small: decorrelated blind spots are the premise and survive the downgrade, whereas a default nobody chose should not be able to spend $12 on one unconverged debate. `--profile flagship` swaps in the frontier pair.

Validation at load: ≥ 2 participants, unique ids, and role references that name real participants. Provider names are checked against the adapter registry when a provider is first built, so a registered-at-runtime adapter is usable without touching the config schema.

Env: one API key per provider in use — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and whatever a new adapter reads. Profiles: `~/.duet/profiles/<name>.toml` overlays; a profile that declares `[[participants]]` replaces the roster wholesale rather than merging entry-by-entry, so profiles like `trio` or `cheap` can swap the whole lineup.

## 10. Session Log (event schema)

JSONL, one event per line: `{seq, ts, type, payload}`. Types: `session_start`, `state_change`, `model_call`, `model_response`, `model_chunk`, `draft`, `verdict`, `tool_call`, `tool_result`, `test_run`, `gate_pending`, `gate_decision`, `commit`, `cost_tick`, `error`. `session_start` is the first event of every session and carries its `owner` — written once at creation because it cannot be reconstructed later, and the field every multi-user query resolves against. `model_chunk` carries streamed increments (§3); `gate_pending`/`gate_decision` are the two halves of the gate exchange (§6.0), and every `gate_decision` carries an `actor`. `seq` is the event-sourcing ordinal; resume = replay events, replay = pretty-print them. Artifacts (spec drafts, diffs, test logs) stored as files under `sessions/<id>/artifacts/`, referenced by `log_ref`.

Every event attributable to a model carries `participant` (the id) alongside `provider` and `model_id`. The participant id is the primary key: provider alone is ambiguous once two participants share one, and the roster can change between a session and its replay. Attribution, per-participant cost breakdowns, and vote tallies are all reconstructed from that field, so nothing in the log assumes a roster size.

## 11. Repo Layout

```
duet/
  cli.py              # typer app
  orchestrator/
    machine.py        # state machine + event sourcing
    debate.py         # Phase A engine
    build.py          # Phase B engine
    arbitration.py
  providers/
    base.py             # Provider protocol, adapter registry, retries, cost
    openai_.py  anthropic_.py
  workspace/
    repo.py           # git branch mgmt, repo map
    sandbox.py        # docker + local runners, denylist
  schemas/            # pydantic: config + roster, verdicts, findings, tasks, events
  prompts/            # versioned prompt templates (proposer, critic, coder, reviewer)
  store/              # session dirs, JSONL log, cost meter
duet.example.toml     # annotated config, including a commented third participant
tests/
  unit/  contract/    # contract tests hit real APIs behind a flag
  e2e/                # scripted fake-provider debates (deterministic)
```

## 12. Testing Strategy for Duet Itself

`FakeProvider` replays scripted responses → deterministic e2e tests of the full state machine (consensus path, deadlock path, budget stop, resume mid-build).

Contract tests (`tests/contract/`, opt-in via `DUET_CONTRACT=1` plus the relevant key) validate adapter behaviour against the real APIs, parametrised over providers so one set of assertions covers every lab. They exist because scripted providers agree with whatever the adapter believes: every assertion in them corresponds to a failure a fake could not have caught — OpenAI strict mode rejecting the JSON Schema pydantic emits, a model hitting its output cap and surfacing as unterminated JSON from the parser, Anthropic structured output needing a forced tool rather than a schema in the system prompt, and the streaming fallback being flagged rather than silent. The suite asserts the two real Phase A schemas are accepted, since `SpecDraft` and `Verdict` are the only ones a debate ever sends. Small models by default; a full pass costs cents, and a missing key skips loudly rather than passing quietly. Sandbox tests assert denylist and network isolation. Prompt changes require re-running the golden-idea suite (`tests/golden/`, opt-in via `DUET_GOLDEN=1` plus both keys). Its three briefs span the range §4.2 claims to handle — a vague vision, a moderate description, and a specific brief carrying explicit decisions — so the suite exercises both halves of the proposer contract: filling gaps with `ASSUMPTION:` and preserving stated decisions as `FIXED:`.

Only convergence is asserted. Rounds, cost, objection counts, the open-objection trace, and marker discipline are *recorded* to a report, because the useful question about a prompt edit is whether it made things better or worse — which needs two reports to compare, not one threshold to trip. Vocabulary drift (models inventing markers the convention does not define) is tracked as a known-failing test rather than an enforced one, so it stays visible without hiding the convergence result underneath it.

Roster-size coverage is a standing requirement, not a later addition: every protocol test runs at N = 2 and N = 3, with the N = 3 roster deliberately placing two participants on one provider so provider-keyed assumptions fail loudly. Role resolution, proposer rotation, and consensus thresholds are unit-tested against rosters of 2–5 (`tests/unit/test_roster.py`), and the registry test registers a stub adapter to prove a new lab needs no change to config or orchestration code.

## 13. Open Questions (to resolve during M0/M1)

1. ~~Should Gate 1 "edit" trigger an automatic one-round sanity review of the human's edits by the roster?~~ **Resolved: yes, but on demand rather than automatically.** `duet review <session>` puts an edited spec back in front of the roster for a single critique round, valid from GATE_1 or DONE — an approved spec is not frozen, it is one nobody is currently arguing about. The human takes the proposer turn, so the edit faces the same scrutiny a model-authored draft does and its verdicts land in the same log. Two rules the orchestrator enforces: the author's assent is recorded like any other vote but never counts toward consensus, which is a claim about the models; and a review is refused mid-debate, since injecting an outside draft into an open round would corrupt it. Left on demand rather than automatic because a trivial typo fix should not cost a round.
2. Reviewer-writes-tests: v1 lets the coder write tests and reviewers critique them; a stricter mode (a reviewer authors acceptance tests before the coder starts, TDD-style) is a candidate v1.1 flag.
3. Cost-tiering: use cheaper family tiers (Terra/Sonnet) for early debate rounds and flagships only for final rounds and sign-offs? Under the roster model this is expressible as per-participant model tiers plus a round threshold, rather than a separate mechanism.
4. N > 2 cost and latency: critics and reviewers scale as N-1 per round, so a 4-participant debate costs roughly 3× a pairwise one. Options: quorum consensus (§4.3), a cheaper "screening" subset that reviews first with flagships only on escalation, or capping concurrent critics per round. Needs measurement on real runs before a default changes.
5. N > 2 correlation: do participants from the same lab (two Claude models, say) produce correlated objections, weakening the decorrelated-blind-spots premise? The corroboration flag (§4.1) gives the data to check this — worth measuring before recommending same-provider rosters.
