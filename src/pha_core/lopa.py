"""Endpoint-aware LOPA and an explicit, once-only probability credit ledger."""
from itertools import product
import math

from .domain import Model, Nonnegative, Probability, StudySnapshot
from .fta import FtaResult
from .sil import SilResult


class Credit(Model):
    factor_id: str
    kind: str
    probability: Probability
    owner: str
    source_id: str


class Outcome(Model):
    id: str
    probability: Probability
    annual_frequency_per_year: Nonnegative
    failed_barrier_ids: tuple[str, ...]
    successful_barrier_ids: tuple[str, ...]
    endpoint: str
    hazardous_release_not_ruled_out: bool = True


class LopaResult(Model):
    scenario_id: str
    initiating_rate_id: str
    endpoint: str
    baseline_frequency_per_year: Nonnegative
    residual_frequency_per_year: Nonnegative
    target_frequency_per_year: float
    required_additional_rrf: float
    target_additional_pfd: Probability
    numerical_target_pass: bool
    overall_assessment: str = "REVIEW_REQUIRED"
    existing_credits: tuple[Credit, ...]
    proposed_credits: tuple[Credit, ...]
    included_barrier_ids: tuple[str, ...]
    included_transition_ids: tuple[str, ...]
    outcomes: tuple[Outcome, ...]
    input_digest: str
    evidence_status: str
    assumptions: tuple[str, ...]


def _probability(s, ipl, fta_results, sil_results, rate):
    source = ipl.failure_source
    context = s.index("contexts")[s.index("data")[rate.datum_id].context_id]
    if source.kind == "datum":
        datum = s.index("data")[source.id]
        if datum.metric not in {"demand_failure_probability", "average_unavailability"} or datum.value is None:
            raise ValueError("IPL needs an available demand failure probability, not a rate/mission probability")
        value, ctx = datum.value, s.index("contexts")[datum.context_id]
    elif source.kind == "fta":
        result: FtaResult = fta_results[source.id]
        if result.input_digest != s.digest() or result.measure not in {"demand_failure_probability", "average_unavailability"}:
            raise ValueError("FTA stale or incompatible with random demand")
        value, ctx = result.probability, s.index("contexts")[result.context_id]
    else:
        result: SilResult = sil_results[source.id]
        if result.input_digest != s.digest() or not result.response_time_pass:
            raise ValueError("SIF stale or too slow for process safety time")
        value, ctx = result.pfdavg, s.index("contexts")[result.context_id]
    if ctx.operating_case_id != context.operating_case_id or ctx.id != context.id:
        raise ValueError("IPL/rate context mismatch; explicit context conversion required")
    if ctx.measure == "average_unavailability" and ctx.average_measure_model != "independent_random_test_phases":
        raise ValueError("products of average unavailabilities require an independent-phase model")
    states = (ipl.effectiveness, ipl.timeliness, ipl.proof_testing)
    accepted = {"REVIEWED", "ASSUMED"} if s.mode == "synthetic" else {"REVIEWED"}
    if any(state not in accepted for state in states):
        raise ValueError(f"{ipl.id}: IPL eligibility unknown/incomplete")
    return value


def calculate(s: StudySnapshot, scenario_id: str, fta_results: dict[str, FtaResult], sil_results: dict[str, SilResult], *, include_proposed=True) -> LopaResult:
    sc = s.index("scenarios")[scenario_id]
    rate = s.index("rates")[sc.initiating_rate_id]
    datum = s.index("data")[rate.datum_id]
    if datum.value is None:
        raise ValueError("initiating frequency missing")
    criterion = s.index("criteria")[sc.criterion_id]
    ids = sc.existing_ipl_ids + (sc.proposed_ipl_ids if include_proposed else ())
    if set(ids) & set(rate.included_barrier_ids):
        raise ValueError("duplicate barrier probability credit already included in upstream rate")
    if set(sc.transition_ids) & set(rate.included_transition_ids):
        raise ValueError("duplicate transition probability credit")
    if ids and rate.endpoint != "protection_demand":
        raise ValueError("new preventive IPL credit requires a protection-demand cut boundary")
    if ids and sc.post_barrier_endpoint != "uncontrolled_process_state":
        raise ValueError("protection failure must enter uncontrolled state; an explicit physical transition is required for LOC")
    if criterion.scope != sc.id:
        raise ValueError("scenario must use its own criterion scope; group targets require explicit aggregation")
    if len(ids) > 12:
        raise ValueError("outcome partition exceeds 12-barrier budget")
    participants = {rate.id, *ids}
    accepted = {"REVIEWED", "ASSUMED"} if s.mode == "synthetic" else {"REVIEWED"}
    if ids and not any(a.scenario_id == sc.id and participants <= set(a.participant_ids) and a.judgment in accepted for a in s.independence):
        raise ValueError("scenario independence not established for initiating event and all credited IPLs")
    ipls = [s.index("ipls")[i] for i in ids]
    # Shared assets and shared support paths cannot be repaired by a checkbox.
    support = {a.id: {a.id} for a in s.assets}
    changed = True
    while changed:
        changed = False
        for link in s.functional_links:
            if link.relation in {"power_supply", "instrument_air", "shared_environment"} and link.source_id in support and link.target_id in support:
                previous = len(support[link.target_id])
                support[link.target_id] |= support[link.source_id]
                changed |= len(support[link.target_id]) != previous
    def source_assets_and_events(ipl):
        assets, basic_ids = set(ipl.asset_ids), set()
        ref = ipl.failure_source
        if ref.kind == "sif":
            assets |= {a for b in s.index("sifs")[ref.id].blocks for a in b.asset_ids}
        if ref.kind == "fta":
            visited = set()

            def trace(node):
                if node in visited:
                    return
                visited.add(node)
                if node in s.index("trees"):
                    trace(s.index("trees")[node].top_id)
                elif node in s.index("gates"):
                    for child in s.index("gates")[node].inputs:
                        trace(child)
                else:
                    event = s.index("events")[node]
                    if event.kind != "house_event":
                        basic_ids.add(f"event:{node}")
                        if event.asset_id:
                            assets.add(event.asset_id)
            trace(ref.id)
        return set().union(*(support[a] for a in assets)) | basic_ids

    dependencies = [support[rate.asset_id]] + [source_assets_and_events(ipl) for ipl in ipls]
    for i, dep in enumerate(dependencies):
        if any(dep & other for other in dependencies[i + 1:]):
            raise ValueError("credited IPLs share assets/support; explicit joint model required")
    probs = [_probability(s, ipl, fta_results, sil_results, rate) for ipl in ipls]
    credits = tuple(Credit(factor_id=i.id, kind="barrier_failure", probability=p, owner=sc.id, source_id=i.failure_source.id) for i, p in zip(ipls, probs))
    endpoint = sc.post_barrier_endpoint if ids else rate.endpoint
    transitions, tp = [], 1.0
    for ref in sc.transition_ids:
        tr = s.index("transitions")[ref]
        d = s.index("data")[tr.probability_datum_id]
        if tr.source_endpoint != endpoint or d.value is None or d.context_id != datum.context_id:
            raise ValueError("transition chain endpoint/context mismatch or missing probability")
        if s.mode == "reviewed" and not tr.physical_model_ref:
            raise ValueError("reviewed LOC transition requires physical-model/evidence reference")
        transitions.append(Credit(factor_id=ref, kind="conditional_transition", probability=d.value, owner=sc.id, source_id=d.id))
        tp *= d.value
        endpoint = tr.target_endpoint
    if endpoint != criterion.endpoint:
        raise ValueError("criterion endpoint differs from calculated frequency; occupancy/ignition cannot change a LOC target")
    count = len(sc.existing_ipl_ids)
    baseline = datum.value * math.prod(probs[:count]) * tp
    residual = baseline * math.prod(probs[count:])
    required = max(1.0, baseline / criterion.value_per_year)
    target_pfd = min(1.0, criterion.value_per_year / baseline) if baseline else 1.0
    outcomes = []
    # Include successful branches. A successful PSV may itself discharge hazardous fluid.
    for n, states in enumerate(product((False, True), repeat=len(ids))):
        probability = math.prod(p if fail else 1 - p for p, fail in zip(probs, states))
        failed = tuple(i for i, fail in zip(ids, states) if fail)
        success = tuple(i for i, fail in zip(ids, states) if not fail)
        if not all(states):
            outcomes.append(Outcome(id=f"{sc.id}:outcome:{n}", probability=probability,
                                    annual_frequency_per_year=datum.value * probability, failed_barrier_ids=failed,
                                    successful_barrier_ids=success, endpoint="protected_state_requires_consequence_review"))
            continue
        remaining = probability
        for j, credit in enumerate(transitions):
            rejected = remaining * (1 - credit.probability)
            outcomes.append(Outcome(id=f"{sc.id}:outcome:{n}:transition:{j}:no", probability=rejected,
                                    annual_frequency_per_year=datum.value * rejected, failed_barrier_ids=failed,
                                    successful_barrier_ids=success, endpoint=f"{credit.factor_id}:not_transitioned"))
            remaining *= credit.probability
        outcomes.append(Outcome(id=f"{sc.id}:outcome:{n}:target", probability=remaining,
                                annual_frequency_per_year=datum.value * remaining, failed_barrier_ids=failed,
                                successful_barrier_ids=success, endpoint=endpoint))
    if not math.isclose(sum(o.probability for o in outcomes), 1.0, abs_tol=1e-12, rel_tol=0):
        raise ValueError("outcome partition does not close")
    return LopaResult(scenario_id=sc.id, initiating_rate_id=rate.id, endpoint=endpoint, baseline_frequency_per_year=baseline,
                      residual_frequency_per_year=residual, target_frequency_per_year=criterion.value_per_year,
                      required_additional_rrf=required, target_additional_pfd=target_pfd,
                      numerical_target_pass=residual <= criterion.value_per_year,
                      existing_credits=credits[:count] + tuple(transitions), proposed_credits=credits[count:],
                      included_barrier_ids=rate.included_barrier_ids + ids,
                      included_transition_ids=rate.included_transition_ids + sc.transition_ids,
                      outcomes=tuple(outcomes), input_digest=s.digest(),
                      evidence_status="SYNTHETIC" if s.mode == "synthetic" else "REVIEWED_INPUTS",
                      assumptions=("Conditional protection failures independent at declared random demand.",
                                   "Successful protection branches require their own consequence review.",
                                   "Numerical frequency target is not complete facility individual risk."))


def aggregate_endpoint(results: tuple[LopaResult, ...], target_per_year: float) -> dict:
    if not results or not math.isfinite(target_per_year) or target_per_year <= 0:
        raise ValueError("positive target and contributors required")
    if len({r.scenario_id for r in results}) != len(results) or len({r.initiating_rate_id for r in results}) != len(results) or len({(r.endpoint, r.input_digest) for r in results}) != 1:
        raise ValueError("aggregation requires unique contributors in one endpoint/baseline")
    total = sum(r.residual_frequency_per_year for r in results)
    return {"endpoint": results[0].endpoint, "frequency_per_year": total,
            "numerical_target_pass": total <= target_per_year,
            "assumption": "Contributor events are distinct; shared initiating-event partitions must be resolved before summation."}
