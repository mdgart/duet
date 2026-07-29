import pytest

from duet.schemas.config import DuetConfig
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
        return self._responses.pop(0)


@pytest.fixture
def config() -> DuetConfig:
    return DuetConfig()


def make_response(text: str, model_id: str = "fake", cost: float = 0.001) -> ModelResponse:
    return ModelResponse(
        text=text,
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=cost),
        model_id=model_id,
    )
