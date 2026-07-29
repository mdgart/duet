# Duet — Technical Specification

**v0.1 · 2026-07-26 · companion to PLAN.md**

## 1. Definitions

- **Session**: one end-to-end run, idea → prototype. Has a UUID, a directory under `~/.duet/sessions/<id>/`, and a JSONL event log.
- **Debate**: a bounded propose/critique/revise exchange between two models ending in consensus, deadlock, or budget stop.
- **Verdict**: a structured JSON block every critique turn must end with (schema §4.3).
- **Gate**: a blocking human decision point. Gate 1 = spec approval, Gate 2 = code approval. Nothing crosses a gate without explicit human input.
- **Coder / Reviewer**: Phase B roles. Assigned to opposite providers; assignment configurable, optionally alternating per task.

## 2. State Machine

```
INIT → DESIGN_DEBATE → GATE_1 → TASK_PLANNING → BUILD_LOOP → GATE_2 → DONE
         │                │            │             │           │
         ├─ DEADLOCK ─────┤ (human     │             ├─ DEADLOCK ┤
         └─ BUDGET_STOP    arbitrates) └───────── BUDGET_STOP / FAILED
```

Transitions are logged events. Every state is resumable: `duet resume <session-id>` reconstructs state from the event log (event-sourced; no separate mutable state file).

- **INIT**: load config, validate API keys, create session dir, snapshot repo (if existing-repo mode: create branch `duet/<session-id>` from current HEAD).
- **DESIGN_DEBATE**: Phase A protocol (§4). Exit on consensus → GATE_1; on round cap → DEADLOCK → human arbitration (§6.3).
- **GATE_1**: human approves/edits/rejects `IMPLEMENTATION.md` (§6.1).
- **TASK_PLANNING**: both models jointly decompose the approved spec into an ordered task list (mini-debate, max 2 rounds), each task with acceptance criteria. Human may reorder/trim (soft gate: default-accept after display).
- **BUILD_LOOP**: per-task coder/reviewer loop (§5). All tasks signed off → GATE_2.
- **GATE_2**: human reviews final diff + test report (§6.2).
- **DONE**: session summary written; branch left for human to merge (never auto-merged).

## 3. Provider Layer

```python
class Provider(Protocol):
    name: str                     # "openai" | "anthropic"
    def complete(self, req: ModelRequest) -> ModelResponse: ...

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

Adapters: `OpenAIProvider` (Responses API, default model `gpt-5.6-sol`), `AnthropicProvider` (Messages API, default model `claude-opus-5`). Structured outputs use each API's native JSON-schema mode; on validation failure, one automatic repair round ("your output failed validation: <errors>; re-emit"), then hard error. Retries: exponential backoff, 5 attempts, on 429/5xx. Every call and response is an event in the session log (prompts included — full replayability).

## 4. Phase A — Design Debate

### 4.1 Roles and turn order

Round r: **Proposer** emits/revises the spec draft → **Critic** emits critique + verdict. Roles swap each round (r1: model A proposes; r2: model B proposes the revision incorporating accepted points). Swapping prevents ownership bias and makes "agreement" mean both models have authored the document.

### 4.2 Proposer output — spec draft

Markdown, fixed section skeleton (enforced by structured output wrapper):

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

- Rounds 1–2: verdict AGREE is rejected by the orchestrator (anti-sycophancy floor); the critic must produce ≥1 objection with concrete evidence.
- AGREE is valid only when every previously raised `blocking` and `major` objection appears in `resolved_objections` or `waived_objections`.
- **Consensus** = both models have issued AGREE on the *same draft hash* (after an AGREE, the other model gets a final confirmation turn on the unchanged draft).
- **Deadlock** = round cap hit (default 8), or the same objection ID reopened 3 times.

### 4.4 Context management

Full draft always in context. Debate history: last 2 rounds verbatim; older rounds replaced by an objection-ledger summary (id, status, one-line resolution) maintained deterministically by the orchestrator from verdict JSON — never by model summarization.

## 5. Phase B — Build Loop

### 5.1 Task structure

From TASK_PLANNING: `Task {id, title, description, acceptance_criteria[], depends_on[], status}`. Executed sequentially in dependency order (v1: no parallelism).

### 5.2 Coder turn

Coder receives: approved spec, current task, repo map (§5.5), prior review findings. Tools (function-calling): `read_file(path)`, `write_file(path, content)`, `apply_patch(diff)`, `list_dir(path)`, `run(cmd)` — all executed **in the sandbox only** (§7). Turn ends when the coder emits `TASK_READY {summary, files_touched[], how_to_test}`. Guardrails: max 25 tool calls per turn; `run` commands pass a denylist filter (§7.2) even inside the sandbox.

### 5.3 Reviewer turn

Reviewer receives: task + acceptance criteria, unified diff since last sign-off, `how_to_test`. Reviewer **must** call `run` for the test suite at least once — a sign-off without an attached test-run event is rejected by the orchestrator. Read-only tools plus `run`; the reviewer cannot edit files. Output schema:

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

SIGN_OFF requires: zero blocking/major findings open, tests passing, acceptance criteria checklist all true.

### 5.4 Loop control per task

REJECT → findings go back to coder → fix turn → re-review. Caps: 6 review cycles per task or same finding ID reopened 3 times → task DEADLOCK → human arbitration (options: give manual instruction, swap coder/reviewer roles, edit the task, skip task). After every task sign-off, the orchestrator commits to the session branch: `duet(task-3): <title>` — granular, bisectable history.

### 5.5 Existing-repo support

At INIT: build repo map (file tree + per-file one-line summaries for source files, generated once by the cheaper configured model, cached by content hash). Agents pull file contents on demand via `read_file`; per-turn context budget (default 60k tokens input) enforced by the orchestrator — oldest file reads evicted first. The session branch is the only thing ever written; the working tree of the user's checkout is never touched.

### 5.6 Role assignment

Config `build.coder = "anthropic" | "openai"`; reviewer is always the other provider. `build.alternate_roles: true` swaps per task (default: false).

## 6. Human Gates

### 6.1 Gate 1 — spec approval

CLI renders `IMPLEMENTATION.md` (pager) + assumption list + waived-objections list + disagreement report if arbitrated. Actions: **approve** · **edit** (opens `$EDITOR`; edited spec becomes canonical, no re-debate unless requested) · **revise** (human writes feedback → one more debate round cycle) · **reject** (session ends, log retained).

### 6.2 Gate 2 — code approval

CLI shows: full diff (`git diff main...duet/<id>` via pager), final test report, per-task summary table, total cost. Actions: **approve** (branch finalized; printed merge instructions — Duet never merges) · **request changes** (human feedback becomes a synthetic blocking finding → build loop reopens) · **reject**.

### 6.3 Deadlock arbitration

Orchestrator emits a disagreement report: each unresolved objection with both models' final positions, side by side, plus each model's one-paragraph closing statement. Human picks a side per objection, supplies their own resolution, or aborts. Resolutions are injected as `FIXED:` items and the debate resumes with cap +2 rounds.

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
duet config [--edit]
```

Live view (`rich`): left-gutter model badges (`[SOL]` cyan / `[OPUS]` orange), streaming text, verdict blocks rendered as tables, persistent status bar: `state · round · task k/n · $cost / $cap`.

## 9. Configuration (`duet.toml`)

```toml
[models]
openai    = "gpt-5.6-sol"        # any Responses-API model id
anthropic = "claude-opus-5"      # any Messages-API model id
mapper    = "claude-haiku-4-5"   # cheap model for repo mapping

[debate]
max_rounds = 8
min_objection_rounds = 2
reopen_limit = 3

[build]
coder = "anthropic"
alternate_roles = false
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
```

Env: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Profiles: `~/.duet/profiles/<name>.toml` overlays.

## 10. Session Log (event schema)

JSONL, one event per line: `{seq, ts, type, payload}`. Types: `state_change`, `model_call`, `model_response`, `verdict`, `tool_call`, `tool_result`, `test_run`, `gate_decision`, `commit`, `cost_tick`, `error`. `seq` is the event-sourcing ordinal; resume = replay events, replay = pretty-print them. Artifacts (spec drafts, diffs, test logs) stored as files under `sessions/<id>/artifacts/`, referenced by `log_ref`.

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
    base.py  openai_.py  anthropic_.py
  workspace/
    repo.py           # git branch mgmt, repo map
    sandbox.py        # docker + local runners, denylist
  schemas/            # pydantic: verdicts, findings, tasks, events, config
  prompts/            # versioned prompt templates (proposer, critic, coder, reviewer)
  store/              # session dirs, JSONL log, cost meter
tests/
  unit/  contract/    # contract tests hit real APIs behind a flag
  e2e/                # scripted fake-provider debates (deterministic)
```

## 12. Testing Strategy for Duet Itself

`FakeProvider` replays scripted responses → deterministic e2e tests of the full state machine (consensus path, deadlock path, budget stop, resume mid-build). Contract tests (opt-in, real APIs) validate structured-output behavior per provider. Sandbox tests assert denylist and network isolation. Prompt changes require re-running a small golden-idea suite (3 ideas, assert convergence within round cap).

## 13. Open Questions (to resolve during M0/M1)

1. Should Gate 1 "edit" trigger an automatic one-round sanity review of the human's edits by both models? (Leaning yes, cheap and catches contradictions.)
2. Reviewer-writes-tests: v1 lets the coder write tests and the reviewer critique them; a stricter mode (reviewer authors acceptance tests before coder starts, TDD-style) is a candidate v1.1 flag.
3. Cost-tiering: use cheaper family tiers (Terra/Sonnet) for early debate rounds and flagships only for final rounds and sign-offs?
