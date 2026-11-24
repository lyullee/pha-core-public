import copy
import json
import os
from pathlib import Path

import pytest

from pha_core.application import analyze
from pha_core.prism import PrismClient, PrismError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    root = Path(os.environ.get("PHA_PRISM_ROOT", str(Path.home() / "Documents/ChatGPT/TNO 위험성 분석 프로젝트/lh2-tno")))
    if not (root / "src/lh2_tno/derived_frequency.py").is_file():
        pytest.skip("Set PHA_PRISM_ROOT to an extended PRISM checkout")
    return PrismClient(root)


@pytest.fixture
def physical():
    return json.loads((Path(__file__).parents[1] / "examples/prism_diagnostic_handoff.json").read_text(encoding="utf-8"))


@pytest.fixture
def event(client, snapshot, physical):
    return client.build_event(snapshot, analyze(snapshot), snapshot.scenarios[0].id,
                              port_ids=tuple(physical["port_ids"]), release=physical["release"])


def test_real_worker_and_derived_provenance(client, physical, event):
    preflight = client.preflight(event, physical["facility"], physical["request"])
    assert preflight["status"] == "BINDING_VALIDATED"
    assert Path(preflight["imported_module"]).is_relative_to(client.root)
    result = client.evaluate(event, physical["facility"], physical["request"])
    data = result["result"]["data"]
    assert data["full_chain_executed"] is True
    assert any(max(row["harm_given_observed_release_low_high"]) > 0 for row in data["receptors"])
    assert data["mass_ledger"]["end_to_end_residual_kg"] == pytest.approx(0, abs=1e-8)
    derived = result["result"]["frequency_event"]
    assert derived["basis"] == "FTA_LOPA_DERIVED"
    assert derived["annual_frequency_per_year"] == pytest.approx(9.632506368e-7)
    assert derived["baseline_digest"] == event["baseline_digest"]
    assert derived["included_barrier_ids"] == ["IPL-PSV01", "IPL-SIF01"]
    assert result["physical_validation_claim"] is False
    assert result["complete_facility_qra_claim"] is False


@pytest.mark.parametrize("mutation,match", [
    (lambda p, e: e.update(port_ids=["wrong.out"]), "port_ids"),
    (lambda p, e: e.update(annual_frequency_per_year=.1), "credited probabilities"),
    (lambda p, e: e["credits"].append(e["credits"][0].copy()), "duplicate probability"),
    (lambda p, e: e.update(prism_source_digest="0" * 64), "source digest"),
    (lambda p, e: p["request"].update(operating_mode="storage"), "operating mode"),
    (lambda p, e: p["request"]["resolved_release"].update(event_kind="normal_vent"), "accidental|normal"),
    (lambda p, e: e["release"].update(hole_diameter_m=.02), "leak size"),
])
def test_prism_rejects_bad_handoffs(client, physical, event, mutation, match):
    mutation(physical, event)
    with pytest.raises(PrismError, match=match):
        client.preflight(event, physical["facility"], physical["request"])


def test_tampered_or_stale_analysis_rejected(client, snapshot, physical):
    analysis = analyze(snapshot)
    analysis["lopa_proposed"][snapshot.scenarios[0].id]["residual_frequency_per_year"] *= 2
    with pytest.raises(ValueError, match="reproducible"):
        client.build_event(snapshot, analysis, snapshot.scenarios[0].id, port_ids=("storage.out",), release=physical["release"])
    analysis["code_digest"] = "0" * 64
    with pytest.raises(ValueError, match="stale"):
        client.build_event(snapshot, analysis, snapshot.scenarios[0].id, port_ids=("storage.out",), release=physical["release"])


def test_derived_campaign_real_aggregation(client, physical, event):
    facility = physical["facility"]
    cases = [{"id": "CASE-01", "event": event, "annual_condition_id": "WIND-01", "annual_condition_probability": 1.,
              "request": physical["request"], "persons_by_receptor": {r["id"]: 1. for r in facility["receptors"]}}]
    campaign = client.campaign(facility, cases)["campaign"]
    contribution = campaign["known_contribution"]["data"]["ir_low_high"]
    assert any(max(values) > 0 for values in contribution.values())
    harm = campaign["cases"][0]["result"]["data"]["receptors"]
    for receptor in harm:
        expected = [event["annual_frequency_per_year"] * h * physical["request"].get("occupancy_probability", 1.)
                    for h in receptor["harm_given_observed_release_low_high"]]
        assert contribution[receptor["id"]] == pytest.approx(expected)
    assert campaign["complete_facility_qra_claim"] is False
    cases[0]["annual_condition_probability"] = .5
    with pytest.raises(PrismError, match="close to one"):
        client.campaign(facility, cases)
    cases[0]["annual_condition_probability"] = 1.
    cases.append({**cases[0], "id": "CASE-DUPLICATE"})
    with pytest.raises(PrismError, match="double count"):
        client.campaign(facility, cases)
