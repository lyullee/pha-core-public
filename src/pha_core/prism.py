"""PRISM adapter with explicit checkout selection and a separate interpreter.

No global sys.path mutation, model copying, inferred hole size or release history.
PRISM remains optional for the FTA/LOPA/SIL package.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .application import analyze
from .domain import StudySnapshot, canonical, digest
from .storage import code_digest


class PrismError(RuntimeError):
    pass


class PrismClient:
    def __init__(self, root, *, timeout_s=120):
        root = Path(root).resolve()
        if not (root / "src/lh2_tno").is_dir() and (root / "lh2-tno" / "src" / "lh2_tno").is_dir():
            root /= "lh2-tno"
        if not (root / "src" / "lh2_tno").is_dir():
            raise ValueError("PRISM root must contain src/lh2_tno (or lh2-tno/src/lh2_tno)")
        self.root = root
        self.timeout_s = timeout_s

    def call(self, operation, **payload):
        # Installed lh2_tno may resolve to a different checkout. Select only in
        # this child process and verify the imported path again inside worker.
        own_src = Path(__file__).resolve().parents[1]
        bootstrap = "import sys; sys.path[:0]=sys.argv[1:3]; from pha_core.prism_worker import main; main()"
        try:
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", bootstrap, str(self.root / "src"), str(own_src)],
                                       input=canonical({"operation": operation, "root": str(self.root), **payload}),
                                       capture_output=True, encoding="utf-8", cwd=self.root, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise PrismError("PRISM worker timeout; no result promoted") from exc
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PrismError(f"PRISM worker failed: {completed.stderr[-1500:]}") from exc
        if completed.returncode or not result.get("ok"):
            raise PrismError(result.get("error", completed.stderr[-1500:]))
        return result["result"]

    def capabilities(self):
        return self.call("capabilities")

    def build_event(self, snapshot: StudySnapshot, analysis: dict, scenario_id: str, *, port_ids: tuple[str, ...], release: dict, run_id: str | None = None):
        if analysis.get("input_digest") != snapshot.digest() or analysis.get("code_digest") != code_digest():
            raise ValueError("stale analysis baseline/code; recompute before integration")
        recalculated = analyze(snapshot)
        for field in ("fta", "lopa_baseline", "sil", "lopa_proposed"):
            if analysis.get(field) != recalculated[field]:
                raise ValueError("analysis payload differs from reproducible input calculation")
        sc = snapshot.index("scenarios")[scenario_id]
        asset = snapshot.index("assets")[sc.asset_id]
        if not asset.prism_component_id:
            raise ValueError("explicit PRISM component mapping required")
        mapped = {p.prism_port_id for p in snapshot.ports if p.asset_id == asset.id and p.purpose in {"accident_release", "relief_discharge"} and p.direction != "in"}
        if not port_ids or not set(port_ids) <= mapped:
            raise ValueError("reviewed accident/relief port mapping required")
        result = analysis["lopa_proposed"][scenario_id]
        if result["endpoint"] != "loss_of_containment":
            raise ValueError("PRISM v1 requires a LOC frequency before ignition/occupancy; other endpoints need a dedicated adapter")
        rate = snapshot.index("rates")[sc.initiating_rate_id]
        rate_datum = snapshot.index("data")[rate.datum_id]
        capabilities = self.capabilities()
        return {
            "id": f"derived:{scenario_id}:{snapshot.digest()[:12]}", "asset_id": asset.id,
            "model_component_id": asset.prism_component_id, "port_ids": list(port_ids),
            "endpoint": result["endpoint"], "baseline_digest": snapshot.digest(),
            "analysis_digest": digest(analysis), "analysis_code_digest": analysis["code_digest"],
            "prism_source_digest": capabilities["prism_source_digest"],
            "analysis_run_id": run_id or f"reproducible:{digest(analysis)}",
            "scenario_id": scenario_id, "operating_case_id": sc.operating_case_id,
            "source": "pha_core FTA/LOPA/SIL immutable snapshot", "source_sha256": snapshot.digest(),
            "base_frequency_per_year": rate_datum.value,
            "annual_frequency_per_year": result["residual_frequency_per_year"],
            "credits": result["existing_credits"] + result["proposed_credits"],
            "upstream_included_barrier_ids": list(rate.included_barrier_ids),
            "upstream_included_transition_ids": list(rate.included_transition_ids),
            "included_barrier_ids": result["included_barrier_ids"],
            "included_transition_ids": result["included_transition_ids"],
            "release": release, "evidence_status": analysis["evidence_status"],
            "operating_mode": snapshot.index("operating_cases")[sc.operating_case_id].mode,
            "hazop_scenario_id": sc.hazop_row_id or sc.id,
            "limitations": ["Derived frequency, not a literature catalogue event.",
                            "Synthetic or reviewed input calculations do not constitute field validation.",
                            "Provider owns release history, opening, inventory and termination; no complete facility QRA claim."],
        }

    def preflight(self, event, facility, request):
        return self.call("bind", event=event, facility=facility, request=request)

    def evaluate(self, event, facility, request):
        return self.call("evaluate", event=event, facility=facility, request=request)

    def campaign(self, facility, cases):
        return self.call("campaign", facility=facility, cases=cases)
