"""Three briefs spanning the range PLAN §1 claims to handle — "anywhere from a
vague vision to a precise implementation brief" — each run end to end against
real models.

Convergence is the assertion. Everything else is recorded rather than asserted,
because the useful question about prompt discipline is "did this change make it
better or worse", which needs two reports, not one threshold.
"""

import collections
import re

import pytest

from duet.orchestrator.debate import DebateEngine
from duet.orchestrator.machine import Machine, providers_for
from duet.schemas.debate import SPEC_SECTIONS
from duet.store.session import SessionStore

VAGUE = "something to help me keep track of what I read"

MODERATE = (
    "a command-line tool for tracking books and articles I read, with tags, "
    "reading status, and a weekly summary"
)

SPECIFIC = (
    "A CLI called `readlog` for tracking reading.\n"
    "It must store data in a single SQLite file at ~/.readlog/db.sqlite.\n"
    "It must expose exactly four commands: add, finish, list, stats.\n"
    "It must not require network access or an account.\n"
    "Written in Python 3.12 with no dependencies outside the standard library."
)

IDEAS = [
    pytest.param("vague", VAGUE, id="vague"),
    pytest.param("moderate", MODERATE, id="moderate"),
    pytest.param("specific", SPECIFIC, id="specific"),
]

# Markers the convention defines (SPECS §4.2). Anything else in this shape is
# the model inventing vocabulary, which is worth watching but not failing on.
KNOWN_MARKERS = {"ASSUMPTION", "FIXED"}
# The trailing whitespace matters: without it "HTTPS://example.com" reads as a
# marker named HTTPS. Real markers are always followed by prose.
MARKER_RE = re.compile(r"\b([A-Z]{4,})[:：]\s")


def run_idea(config, tmp_path_factory, name: str, brief: str) -> tuple:
    root = tmp_path_factory.mktemp(f"golden-{name}")
    store = SessionStore.new(root=root, owner="golden-suite")
    machine = Machine(config, store, providers_for(config))
    result = machine.start(brief)
    engine = DebateEngine.replay(config, store, {})
    return result, store, engine.state


@pytest.mark.parametrize("name,brief", IDEAS)
def test_a_real_idea_converges_within_the_round_cap(
    golden_config, report, tmp_path_factory, name, brief
):
    result, store, state = run_idea(golden_config, tmp_path_factory, name, brief)
    spec = (store.dir / "artifacts" / "IMPLEMENTATION.md")
    markdown = spec.read_text() if spec.is_file() else ""

    report[name] = {
        "outcome": result.state,
        "rounds": state.round,
        "cost_usd": store.cost_usd,
        "objections": len(state.ledger.entries),
        "corroborated": len(state.ledger.corroborated()),
        "open_history": state.open_history,
        "spec_chars": len(markdown),
        "markers": collections.Counter(MARKER_RE.findall(markdown)),
        "detail": result.detail,
        "session": store.session_id,
    }

    assert result.state == "GATE_1", f"{name} ended {result.state}: {result.detail}"
    assert state.round <= golden_config.debate.max_rounds
    assert store.cost_usd < golden_config.budget.max_usd

    # A spec that converged but is hollow has not done the job.
    for _, heading in SPEC_SECTIONS:
        assert f"## {heading}" in markdown, f"{name} spec is missing '{heading}'"
    assert len(markdown) > 2000, f"{name} spec is suspiciously short"


def test_a_vague_brief_produces_stated_assumptions(golden_config, report):
    """SPECS §4.2: gaps are filled with explicit ASSUMPTION: markers rather
    than questions back to the human."""
    row = report.get("vague")
    if not row or not row["spec_chars"]:
        pytest.skip("the vague idea produced no spec to inspect")
    assert row["markers"].get("ASSUMPTION", 0) > 0


def test_a_specific_brief_preserves_the_humans_decisions(golden_config, report):
    """SPECS §4.2: stated decisions are kept verbatim and marked FIXED:, which
    is what makes them un-overturnable by a critic."""
    row = report.get("specific")
    if not row or not row["spec_chars"]:
        pytest.skip("the specific idea produced no spec to inspect")
    assert row["markers"].get("FIXED", 0) > 0


@pytest.mark.xfail(reason="known: models invent markers such as DECIDED: (PLAN M1)", strict=False)
def test_specs_use_only_the_markers_the_convention_defines(golden_config, report):
    """Tracked rather than enforced: this is a prompt fix, and failing the
    whole suite over vocabulary would hide the convergence result that matters."""
    invented = {
        name: sorted(set(row["markers"]) - KNOWN_MARKERS)
        for name, row in report.items()
        if set(row["markers"]) - KNOWN_MARKERS
    }
    assert not invented, f"markers outside the convention: {invented}"
