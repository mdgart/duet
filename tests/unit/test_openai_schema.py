"""OpenAI strict structured-output dialect (SPECS §3).

Regression cover for a live 400 that the scripted providers could not catch:
pydantic emits optional properties and range constraints, strict mode rejects both.
"""

import json

from pydantic import BaseModel, Field

from duet.providers.openai_ import strict_json_schema
from duet.schemas.debate import SpecDraft, Verdict


class Inner(BaseModel):
    kept: str
    optional: int = 3


class Outer(BaseModel):
    name: str
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    items: list[Inner] = Field(default_factory=list)


def test_every_object_forbids_extra_keys_and_requires_everything():
    schema = strict_json_schema(Outer.model_json_schema())
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name", "score", "items"]
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["kept", "optional"]


def test_unsupported_keywords_are_stripped():
    blob = json.dumps(strict_json_schema(Outer.model_json_schema()))
    for keyword in ("minimum", "maximum", "default"):
        assert f'"{keyword}"' not in blob


def test_the_real_phase_a_schemas_convert():
    for model in (SpecDraft, Verdict):
        schema = strict_json_schema(model.model_json_schema())
        assert schema["required"] == list(schema["properties"])
        for definition in schema.get("$defs", {}).values():
            assert definition["required"] == list(definition["properties"])
            assert definition["additionalProperties"] is False


def test_transform_does_not_mutate_the_input():
    original = Outer.model_json_schema()
    before = json.dumps(original, sort_keys=True)
    strict_json_schema(original)
    assert json.dumps(original, sort_keys=True) == before


def test_a_property_literally_named_default_survives():
    """The stripper must not confuse a field name with a schema keyword."""

    class Tricky(BaseModel):
        default: str
        format: str

    schema = strict_json_schema(Tricky.model_json_schema())
    assert sorted(schema["properties"]) == ["default", "format"]
    assert schema["required"] == ["default", "format"]
