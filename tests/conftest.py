import json
from pathlib import Path
import pytest

from pha_core.domain import StudySnapshot


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[1] / "examples/tank_overpressure.snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture
def snapshot(raw):
    return StudySnapshot.model_validate_json(json.dumps(raw))


def parse(raw):
    return StudySnapshot.model_validate_json(json.dumps(raw, allow_nan=True))
