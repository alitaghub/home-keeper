"""The show_chart schema's verdicts on the specs the panel's parser judges too.

apps/web/src/agent/chartSpec.test.ts reads the same fixture file, so the two parsers
cannot drift apart unnoticed.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from home_keeper_agent.charts import show_chart

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/chart_specs.json"


def _revive(value: dict[str, object]) -> object:
    # JSON cannot write NaN or infinity; the fixture marks them as {"$float": "NaN"}.
    if value.keys() == {"$float"} and isinstance(value["$float"], str):
        return float(value["$float"])
    return value


def _load() -> dict[str, list[dict[str, object]]]:
    return json.loads(FIXTURES.read_text(), object_hook=_revive)


CASES = _load()
VALID = [pytest.param(case["spec"], id=str(case["case"])) for case in CASES["valid"]]
INVALID = [pytest.param(case["spec"], id=str(case["case"])) for case in CASES["invalid"]]


@pytest.mark.parametrize("spec", VALID)
def test_show_chart_accepts_a_valid_spec_with_a_confirmation(spec: object) -> None:
    result = show_chart.invoke({"spec": spec})

    assert isinstance(result, str)
    assert result


@pytest.mark.parametrize("spec", INVALID)
def test_show_chart_rejects_an_invalid_spec(spec: object) -> None:
    with pytest.raises(ValidationError):
        show_chart.invoke({"spec": spec})
