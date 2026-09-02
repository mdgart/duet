"""The roster is a list of participants, never a fixed pair (SPECS §1, §4.1, §5.6)."""

import pytest

from duet.providers.base import build_provider, known_providers, register_provider
from duet.schemas.config import DebateConfig, DuetConfig, ModelSpec, Participant


def roster_of(*ids: str) -> DuetConfig:
    return DuetConfig(
        participants=[
            Participant(id=i, provider="anthropic", model=f"model-{i}") for i in ids
        ]
    )


def test_the_default_roster_is_cheap_and_still_cross_lab():
    """Defaults must be safe to run by accident. Cross-lab is the premise and
    survives the downgrade; a flagship pair reached $12 on one unconverged run."""
    cfg = DuetConfig()
    assert cfg.roster.ids == ["mini", "haiku"]
    assert [p.provider for p in cfg.roster] == ["openai", "anthropic"]
    assert all(cfg.pricing_for(p.model) for p in cfg.roster), "an unpriced default disarms the cap"


def test_participants_may_share_a_provider():
    cfg = DuetConfig(
        participants=[
            Participant(id="sol", provider="openai", model="gpt-5.6-sol"),
            Participant(id="opus", provider="anthropic", model="claude-opus-5"),
            Participant(id="haiku", provider="anthropic", model="claude-haiku-4-5"),
        ]
    )
    assert len(cfg.roster) == 3
    assert [p.provider for p in cfg.roster] == ["openai", "anthropic", "anthropic"]
    assert cfg.roster.get("haiku").badge == "HAIKU"  # defaults from id


def test_others_excludes_only_the_named_participant():
    cfg = roster_of("a", "b", "c")
    assert [p.id for p in cfg.roster.others("b")] == ["a", "c"]


def test_proposer_rotates_round_robin():
    cfg = roster_of("a", "b", "c")
    picked = [cfg.roster.proposer_for_round(r).id for r in range(1, 8)]
    assert picked == ["a", "b", "c", "a", "b", "c", "a"]


def test_two_participant_rotation_is_the_alternating_protocol():
    cfg = roster_of("a", "b")
    assert [cfg.roster.proposer_for_round(r).id for r in range(1, 5)] == ["a", "b", "a", "b"]


def test_reviewers_default_to_everyone_else():
    cfg = roster_of("a", "b", "c")
    coder, reviewers = cfg.roles_for_task(0)
    assert coder.id == "a"
    assert [r.id for r in reviewers] == ["b", "c"]


def test_explicit_coder_and_alternating_roles_rotate_per_task():
    cfg = roster_of("a", "b", "c")
    cfg.build.coder = "b"
    cfg.build.alternate_roles = True
    assert [cfg.roles_for_task(i)[0].id for i in range(4)] == ["b", "c", "a", "b"]


def test_fixed_roles_ignore_task_index():
    cfg = roster_of("a", "b", "c")
    cfg.build.coder = "c"
    assert [cfg.roles_for_task(i)[0].id for i in range(3)] == ["c", "c", "c"]


def test_explicit_reviewer_subset_drops_the_coder():
    cfg = roster_of("a", "b", "c")
    cfg.build.reviewers = ["a", "c"]
    coder, reviewers = cfg.roles_for_task(0)
    assert coder.id == "a"
    assert [r.id for r in reviewers] == ["c"]


def test_rotation_into_an_empty_reviewer_set_is_rejected():
    cfg = roster_of("a", "b")
    cfg.build.reviewers = ["b"]
    cfg.build.alternate_roles = True
    with pytest.raises(ValueError, match="no reviewer"):
        cfg.roles_for_task(1)  # coder rotates to b, the only listed reviewer


def test_roster_rejects_duplicates_and_singletons():
    with pytest.raises(ValueError, match="duplicate participant ids"):
        roster_of("a", "a")
    with pytest.raises(ValueError, match="at least two participants"):
        roster_of("a")


def test_roles_must_name_real_participants():
    with pytest.raises(ValueError, match="build.coder"):
        DuetConfig(
            participants=list(DuetConfig().participants), build={"coder": "ghost"}
        )
    with pytest.raises(ValueError, match="build.reviewers"):
        DuetConfig(
            participants=list(DuetConfig().participants), build={"reviewers": ["ghost"]}
        )


def test_unknown_participant_lookup_names_the_roster():
    cfg = roster_of("a", "b")
    with pytest.raises(ValueError, match=r"no participant 'zz' in roster \['a', 'b'\]"):
        cfg.roster.get("zz")


def test_consensus_thresholds_scale_with_roster_size():
    unanimous = DebateConfig()
    assert unanimous.required_agreements(2) == 2
    assert unanimous.required_agreements(5) == 5

    majority = DebateConfig(consensus="quorum", quorum=0.6)
    assert majority.required_agreements(5) == 3
    assert majority.required_agreements(2) == 2  # never below two voices


def test_provider_registry_is_extensible():
    assert known_providers() == ["anthropic", "openai"]

    class StubProvider:
        name = "stub"

        def __init__(self, model_id: str):
            self.model_id = model_id

        def complete(self, req):  # pragma: no cover - never called
            raise NotImplementedError

    register_provider("stub", lambda spec, cfg: StubProvider(spec.model))
    try:
        provider = build_provider(ModelSpec(provider="stub", model="m-1"), DuetConfig())
        assert provider.model_id == "m-1"
    finally:
        from duet.providers import base

        base._FACTORIES.pop("stub")


def test_unknown_provider_lists_what_is_registered():
    with pytest.raises(ValueError, match="unknown provider 'mistral'; registered: anthropic, openai"):
        build_provider(ModelSpec(provider="mistral", model="large"), DuetConfig())


# --- objection identity (from the golden suite's deadlock) -------------------


def test_composite_section_labels_collapse_to_the_skeleton():
    """Critics compose labels freely; the dedup key must not fragment on it."""
    from duet.schemas.debate import canonical_section, normalize_claim

    for label in ("Data model", "Data model — tags storage", "Data model / tags storage",
                  "Data model — tags storage, User-facing behavior — add command"):
        assert canonical_section(label) == "Data model"
    # Same claim, drifting labels, one key.
    keys = {normalize_claim(label, "tags are lossy")
            for label in ("Data model — tags storage", "Data model / tags storage")}
    assert len(keys) == 1


def test_a_critic_can_link_a_reworded_reopen():
    """Observed live: the same concern raised in all 8 rounds, median claim
    similarity 0.07. No wording rule finds that; the critic naming the id does."""
    from duet.orchestrator.ledger import ObjectionLedger
    from duet.schemas.debate import Objection, Verdict

    ledger = ObjectionLedger()
    first = Objection(severity="blocking", section="Data model",
                      claim="comma-separated tags are fragile for filtering",
                      evidence="e", proposed_change="p")
    ledger.merge_round(1, {"a": Verdict(verdict="OBJECT", objections=[first])})
    ledger.close_for("a", ["OBJ-1"])
    assert ledger.get("OBJ-1").status == "resolved"

    # Entirely different wording, explicitly linked.
    reworded = Objection(severity="blocking", section="Data model — tags storage / add",
                         claim="the tag model silently corrupts inputs containing commas",
                         evidence="e", proposed_change="p", reopens="OBJ-1")
    ledger.merge_round(2, {"a": Verdict(verdict="OBJECT", objections=[reworded])})

    assert len(ledger.entries) == 1, "a linked reopen must not create a new entry"
    assert ledger.get("OBJ-1").reopen_count == 1
    assert ledger.get("OBJ-1").status == "open"


def test_an_invented_reopen_id_is_ignored_not_trusted():
    """The critic is believed about identity, never about the id existing."""
    from duet.orchestrator.ledger import ObjectionLedger
    from duet.schemas.debate import Objection, Verdict

    ledger = ObjectionLedger()
    ledger.merge_round(1, {"a": Verdict(verdict="OBJECT", objections=[
        Objection(severity="major", section="Data model", claim="new point",
                  evidence="e", proposed_change="p", reopens="OBJ-99")])})
    assert [e.id for e in ledger.entries] == ["OBJ-1"]
    assert ledger.get("OBJ-1").reopen_count == 0


def test_the_prompt_ledger_states_who_raised_it_not_that_it_is_endorsed():
    """`[corroborated]` reaching a model tells it a point is strong when all
    that happened is two readers thought of it — how a wrong constant travelled
    unchecked between participants. Who raised it is a fact; corroborated is a
    verdict, and verdicts are the human's to reach."""
    from duet.orchestrator.ledger import ObjectionLedger
    from duet.schemas.debate import Objection, Verdict

    ledger = ObjectionLedger()
    same = lambda: Objection(severity="major", section="Data model",
                             claim="latent heat is overstated", evidence="e", proposed_change="p")
    ledger.merge_round(1, {"a": Verdict(verdict="OBJECT", objections=[same()]),
                           "b": Verdict(verdict="OBJECT", objections=[same()])})

    entry = ledger.entries[0]
    assert entry.corroborated  # still tracked, for the human-facing report
    line = ledger.summary_lines()[0]
    assert "raised by a+b" in line
    assert "corroborat" not in line.lower(), "no endorsement may reach a prompt"
