"""JSON worker entry point; all imports of PRISM stay inside its selected process."""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
from importlib.metadata import PackageNotFoundError, version

from .domain import canonical, digest


def source_digest(root):
    src = root / "src"
    manifest = {p.relative_to(src).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for package in ("lh2_tno", "prism_qra", "prism_lh2_models")
                for p in sorted((src / package).rglob("*.py"))}
    return digest(manifest)


def dispatch(payload):
    import lh2_tno
    from lh2_tno.contracts import Facility
    from lh2_tno.consequences import ConsequenceRequest
    from lh2_tno.derived_frequency import DerivedFrequencyEvent, bind_derived_event
    from lh2_tno import public_backend
    from lh2_tno.backend import IsolatedBackend
    from lh2_tno.pipeline import ResearchPipeline

    root = Path(payload["root"]).resolve()
    if not Path(lh2_tno.__file__).resolve().is_relative_to(root / "src"):
        raise ValueError("PRISM imported from a different checkout")
    sha = source_digest(root)
    identity = {"prism_root": str(root), "imported_module": lh2_tno.__file__, "prism_source_digest": sha,
                "derived_frequency_contract": "FTA_LOPA_DERIVED.v1", "physical_validation_claim": False,
                "complete_facility_qra_claim": False}
    if payload["operation"] == "capabilities":
        config = public_backend.installed_backend_config(require_operations=("integrated_source", "consequence_chain", "facility_risk_aggregate"))
        versions = {}
        for name in ("pydantic", "CoolProp", "numpy", "slabx", "slabx-lh2"):
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                versions[name] = None
        return {**identity, "backend_config_digest": config.digest(),
                "python_executable": sys.executable, "python_version": sys.version,
                "installed_dependencies": versions,
                "backend_interface_audit": public_backend.backend_interface_audit(config)}
    facility = Facility.model_validate_json(canonical(payload["facility"]))
    if payload["operation"] == "campaign":
        from lh2_tno.derived_campaign import evaluate_derived_campaign
        config = public_backend.installed_backend_config(require_operations=("integrated_source", "consequence_chain", "facility_risk_aggregate"))
        pipeline = ResearchPipeline(facility, IsolatedBackend(config))
        return {**identity, "campaign": evaluate_derived_campaign(pipeline, payload["cases"], expected_source_digest=sha)}
    event = DerivedFrequencyEvent.model_validate_json(canonical(payload["event"]))
    if event.prism_source_digest != sha:
        raise ValueError("PRISM source digest changed since handoff preparation")
    request = ConsequenceRequest.model_validate_json(canonical(payload["request"]))
    bound = bind_derived_event(request, event)
    # Facility/port/property ownership is also checked before a real worker
    # run; bind-only preflight uses the same explicit checks here.
    component = next((c for c in facility.components if c.id == event.model_component_id), None)
    if component is None:
        raise ValueError("derived component is absent from PRISM facility")
    providers = (bound.resolved_release_set.releases if bound.resolved_release_set is not None else
                 (bound.resolved_release or bound.resolved_liquid_release,))
    for provider in providers:
        port = next((p for p in facility.ports if p.id == provider.port_id), None)
        if port is None or port.component_id != component.id or port.direction == "in":
            raise ValueError("provider exit is not a matching outward facility port")
        if provider.property_basis != component.property_basis:
            raise ValueError("provider/facility property basis mismatch")
        if port.phase not in (("liquid", "two_phase", "any") if bound.resolved_liquid_release is not None else ("vapour", "any")):
            raise ValueError("provider/facility phase mismatch")
        if port.position_enu_m is not None and port.position_enu_m != provider.position_enu_m:
            raise ValueError("provider/facility position mismatch")
    if payload["operation"] == "bind":
        return {**identity, "status": "BINDING_VALIDATED", "facility_digest": facility.digest(),
                "request_digest": bound.digest(), "bound_request": bound.model_dump(mode="json"),
                "event_digest": event.digest(), "evidence_status": event.evidence_status,
                "physical_consequences_executed": False}
    if payload["operation"] != "evaluate":
        raise ValueError("unknown PRISM worker operation")
    config = public_backend.installed_backend_config(require_operations=("integrated_source", "consequence_chain"))
    result = ResearchPipeline(facility, IsolatedBackend(config)).consequences(bound)
    if result.get("frequency_event") != event.model_dump(mode="json"):
        raise ValueError("derived event provenance lost in PRISM result")
    return {**identity, "status": "EXECUTED", "event_digest": event.digest(), "request_digest": bound.digest(),
            "evidence_status": event.evidence_status, "result_digest": digest(result), "result": result,
            "scope": "one derived event and current weather; not complete annual facility risk"}


def main():
    try:
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(payload)
        print(canonical({"ok": True, "result": result}))
    except Exception as exc:
        print(canonical({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
