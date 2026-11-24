"""Static monotone fault trees using reduced ordered binary decision diagrams.

Repeated event IDs are one Boolean variable, including across transfer references.
Probability is exact for the declared independent basic variables; cutset budgets
never change the probability calculation.
"""
from functools import lru_cache
import math

from .domain import Model, Probability, StudySnapshot


class FtaFrequencyResult(Model):
    tree_id: str
    initiating_rate_id: str
    endpoint: str
    annual_frequency_per_year: float
    probability_given_demand: Probability
    included_barrier_ids: tuple[str, ...]
    method_id: str = "explicit_demand_gated_frequency_v1"
    independence_basis: str
    input_digest: str


def demand_gated_frequency(s: StudySnapshot, result, rate_id: str, *, target_endpoint: str, included_barrier_ids: tuple[str, ...], independence_basis: str):
    """Explicit annual-demand × conditional-failure model; never probability/T.

The caller identifies the barrier factors represented by the whole tree. A
downstream LOPA must consume the resulting rate with this same included ledger.
"""
    rate = s.index("rates")[rate_id]
    d = s.index("data")[rate.datum_id]
    tree = s.index("trees")[result.tree_id]
    if result.input_digest != s.digest() or result.measure != "demand_failure_probability" or d.value is None:
        raise ValueError("current conditional demand probability and initiating rate required")
    if rate.endpoint != "protection_demand" or result.context_id != d.context_id or not independence_basis.strip():
        raise ValueError("explicit independent-demand model in one context required")
    if len(included_barrier_ids) != len(set(included_barrier_ids)) or set(included_barrier_ids) & set(rate.included_barrier_ids):
        raise ValueError("duplicate barrier credit in demand-gated frequency")
    if not set(included_barrier_ids) <= set(result.quantified_function_ids):
        raise ValueError("included barriers must be functions explicitly quantified by the tree")
    if target_endpoint not in {"uncontrolled_process_state", "loss_of_containment"} or (target_endpoint == "loss_of_containment" and tree.cut_boundary != target_endpoint):
        raise ValueError("LOC rate requires a tree explicitly ending at LOC; protection failure alone is not a physical LOC model")
    return FtaFrequencyResult(tree_id=result.tree_id, initiating_rate_id=rate_id, endpoint=target_endpoint,
                              annual_frequency_per_year=d.value * result.probability,
                              probability_given_demand=result.probability,
                              included_barrier_ids=rate.included_barrier_ids + included_barrier_ids,
                              independence_basis=independence_basis, input_digest=s.digest())


def exponential_mission_probability(failure_rate_per_h: float, mission_h: float) -> float:
    """Nonrepairable, constant hazard occurrence within an explicitly stated mission."""
    if not math.isfinite(failure_rate_per_h) or not math.isfinite(mission_h) or failure_rate_per_h < 0 or mission_h <= 0:
        raise ValueError("finite nonnegative failure rate and positive mission required")
    return -math.expm1(-failure_rate_per_h * mission_h)


class FtaResult(Model):
    tree_id: str
    context_id: str
    measure: str
    probability: Probability
    method_id: str = "robdd_static_monotone_v1"
    bdd_nodes: int
    cutsets: tuple[tuple[str, ...], ...]
    cutset_output_complete: bool
    probability_calculation_complete: bool = True
    birnbaum: tuple[tuple[str, float], ...]
    quantified_function_ids: tuple[str, ...]
    included_barrier_ids: tuple[str, ...] = ()
    input_digest: str
    evidence_status: str
    assumptions: tuple[str, ...]


class BDD:
    def __init__(self, variables, max_nodes=100000):
        self.order = {v: i for i, v in enumerate(variables)}
        self.nodes = [None, None]
        self.unique = {}
        self.max_nodes = max_nodes
        self.apply = lru_cache(maxsize=None)(self._apply)

    def node(self, var, lo, hi):
        if lo == hi:
            return lo
        key = (var, lo, hi)
        if key not in self.unique:
            if len(self.nodes) >= self.max_nodes:
                raise ValueError("BDD node budget exceeded; probability not computed")
            self.unique[key] = len(self.nodes)
            self.nodes.append(key)
        return self.unique[key]

    def _apply(self, op, a, b):
        if a > b:
            return self.apply(op, b, a)
        if a == b:
            return a
        if op == "AND":
            if a == 0:
                return 0
            if a == 1:
                return b
        else:
            if a == 1:
                return 1
            if a == 0:
                return b
        av, al, ah = self.nodes[a]
        bv, bl, bh = self.nodes[b]
        var = min(av, bv)
        if av != var:
            al = ah = a
        if bv != var:
            bl = bh = b
        return self.node(var, self.apply(op, al, bl), self.apply(op, ah, bh))

    def probability(self, root, p):
        @lru_cache(maxsize=None)
        def calc(n):
            if n < 2:
                return float(n)
            var, lo, hi = self.nodes[n]
            return (1 - p[var]) * calc(lo) + p[var] * calc(hi)
        return calc(root)


def quantify(s: StudySnapshot, tree_id: str, *, cutset_limit=1000, cutset_budget=20000, max_nodes=100000) -> FtaResult:
    if cutset_limit < 0 or cutset_budget < 1:
        raise ValueError("invalid cutset budget")
    trees, gates, events, data = (s.index(f) for f in ("trees", "gates", "events", "data"))
    tree = trees[tree_id]
    context = s.index("contexts")[tree.context_id]
    if context.measure == "average_unavailability" and context.average_measure_model != "independent_random_test_phases":
        raise ValueError("average unavailability cannot use products of averages without an explicit independent-phase model")
    if context.independence == "UNKNOWN":
        raise ValueError("unknown basic-event independence; explicit common events or reviewed independence required")
    reached, variables, values = set(), [], {}

    def inspect(ref):
        if ref in reached:
            return
        reached.add(ref)
        if ref in trees:
            if trees[ref].context_id != tree.context_id:
                raise ValueError("transfer tree context mismatch")
            inspect(trees[ref].top_id)
        elif ref in gates:
            for child in gates[ref].inputs:
                inspect(child)
        else:
            e = events[ref]
            if e.kind == "house_event":
                return
            if not e.datum_id or data[e.datum_id].value is None:
                raise ValueError(f"{ref}: undeveloped/missing event cannot be quantified")
            d = data[e.datum_id]
            if d.unit != "1" or d.metric != context.measure or d.context_id != context.id:
                raise ValueError(f"{ref}: probability measure/context mismatch; frequency cannot enter Boolean tree")
            variables.append(ref)
            values[ref] = d.value

    inspect(tree_id)
    variables.sort()
    bdd = BDD(variables, max_nodes)

    @lru_cache(maxsize=None)
    def build(ref):
        if ref in trees:
            return build(trees[ref].top_id)
        if ref in events:
            e = events[ref]
            return int(e.house_state) if e.kind == "house_event" else bdd.node(bdd.order[ref], 0, 1)
        gate = gates[ref]
        inputs = [build(child) for child in gate.inputs]
        if gate.kind == "TRANSFER":
            return inputs[0]
        if gate.kind in {"AND", "OR"}:
            root = int(gate.kind == "AND")
            for child in inputs:
                root = bdd.apply(gate.kind, root, child)
            return root
        # DP for at least k failures, also valid when input subtrees overlap.
        dp = [1] + [0] * gate.k
        for child in inputs:
            for j in range(gate.k, 0, -1):
                dp[j] = bdd.apply("OR", dp[j], bdd.apply("AND", child, dp[j - 1]))
        return dp[gate.k]

    root = build(tree_id)
    p = [values[v] for v in variables]
    result = bdd.probability(root, p)
    importance = []
    for i, var in enumerate(variables):
        low, high = p.copy(), p.copy()
        low[i], high[i] = 0.0, 1.0
        importance.append((var, bdd.probability(root, high) - bdd.probability(root, low)))
    used_budget = 0

    @lru_cache(maxsize=None)
    def minimal(n):
        nonlocal used_budget
        if n < 2:
            return (frozenset(),) if n else ()
        var, lo, hi = bdd.nodes[n]
        candidates = set(minimal(lo)) | {c | {variables[var]} for c in minimal(hi)}
        used_budget += len(candidates)
        if used_budget > cutset_budget:
            raise OverflowError
        accepted = []
        for c in sorted(candidates, key=lambda c: (len(c), sorted(c))):
            if not any(a <= c for a in accepted):
                accepted.append(c)
        return tuple(accepted)

    try:
        all_cuts = minimal(root)
        cuts = tuple(tuple(sorted(c)) for c in all_cuts[:cutset_limit])
        complete = len(all_cuts) <= cutset_limit
    except OverflowError:
        cuts, complete = (), False
    # Instance-method memoization must not retain finished BDD instances.
    bdd.apply.cache_clear()
    return FtaResult(tree_id=tree.id, context_id=context.id, measure=context.measure,
                     probability=result, bdd_nodes=len(bdd.nodes), cutsets=cuts,
                     cutset_output_complete=complete, birnbaum=tuple(importance),
                     quantified_function_ids=tree.quantified_function_ids,
                     input_digest=s.digest(), evidence_status="SYNTHETIC" if s.mode == "synthetic" else "REVIEWED_INPUTS",
                     assumptions=(context.independence_basis, "Distinct basic variables independent; dependencies represented by shared event IDs."))
