import math
import pytest

from pha_core import fta, lopa, sil
from pha_core.application import analyze
from conftest import parse


def lopa_result(s):
    return lopa.calculate(s, s.scenarios[0].id, {t.id: fta.quantify(s, t.id) for t in s.trees},
                          {sf.id: sil.calculate(s, sf.id) for sf in s.sifs})


def test_reference_chain(snapshot):
    a = analyze(snapshot)
    sf = a["sil"]["SIF-TK01"]
    r = a["lopa_proposed"][snapshot.scenarios[0].id]
    assert sf["pfdavg"] == pytest.approx(.00482784)
    assert sf["rrf"] == pytest.approx(207.1319679194008)
    assert sf["numerical_sil"] == "SIL2_NUMERIC_ONLY"
    assert sf["overall_verification"] == "REVIEW_REQUIRED"
    assert r["baseline_frequency_per_year"] == pytest.approx(.00019952)
    assert r["residual_frequency_per_year"] == pytest.approx(9.632506368e-7)
    assert r["required_additional_rrf"] == pytest.approx(199.52)
    assert r["target_additional_pfd"] == pytest.approx(.005012028869286287)
    assert sum(o["probability"] for o in r["outcomes"]) == pytest.approx(1)
    assert sum(o["annual_frequency_per_year"] for o in r["outcomes"]) == pytest.approx(.1)
    assert len(r["outcomes"]) == 5
    assert all(o["hazardous_release_not_ruled_out"] for o in r["outcomes"])


@pytest.mark.parametrize("mutation,match", [
    (lambda r: r["rates"][0].update(included_barrier_ids=["IPL-PSV01"]), "duplicate barrier"),
    (lambda r: r["rates"][0].update(included_transition_ids=["TR-UNCONTROLLED-TO-LOC"]), "duplicate transition"),
    (lambda r: r["independence"][0].update(judgment="UNKNOWN"), "independence"),
    (lambda r: r["ipls"][0].update(timeliness="UNKNOWN"), "eligibility"),
    (lambda r: r["criteria"][0].update(endpoint="consequence_outcome"), "endpoint"),
    (lambda r: r["rates"][0].update(endpoint="loss_of_containment"), "cut boundary"),
    (lambda r: r["sifs"][0].update(final_action_s=20.), "too slow"),
])
def test_lopa_semantic_guards(raw, mutation, match):
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        lopa_result(parse(raw))


def test_shared_power_is_not_independence(raw):
    raw["assets"].append({"id": "POWER", "tag": "POWER", "asset_class": "utility", "asset_type": "power"})
    for target in ("AST-PSV01", "AST-PT01"):
        raw["functional_links"].append({"id": f"POWER-{target}", "source_id": "POWER", "target_id": target, "relation": "power_supply"})
    with pytest.raises(ValueError, match="share assets/support"):
        lopa_result(parse(raw))


def test_initiating_equipment_shared_with_protection_rejected(raw):
    raw["rates"][0]["asset_id"] = "AST-PSV01"
    with pytest.raises(ValueError, match="share assets/support"):
        lopa_result(parse(raw))


def test_physical_transition_cannot_be_bypassed(raw):
    raw["scenarios"][0].update(post_barrier_endpoint="loss_of_containment", transition_ids=[])
    with pytest.raises(ValueError, match="explicit physical transition"):
        lopa_result(parse(raw))


def test_zero_frequency_has_finite_json(raw):
    next(d for d in raw["data"] if d["metric"] == "occurrence_rate")["value"] = 0.
    r = lopa_result(parse(raw))
    assert r.required_additional_rrf == r.target_additional_pfd == 1.
    assert r.residual_frequency_per_year == 0
    assert "Infinity" not in r.model_dump_json()


@pytest.mark.parametrize("mutation,match", [
    (lambda sf: sf.update(demand_mode="high"), "unsupported demand"),
    (lambda sf: sf.update(as_good_as_new=False), "unsupported demand"),
    (lambda sf: sf.update(cross_block_independence="UNKNOWN"), "independence"),
    (lambda sf: sf["blocks"][0].update(proof_coverage=.9), "partial"),
    (lambda sf: sf["blocks"][0].update(lambda_du_per_h=.1), "applicability"),
    (lambda sf: sf["blocks"][0].update(beta_du=.1), "double count"),
])
def test_sif_model_scope(raw, mutation, match):
    mutation(raw["sifs"][0])
    with pytest.raises(ValueError, match=match):
        sil.calculate(parse(raw), "SIF-TK01")


def test_response_time_equality_fails(raw):
    raw["sifs"][0]["process_safety_time_s"] = 6.1
    assert not sil.calculate(parse(raw), "SIF-TK01").response_time_pass


def test_proof_interval_monotonicity_and_1oo1_integral(raw):
    base = sil.calculate(parse(raw), "SIF-TK01")
    b = raw["sifs"][0]["blocks"][0]
    # Independent numerical integration of the specified first-order q(t),
    # midpoint quadrature is exact for this linear model.
    q = sum(b["lambda_du_per_h"] * (b["proof_interval_h"] * (i + .5) / 1000 + b["mttr_h"]) + b["lambda_dd_per_h"] * b["mttr_h"] for i in range(1000)) / 1000
    assert base.blocks[0].pfdavg == pytest.approx(q)
    b["proof_interval_h"] *= 2
    assert sil.calculate(parse(raw), "SIF-TK01").pfdavg > base.pfdavg


@pytest.mark.parametrize("architecture,n,coefficient", [("1oo2", 2, 1/3), ("2oo3", 3, 1.)])
def test_redundant_du_only_against_time_polynomial(raw, architecture, n, coefficient):
    b = raw["sifs"][0]["blocks"][0]
    b.update(architecture=architecture, redundant_model_assumptions=True, lambda_dd_per_h=0., mttr_h=0.)
    for i in range(1, n):
        raw["assets"].append({"id": f"EXTRA{i}", "tag": f"EXTRA{i}", "asset_class": "instrument", "asset_type": "sensor"})
        b["asset_ids"].append(f"EXTRA{i}")
    expected = coefficient * b["lambda_du_per_h"]**2 * b["proof_interval_h"]**2
    assert sil.calculate(parse(raw), "SIF-TK01").blocks[0].pfdavg == pytest.approx(expected)
    b["beta_du"] = 1.
    assert sil.calculate(parse(raw), "SIF-TK01").blocks[0].pfdavg == pytest.approx(b["lambda_du_per_h"] * b["proof_interval_h"] / 2)


@pytest.mark.parametrize("pfd,band", [(.1,"NO_SIL_NUMERIC_RANGE"), (.01,"SIL1_NUMERIC_ONLY"), (.001,"SIL2_NUMERIC_ONLY"),
                                    (.0001,"SIL3_NUMERIC_ONLY"), (.00001,"SIL4_NUMERIC_ONLY"), (0.,"BELOW_SIL4_NUMERIC_RANGE_REVIEW_REQUIRED")])
def test_sil_numeric_boundaries(pfd, band):
    assert sil.numerical_band(pfd) == band


def test_existing_sif_is_supported(raw):
    raw["scenarios"][0].update(existing_ipl_ids=["IPL-PSV01", "IPL-SIF01"], proposed_ipl_ids=[])
    a = analyze(parse(raw))
    sc = raw["scenarios"][0]["id"]
    assert a["lopa_baseline"][sc]["residual_frequency_per_year"] == a["lopa_proposed"][sc]["residual_frequency_per_year"]


def test_aggregate_rejects_duplicate_initiator(snapshot):
    result = lopa_result(snapshot)
    with pytest.raises(ValueError, match="unique contributors"):
        lopa.aggregate_endpoint((result, result), 1e-6)
