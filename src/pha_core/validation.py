"""Fail closed on ambiguous semantics and references before any calculation."""
from .domain import Record, Evidence


def validate_snapshot(s):
    fields = tuple(name for name in type(s).model_fields if isinstance(getattr(s, name), tuple))
    all_records = [x for field in fields for x in getattr(s, field)]
    all_records += [block for sif in s.sifs for block in sif.blocks]
    ids = [x.id for x in all_records]
    if len(ids) != len(set(ids)):
        raise ValueError("entity IDs must be globally unique in a baseline")
    evidence = s.index("evidence")
    for obj in all_records:
        if isinstance(obj, Evidence) and s.mode == "reviewed" and obj.review != "REVIEWED":
            raise ValueError(f"{obj.id}: reviewed evidence status required")
        if isinstance(obj, Record):
            for name in type(obj).model_fields:
                value = getattr(obj, name)
                if name.endswith("_ids") and isinstance(value, tuple) and len(value) != len(set(value)):
                    raise ValueError(f"{obj.id}: duplicate reference in {name}")
            if any(ref not in evidence for ref in obj.evidence_ids):
                raise ValueError(f"{obj.id}: unknown evidence reference")
            if s.mode == "reviewed" and obj.review != "REVIEWED":
                raise ValueError(f"{obj.id}: reviewed mode requires reviewed inputs")
    if s.mode == "reviewed" and any(x.source_kind == "synthetic" for x in s.evidence + s.data):
        raise ValueError("synthetic inputs cannot be promoted to reviewed mode")

    def require(field, ref, owner):
        if ref not in s.index(field):
            raise ValueError(f"{owner}: unknown {field} reference {ref}")

    tags = [a.tag for a in s.assets]
    if len(set(tags)) != len(tags):
        raise ValueError("duplicate active asset tag")
    mapped = [a.prism_component_id for a in s.assets if a.prism_component_id]
    if len(set(mapped)) != len(mapped):
        raise ValueError("PRISM component mapping must be unique")
    for a in s.assets:
        if a.parent_asset_id:
            require("assets", a.parent_asset_id, a.id)
    parents = {a.id: (a.parent_asset_id,) if a.parent_asset_id else () for a in s.assets}
    acyclic(parents)
    for p in s.parameters:
        require("assets", p.asset_id, p.id)
        require("operating_cases", p.operating_case_id, p.id)
    for p in s.ports:
        require("assets", p.asset_id, p.id)
        if s.index("assets")[p.asset_id].asset_class != "process_equipment":
            raise ValueError("physical process ports belong to process equipment")
    ports = s.index("ports")
    for c in s.connections:
        require("ports", c.source_port_id, c.id)
        require("ports", c.target_port_id, c.id)
        a, b = ports[c.source_port_id], ports[c.target_port_id]
        if a.id == b.id or a.direction == "in" or b.direction == "out":
            raise ValueError("invalid process connection direction or self-connection")
        if a.property_basis != b.property_basis or (a.phase != b.phase and "any" not in (a.phase, b.phase)):
            raise ValueError("process connection property/phase mismatch")
    valid_functional = set(s.index("assets")) | set(s.index("ipls")) | set(s.index("sifs")) | set(s.index("scenarios"))
    for link in s.functional_links:
        if link.source_id not in valid_functional or link.target_id not in valid_functional or link.source_id == link.target_id:
            raise ValueError(f"{link.id}: invalid functional reference")
    for c in s.contexts:
        require("operating_cases", c.operating_case_id, c.id)
    for d in s.data:
        require("contexts", d.context_id, d.id)
        if d.unit == "1" and d.metric != "conditional_probability" and d.metric != s.index("contexts")[d.context_id].measure:
            raise ValueError(f"{d.id}: probability measure differs from context")
    for e in s.events:
        if e.asset_id:
            require("assets", e.asset_id, e.id)
        if e.datum_id:
            require("data", e.datum_id, e.id)
    logic_ids = set(s.index("events")) | set(s.index("gates")) | set(s.index("trees"))
    graph = {e.id: () for e in s.events}
    for g in s.gates:
        if any(ref not in logic_ids for ref in g.inputs):
            raise ValueError(f"{g.id}: unknown Boolean reference")
        graph[g.id] = g.inputs
    for t in s.trees:
        require("contexts", t.context_id, t.id)
        if t.top_id not in logic_ids:
            raise ValueError(f"{t.id}: unknown top reference")
        graph[t.id] = (t.top_id,)
        for function in t.quantified_function_ids:
            require("ipls", function, t.id)
    acyclic(graph)
    for r in s.rates:
        require("assets", r.asset_id, r.id)
        require("data", r.datum_id, r.id)
        require("operating_cases", r.operating_case_id, r.id)
        datum = s.index("data")[r.datum_id]
        if datum.metric != "occurrence_rate" or s.index("contexts")[datum.context_id].operating_case_id != r.operating_case_id:
            raise ValueError("rate must reference an occurrence rate in the same operating case")
        for ref in r.included_barrier_ids:
            require("ipls", ref, r.id)
        for ref in r.included_transition_ids:
            require("transitions", ref, r.id)
    for ipl in s.ipls:
        if not ipl.asset_ids or len(set(ipl.asset_ids)) != len(ipl.asset_ids):
            raise ValueError("IPL assets must be nonempty and unique")
        for ref in ipl.asset_ids:
            require("assets", ref, ipl.id)
        require({"datum": "data", "fta": "trees", "sif": "sifs"}[ipl.failure_source.kind], ipl.failure_source.id, ipl.id)
        if ipl.failure_source.kind == "fta" and ipl.id not in s.index("trees")[ipl.failure_source.id].quantified_function_ids:
            raise ValueError("FTA must explicitly quantify the IPL function")
    for tr in s.transitions:
        require("data", tr.probability_datum_id, tr.id)
        if s.index("data")[tr.probability_datum_id].metric != "conditional_probability" or tr.source_endpoint == tr.target_endpoint:
            raise ValueError("transition requires a conditional probability between distinct endpoints")
        order = ("protection_demand", "uncontrolled_process_state", "loss_of_containment", "consequence_outcome")
        if order.index(tr.source_endpoint) >= order.index(tr.target_endpoint):
            raise ValueError("transition must progress toward a downstream endpoint")
    for sif in s.sifs:
        require("contexts", sif.context_id, sif.id)
        if not sif.blocks or {b.role for b in sif.blocks} != {"sensor", "logic", "final_element"}:
            raise ValueError("SIF requires sensor, logic and final-element blocks")
        used = [a for b in sif.blocks for a in b.asset_ids]
        if len(used) != len(set(used)):
            raise ValueError("SIF blocks cannot silently share assets")
        for ref in used:
            require("assets", ref, sif.id)
        for b in sif.blocks:
            expected = {"1oo1": 1, "1oo2": 2, "2oo3": 3}[b.architecture]
            if len(b.asset_ids) != expected:
                raise ValueError("voting architecture requires the declared number of distinct channels")
    for sc in s.scenarios:
        require("assets", sc.asset_id, sc.id)
        require("operating_cases", sc.operating_case_id, sc.id)
        require("rates", sc.initiating_rate_id, sc.id)
        require("criteria", sc.criterion_id, sc.id)
        if s.index("rates")[sc.initiating_rate_id].operating_case_id != sc.operating_case_id:
            raise ValueError("scenario/rate operating-case mismatch")
        refs = sc.existing_ipl_ids + sc.proposed_ipl_ids
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate existing/proposed IPL")
        for ref in refs:
            require("ipls", ref, sc.id)
        if len(sc.transition_ids) != len(set(sc.transition_ids)):
            raise ValueError("duplicate transition")
        for ref in sc.transition_ids:
            require("transitions", ref, sc.id)
    for ind in s.independence:
        require("scenarios", ind.scenario_id, ind.id)
        if len(ind.participant_ids) != len(set(ind.participant_ids)):
            raise ValueError("duplicate independence participant")
        for ref in ind.participant_ids:
            if ref not in s.index("rates") and ref not in s.index("ipls"):
                raise ValueError("invalid independence participant")


def acyclic(graph):
    visiting, done = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError(f"cycle in reference graph at {node}")
        if node in done:
            return
        visiting.add(node)
        for child in graph.get(node, ()):
            visit(child)
        visiting.remove(node)
        done.add(node)

    for node in graph:
        visit(node)
