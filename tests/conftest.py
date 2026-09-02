import pytest
from pydantic import BaseModel

from duet.providers.base import single_chunk
from duet.schemas.config import DuetConfig, Participant
from duet.schemas.debate import Objection, SpecDraft, Verdict, WaivedObjection
from duet.schemas.model_io import ModelRequest, ModelResponse, Usage


class FakeProvider:
    """Scripted provider for deterministic tests (SPECS §12)."""

    def __init__(self, name: str, model_id: str, responses: list[ModelResponse]):
        self.name = name
        self.model_id = model_id
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        if not self._responses:
            raise AssertionError(f"{self.name}: script exhausted after {len(self.requests)} calls")
        return self._responses.pop(0)

    def stream(self, req: ModelRequest):
        yield from single_chunk(self.complete(req))


@pytest.fixture
def config() -> DuetConfig:
    return DuetConfig()


def make_response(text: str, model_id: str = "fake", cost: float = 0.001) -> ModelResponse:
    return ModelResponse(
        text=text,
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=cost),
        model_id=model_id,
    )


def parsed(obj: BaseModel, model_id: str = "fake", cost: float = 0.001) -> ModelResponse:
    """A scripted structured-output response."""
    return ModelResponse(
        text=obj.model_dump_json(),
        parsed=obj,
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=cost),
        model_id=model_id,
    )


def spec(title: str, addresses: list[str] | None = None) -> SpecDraft:
    """A complete draft. The title varies the content hash between revisions."""
    body = {f: f"{title} — {heading}" for f, heading in _SECTIONS}
    return SpecDraft(title=title, addresses=addresses or [], **body)


_SECTIONS = [
    ("problem_statement", "problem"),
    ("goals_non_goals", "goals"),
    ("user_facing_behavior", "behaviour"),
    ("architecture", "architecture"),
    ("data_model", "data"),
    ("external_dependencies", "deps"),
    ("milestones", "milestones"),
    ("acceptance_criteria", "acceptance"),
    ("open_questions", "questions"),
]


def objects(section: str, claim: str, severity: str = "major") -> Verdict:
    return Verdict(
        verdict="OBJECT",
        objections=[
            Objection(
                severity=severity,
                section=section,
                claim=claim,
                evidence="cited evidence",
                proposed_change="do it differently",
            )
        ],
        agreement_score=0.3,
    )


def agrees(resolved: list[str] | None = None, waived: list[tuple[str, str]] | None = None) -> Verdict:
    return Verdict(
        verdict="AGREE",
        resolved_objections=resolved or [],
        waived_objections=[WaivedObjection(id=i, reason=r) for i, r in (waived or [])],
        agreement_score=0.95,
    )


def roster(*ids: str) -> DuetConfig:
    return DuetConfig(
        participants=[
            Participant(id=i, provider="anthropic", model=f"model-{i}") for i in ids
        ]
    )
