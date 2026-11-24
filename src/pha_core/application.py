"""Orchestrate pure calculations; capture failures and immutable run dependencies."""
import json
from . import __version__, fta, lopa, sil
from .domain import StudySnapshot
from .storage import Store, code_digest


def existing_sifs(snapshot):
    return {snapshot.index("ipls")[ref].failure_source.id for sc in snapshot.scenarios for ref in sc.existing_ipl_ids
            if snapshot.index("ipls")[ref].failure_source.kind == "sif"}


def analyze(snapshot: StudySnapshot):
    fta_results = {tree.id: fta.quantify(snapshot, tree.id) for tree in snapshot.trees}
    prior_sil = {ref: sil.calculate(snapshot, ref) for ref in existing_sifs(snapshot)}
    before = {sc.id: lopa.calculate(snapshot, sc.id, fta_results, prior_sil, include_proposed=False) for sc in snapshot.scenarios}
    sil_results = {sif.id: sil.calculate(snapshot, sif.id) for sif in snapshot.sifs}
    after = {sc.id: lopa.calculate(snapshot, sc.id, fta_results, sil_results) for sc in snapshot.scenarios}
    return {"schema_version": "pha.analysis.v1", "software_version": __version__, "code_digest": code_digest(),
            "input_digest": snapshot.digest(), "project_id": snapshot.project_id, "baseline_id": snapshot.baseline_id,
            "evidence_status": "SYNTHETIC" if snapshot.mode == "synthetic" else "REVIEWED_INPUTS",
            "fta": {k: v.model_dump(mode="json") for k, v in fta_results.items()},
            "lopa_baseline": {k: v.model_dump(mode="json") for k, v in before.items()},
            "sil": {k: v.model_dump(mode="json") for k, v in sil_results.items()},
            "lopa_proposed": {k: v.model_dump(mode="json") for k, v in after.items()},
            "physical_validation_claim": False, "complete_facility_qra_claim": False}


def run_study(store: Store, baseline_digest: str):
    snapshot = store.load(baseline_digest)
    runs = []

    def execute(module, operation):
        run_id = store.start_run(baseline_digest, module, tuple(runs))
        try:
            result = operation()
            store.finish_run(run_id, result=result)
        except Exception as exc:
            store.finish_run(run_id, error=f"{type(exc).__name__}: {exc}")
            raise
        runs.append(run_id)
        return result

    fta_results = execute("fta", lambda: {t.id: fta.quantify(snapshot, t.id).model_dump(mode="json") for t in snapshot.trees})
    typed_fta = {k: fta.FtaResult.model_validate_json(json.dumps(v)) for k, v in fta_results.items()}
    prior_sil = {ref: sil.calculate(snapshot, ref) for ref in existing_sifs(snapshot)}
    before = execute("lopa_baseline", lambda: {sc.id: lopa.calculate(snapshot, sc.id, typed_fta, prior_sil, include_proposed=False).model_dump(mode="json") for sc in snapshot.scenarios})
    sil_results = execute("sil", lambda: {sf.id: sil.calculate(snapshot, sf.id).model_dump(mode="json") for sf in snapshot.sifs})
    typed_sil = {k: sil.SilResult.model_validate_json(json.dumps(v)) for k, v in sil_results.items()}
    after = execute("lopa_proposed", lambda: {sc.id: lopa.calculate(snapshot, sc.id, typed_fta, typed_sil).model_dump(mode="json") for sc in snapshot.scenarios})
    result = {"schema_version": "pha.analysis.v1", "software_version": __version__, "code_digest": code_digest(),
              "input_digest": baseline_digest, "run_ids": runs.copy(), "fta": fta_results,
              "lopa_baseline": before, "sil": sil_results, "lopa_proposed": after,
              "evidence_status": "SYNTHETIC" if snapshot.mode == "synthetic" else "REVIEWED_INPUTS",
              "physical_validation_claim": False, "complete_facility_qra_claim": False}
    execute("analysis", lambda: result)
    return store.run(runs[-1])
