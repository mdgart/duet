# duet

Turn an idea into an implementation spec by making two frontier models from
different labs argue about it until they agree.

One model drafts a spec. A model from a *different* lab critiques it with
structured objections. Roles rotate, objections are tracked in a ledger, and the
debate ends when every participant agrees on the same draft — or deadlocks and
hands you a report of what they could not settle. You approve the result at a
hard gate. Nothing is merged, deployed, or published on your behalf.

The premise is that models from different labs have decorrelated blind spots. A
GPT proposal reviewed by Claude catches things same-model self-review misses,
and persistent disagreement usually marks a real design risk worth your
attention.

```
[MINI]  round 1 drafted 0da9e7bbf5d0        14s · 2306 tok · $0.01/$5
[HAIKU] OBJECT                              22s · 1840 tok · $0.03/$5
    major Data model: no pagination scheme is defined
[MINI]  round 2 drafted 5680ec1a17df · addresses OBJ-1
[HAIKU] AGREE
— DESIGN_DEBATE → GATE_1
```

## Status

**Phase A (idea → spec) works.** Phase B (spec → code) is not built yet.

| | |
|---|---|
| Idea → agreed `IMPLEMENTATION.md`, with human approval gate | ✅ |
| OpenAI + Anthropic adapters, any number of participants | ✅ |
| Resumable sessions, cost tracking, budget caps | ✅ |
| Spec → working code in a sandbox (Phase B) | ⬜ next |
| Web UI, MCP server, ideation phase | ⬜ [planned](PLAN.md#6-v2--web-ui-ideation-mcp) |

Three test suites: 110 unit/e2e tests, 16 contract tests against live provider
APIs, and a golden suite that runs three real debates end to end.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mdgart/duet.git
cd duet
uv sync --extra dev
```

Then either prefix commands with `uv run`, or activate the environment once:

```bash
source .venv/bin/activate
```

## Set up API keys

duet calls OpenAI and Anthropic with **your** keys and bills you directly — it
never brokers or proxies. Put them somewhere every shell can see, and keep them
out of your shell history:

```bash
mkdir -p ~/.config/duet && chmod 700 ~/.config/duet
cat >> ~/.config/duet/env <<'EOF'
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
EOF
chmod 600 ~/.config/duet/env
echo '[ -f "$HOME/.config/duet/env" ] && source "$HOME/.config/duet/env"' >> ~/.zshenv
```

Check both providers answer:

```bash
duet ping
```

## Quick start

```bash
duet new "a CLI that tracks what I read"
```

The debate streams live. At the gate you get a summary written for the person
deciding — the problem, the approach, and the choices that would most change the
result if they were wrong — plus what the debate cost and settled:

```
──────────────── gate_1 · 2/2: haiku, mini ────────────────
In short

Engineers each run their own mix of AI tools, so nobody can say what is
running where. This registers every tool centrally and enforces an
allow-list on the endpoint. The decision that matters most is whether
endpoint enforcement is a security boundary or a compliance signal.

3 rounds · $0.0060 · 7/9 objections settled · 4 assumptions made for you
full document: ~/.duet/sessions/<id>/artifacts/IMPLEMENTATION.md (24,758 chars)

action (approve/edit/revise/reject/view):
```

`view` pages the full spec — which is written for whoever builds it and runs to
tens of thousands of characters, so it is not what you read to decide.
**approve** accepts it, **edit** opens `$EDITOR`, **revise** sends it back with
feedback, **reject** ends the session.

If the models cannot agree, you get a disagreement report instead: what each
one still holds, whether the debate was converging, and a choice of **resolve**
or **abort**. Choosing resolve opens an editor listing every unresolved
objection with space for your decision on each; those become `FIXED:` items the
models must accept, and the debate resumes with a raised round cap. Objections
you leave blank come back.

The argument is the idea itself, or `@path` to read it from a file. Background
material comes from `--context`, which is repeatable. Both accept `~`:

```bash
duet new "a CLI that tracks what I read"
duet new "@~/notes/brief.md" --context docs/architecture.md --context docs/conventions.md
```

The **brief** is what you want built and the debate revises it. **Context** is
what is already true — existing systems, constraints, decisions — and the models
must treat it as settled rather than argue with it. Context is read as literal
text; no URL in it is ever fetched.

## Commands

| | |
|---|---|
| `duet new "<idea>"` | Start a debate. `@file.md` reads a brief from disk. |
| `duet resume <id>` | Continue a suspended session. Any unambiguous id prefix works. |
| `duet review <id>` | Put an edited spec back to the models for one critique round. |
| `duet sessions` | List sessions — what each was about, owner, state and cost, newest first. |
| `duet roster` | Show participants, their roles, and what they cost. |
| `duet ping` | Check every participant's credentials and adapter. |
| `duet config` | Print the resolved configuration. |

## Cost

You are billed by the providers directly. A converged three-round debate costs
**about 5–10 cents** on the default roster, and **$1–2** on flagships.

Defaults are chosen to be safe to run by accident: small models from each lab,
and a `$5` cap. Hitting the cap *suspends* the session rather than ending it —
raise the cap and carry on from where it stopped:

```bash
duet resume <id> --budget 15
```

Nothing is lost or repeated: the budget is checked between turns, so no turn you
paid for is discarded, and the debate resumes from the log rather than
restarting.

```bash
duet new "an idea" --profile flagship   # gpt-5.6-sol + claude-opus-5
```

Profiles live in `~/.duet/profiles/<name>.toml`; one declaring
`[[participants]]` swaps the whole roster. See
[`duet.example.toml`](duet.example.toml) for every setting.

## More than two models

The name is the common case, not a limit. Participants are a list, and every
protocol — proposer rotation, consensus threshold, event attribution — is
defined for N:

```toml
[[participants]]
id = "mini"
provider = "openai"
model = "gpt-5.4-mini"

[[participants]]
id = "haiku"
provider = "anthropic"
model = "claude-haiku-4-5-20251001"

[[participants]]
id = "sonnet"
provider = "anthropic"
model = "claude-sonnet-5"
```

Each round one participant proposes and every other critiques independently.
Two participants may share a provider; participants are identified by id, not
by lab.

## How a session is stored

Everything lives in `~/.duet/sessions/<id>/`. The append-only `log.jsonl` is the
source of truth — state, votes, objections and cost all rebuild from it, which
is why a session suspended at a gate resumes in a different process, or on a
different day. Drafts are written as they are produced, so nothing is lost if
you interrupt a run.

## Development

```bash
uv run --extra dev pytest                              # 110 unit + e2e tests
DUET_CONTRACT=1 uv run --extra dev pytest tests/contract   # live API, ~cents
DUET_GOLDEN=1 uv run --extra dev pytest tests/golden -s    # 3 real debates, ~30c
```

The contract suite exists because scripted fakes agree with whatever the adapter
believes; every assertion in it corresponds to a bug only a live call found. The
golden suite is how prompt changes are judged — it records rounds, cost and
objection churn per idea, so an edit is evaluated by diffing two reports rather
than by argument.

## Design docs

- [PLAN.md](PLAN.md) — vision, milestones, and the V2 architecture
- [SPECS.md](SPECS.md) — protocol, schemas, and the reasoning behind them

## Licence

Not yet chosen.
