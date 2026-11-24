"""Restricted, explicitly approximate low-demand PFDavg methods.

Numerical SIL range is not functional-safety verification or certification.
Redundant channels are identical and synchronously proof-tested. The beta DU
fraction is removed from independent rates before adding the common-cause term.
DD common cause, staggered/partial tests and online maintenance are not modeled.
"""
from .domain import Model, Nonnegative, Probability, StudySnapshot


class BlockResult(Model):
    block_id: str
    architecture: str
    pfdavg: Probability
    method_id: str
    independent_du_per_h: Nonnegative
    common_du_per_h: Nonnegative


class SilResult(Model):
    sif_id: str
    context_id: str
    pfdavg: Probability
    rrf: float | None
    numerical_sil: str
    blocks: tuple[BlockResult, ...]
    aggregate_method: str = "first_order_sum_not_exact_joint_time_average_v1"
    response_time_s: Nonnegative
    process_safety_time_s: float
    response_time_pass: bool
    overall_verification: str = "REVIEW_REQUIRED"
    architecture_review: str
    systematic_review: str
    input_digest: str
    evidence_status: str
    assumptions: tuple[str, ...]


def numerical_band(pfd: float) -> str:
    if pfd < 0 or pfd > 1:
        raise ValueError("PFD outside [0,1]")
    for level, low, high in ((1, 1e-2, 1e-1), (2, 1e-3, 1e-2), (3, 1e-4, 1e-3), (4, 1e-5, 1e-4)):
        if low <= pfd < high:
            return f"SIL{level}_NUMERIC_ONLY"
    return "BELOW_SIL4_NUMERIC_RANGE_REVIEW_REQUIRED" if pfd < 1e-5 else "NO_SIL_NUMERIC_RANGE"


def calculate(s: StudySnapshot, sif_id: str) -> SilResult:
    sif = s.index("sifs")[sif_id]
    context = s.index("contexts")[sif.context_id]
    if context.measure not in {"average_unavailability", "demand_failure_probability"}:
        raise ValueError("SIF PFDavg needs average or uniform-random-demand context")
    if sif.demand_mode != "low" or not sif.as_good_as_new or not sif.uniform_random_demand:
        raise ValueError("unsupported demand/renewal model; PFH is not PFDavg")
    if sif.cross_block_independence == "UNKNOWN" or (s.mode == "reviewed" and sif.cross_block_independence != "REVIEWED"):
        raise ValueError("SIF cross-block independence must be explicitly assessed")
    results = []
    for b in sif.blocks:
        if b.proof_coverage != 1:
            raise ValueError("partial proof-test coverage requires an unimplemented model")
        du, dd, t, repair, beta = b.lambda_du_per_h, b.lambda_dd_per_h, b.proof_interval_h, b.mttr_h, b.beta_du
        if du * t > .1 or dd * repair > .01 or repair / t > .01:
            raise ValueError("outside first-order small-probability / short-repair applicability")
        if b.architecture == "1oo1":
            if beta != 0:
                raise ValueError("1oo1 beta would double count common cause; model shared events explicitly")
            independent, common = du, 0.0
            pfd = du * (t / 2 + repair) + dd * repair
            method = "handbook_low_demand_1oo1_first_order_v1"
        else:
            if not b.redundant_model_assumptions:
                raise ValueError("redundant method requires explicit identical/synchronous/independent-DD assumptions")
            independent, common = (1 - beta) * du, beta * du
            if b.architecture == "1oo2":
                pfd = independent**2 * t**2 / 3 + independent * dd * repair * t + common * t / 2
            else:
                pfd = independent**2 * t**2 + 3 * independent * dd * repair * t + common * t / 2
            method = f"handbook_{b.architecture}_synchronous_du_beta_split_approx_v1"
        results.append(BlockResult(block_id=b.id, architecture=b.architecture, pfdavg=pfd,
                                   method_id=method, independent_du_per_h=independent, common_du_per_h=common))
    pfd = sum(b.pfdavg for b in results)
    if pfd > .1:
        raise ValueError("aggregate outside first-order applicability; use an explicit time-dependent model")
    response = sif.detection_s + sif.logic_s + sif.final_action_s + sif.process_recovery_s
    return SilResult(sif_id=sif.id, context_id=sif.context_id, pfdavg=pfd,
                     rrf=1 / pfd if pfd else None, numerical_sil=numerical_band(pfd), blocks=tuple(results),
                     response_time_s=response, process_safety_time_s=sif.process_safety_time_s,
                     response_time_pass=response < sif.process_safety_time_s,
                     architecture_review=sif.architecture_review, systematic_review=sif.systematic_review,
                     input_digest=s.digest(), evidence_status="SYNTHETIC" if s.mode == "synthetic" else "REVIEWED_INPUTS",
                     assumptions=("Constant hazards; full synchronous proof tests; as-good-as-new restoration; low demand.",
                                  "Uniform random independent demand; small-probability first-order approximation.",
                                  "Redundancy: identical channels; DU beta split; no DD common cause; short repairs.",
                                  "PFDavg block sum is an approximation; architecture/systematic/lifecycle review remains separate."))
