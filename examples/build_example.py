"""Build the operational synthetic fixture from the reviewed architecture fixture.

Only this synthetic example maps the design tank to the PRISM demo 'storage'
component. No field geometry or provider release is inferred.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
source = Path(__file__).parents[1] / "design_examples/tank_overpressure.design.json"
if not source.is_file():
    source = ROOT / "design_examples/tank_overpressure.design.json"
d = json.loads(source.read_text(encoding="utf-8"))


def rec(id, **kwargs):
    return {"id": id, "review": "SYNTHETIC", **kwargs}


ctx = d["evaluation_context"]["id"]
scenario = d["scope"]["scenario_id"]
op = d["scope"]["operating_case_id"]
rate = d["initiating_rate"]["id"]
snapshot = {
    "project_id": "PHA-DEMO", "study_id": "STUDY-01", "baseline_id": "BL-01", "mode": "synthetic",
    "change_reason": "Operational synthetic fixture; explicit demo mapping to PRISM storage/storage.out.",
    "evidence": [{"id": "EVID-DESIGN", "original_name": source.name,
                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "section": "synthetic architecture fixture", "source_kind": "synthetic"}],
    "assets": [rec(a["id"], tag=a["tag"], asset_class=a["asset_class"], asset_type=a["asset_type"],
                   prism_component_id="storage" if a["id"] == "AST-TK01" else None) for a in d["assets"]],
    "ports": [rec("PORT-TK-ACCIDENT", asset_id="AST-TK01", purpose="accident_release", direction="out", phase="vapour",
                  property_basis="Hydrogen:CoolProp-HEOS:Hydrogen:native", prism_port_id="storage.out")],
    "functional_links": [rec("LINK-MEASURES", source_id="AST-PT01", target_id="AST-TK01", relation="measures"),
                         rec("LINK-SIGNAL", source_id="AST-PT01", target_id="AST-SIS01", relation="signal_to"),
                         rec("LINK-COMMAND", source_id="AST-SIS01", target_id="AST-XV01", relation="commands")],
    "operating_cases": [rec(op, mode="receiving", annual_exposure_basis="Demand rate already includes calendar-year mode exposure; do not multiply again.")],
    "contexts": [rec(ctx, measure="demand_failure_probability", operating_case_id=op,
                     time_model="uniform independent random demand, synthetic stationary assumptions",
                     independence="EXPLICIT_COMMON_EVENTS", independence_basis=d["evaluation_context"]["independence_assumption"])],
    "data": [rec(x["id"], metric=x["metric"], value=x["value"], unit=x["unit"], context_id=ctx,
                 source_kind="synthetic", population_basis="one declared function/demand", exposure_basis="conditional on independent demand",
                 failure_mode="synthetic example") for x in d["quantitative_data"]] +
            [rec("DAT-DEMAND-RATE", metric="occurrence_rate", value=.1, unit="year^-1", context_id=ctx,
                 source_kind="synthetic", population_basis="one system", exposure_basis="calendar year, mode exposure already included", failure_mode="overpressure demand")],
    "events": [rec(x["id"], asset_id=x["asset_id"], datum_id=x["datum_id"]) for x in d["basic_events"]],
    "gates": [rec("GATE-PSV-UNAVAILABLE", kind="OR", inputs=[x["id"] for x in d["basic_events"]])],
    "trees": [rec("FTA-PSV01", top_id="GATE-PSV-UNAVAILABLE", context_id=ctx, cut_boundary="protection_demand", quantified_function_ids=["IPL-PSV01"])],
    "rates": [rec(rate, asset_id="AST-PCV01", datum_id="DAT-DEMAND-RATE", operating_case_id=op, endpoint="protection_demand", conditioning="already exposure-adjusted random overpressure demand")],
    "ipls": [rec("IPL-PSV01", asset_ids=["AST-PSV01", "AST-PSVPATH01"], failure_source={"kind": "fta", "id": "FTA-PSV01"},
                 effectiveness="ASSUMED", timeliness="ASSUMED", proof_testing="ASSUMED", qualification_basis="Explicit synthetic assumption only"),
             rec("IPL-SIF01", asset_ids=["AST-PT01", "AST-SIS01", "AST-XV01"], failure_source={"kind": "sif", "id": "SIF-TK01"},
                 effectiveness="ASSUMED", timeliness="ASSUMED", proof_testing="ASSUMED", qualification_basis="Proposed synthetic protection")],
    "independence": [rec("IND-TK01", scenario_id=scenario, participant_ids=[rate, "IPL-PSV01", "IPL-SIF01"], judgment="ASSUMED", rationale="Distinct independent demand and PSV/SIF failure mechanisms assumed only for synthetic demonstration")],
    "transitions": [rec("TR-UNCONTROLLED-TO-LOC", source_endpoint="uncontrolled_process_state", target_endpoint="loss_of_containment",
                        probability_datum_id="DAT-LOC-TRANSITION", conditioning="all applicable protection functions failed", physical_model_ref=None)],
    "criteria": [rec(d["criterion"]["id"], endpoint="loss_of_containment", value_per_year=1e-6, scope=scenario, basis="Synthetic organizational LOC target, not regulatory or individual risk")],
    "sifs": [rec("SIF-TK01", context_id=ctx, cross_block_independence="ASSUMED", detection_s=1., logic_s=.1, final_action_s=5., process_safety_time_s=20.,
                 blocks=[rec(x["id"], asset_ids=[x["asset_id"]], role=x["role"], architecture="1oo1", lambda_du_per_h=x["lambda_du_per_h"],
                             lambda_dd_per_h=x["lambda_dd_per_h"], proof_interval_h=8760., mttr_h=8.) for x in d["sif"]["elements"]])],
    "scenarios": [rec(scenario, asset_id="AST-TK01", operating_case_id=op, deviation="tank overpressure", initiating_rate_id=rate,
                      criterion_id=d["criterion"]["id"], existing_ipl_ids=["IPL-PSV01"], proposed_ipl_ids=["IPL-SIF01"],
                      transition_ids=["TR-UNCONTROLLED-TO-LOC"], hazop_row_id="HAZOP-DEMO-01")],
}
from pha_core.domain import StudySnapshot
validated = StudySnapshot.model_validate_json(json.dumps(snapshot))
target = Path(__file__).parent / "tank_overpressure.snapshot.json"
target.write_text(json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
resource = Path(__file__).parents[1] / "src/pha_core/resources" / target.name
resource.parent.mkdir(parents=True, exist_ok=True)
resource.write_bytes(target.read_bytes())
print(target)
