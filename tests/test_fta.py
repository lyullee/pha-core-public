from itertools import product
import math
import random

import pytest

from pha_core import fta
from conftest import parse


def test_design_psv(snapshot):
    result = fta.quantify(snapshot, "FTA-PSV01")
    assert result.probability == pytest.approx(1 - (1 - .006) * (1 - .004))
    assert result.cutsets == (("EV-PSV-MECHANICAL",), ("EV-PSV-PATH-BLOCKED",))
    assert dict(result.birnbaum) == pytest.approx({"EV-PSV-MECHANICAL": .996, "EV-PSV-PATH-BLOCKED": .994})
    assert result.included_barrier_ids == ()
    assert result.quantified_function_ids == ("IPL-PSV01",)


def test_shared_event_absorption(raw):
    a, b = [e["id"] for e in raw["events"]]
    raw["gates"] = [{"id": "G1", "kind": "AND", "inputs": [a, b]},
                    {"id": "G2", "kind": "OR", "inputs": [a, "G1"]}]
    raw["trees"][0]["top_id"] = "G2"
    r = fta.quantify(parse(raw), "FTA-PSV01")
    assert r.probability == .006
    assert r.cutsets == ((a,),)
    assert dict(r.birnbaum)[b] == 0


@pytest.mark.parametrize("seed", range(24))
def test_bdd_against_independent_truth_table(seed, raw):
    rng = random.Random(seed)
    raw["events"], raw["data"] = [], [d for d in raw["data"] if d["metric"] in {"occurrence_rate", "conditional_probability"}]
    probs = [rng.uniform(.01, .5) for _ in range(5)]
    for i, p in enumerate(probs):
        raw["events"].append({"id": f"E{i}", "datum_id": f"D{i}"})
        raw["data"].append({"id": f"D{i}", "metric": "demand_failure_probability", "value": p, "unit": "1",
                            "context_id": raw["contexts"][0]["id"], "source_kind": "synthetic",
                            "population_basis": "item", "exposure_basis": "demand", "failure_mode": "failure"})
    refs = [f"E{i}" for i in range(5)]
    gates = []
    for i in range(7):
        children = rng.sample(refs, rng.randint(2, min(4, len(refs))))
        kind = rng.choice(["AND", "OR", "K_OUT_OF_N"])
        gate = {"id": f"G{i}", "kind": kind, "inputs": children}
        if kind == "K_OUT_OF_N":
            gate["k"] = rng.randint(1, len(children))
        gates.append(gate)
        refs.append(gate["id"])
    raw["gates"] = gates
    raw["trees"][0]["top_id"] = "G6"
    r = fta.quantify(parse(raw), "FTA-PSV01")

    def truth(states):
        state = {f"E{i}": value for i, value in enumerate(states)}
        for g in gates:
            count = sum(state[c] for c in g["inputs"])
            state[g["id"]] = count >= (len(g["inputs"]) if g["kind"] == "AND" else g.get("k", 1))
        return state["G6"]

    expected = sum(math.prod(p if v else 1 - p for p, v in zip(probs, states))
                   for states in product((False, True), repeat=5) if truth(states))
    assert r.probability == pytest.approx(expected, abs=1e-13)
    minimal_truth_sets = []
    for states in product((False, True), repeat=5):
        if truth(states) and all(not truth(tuple(False if j == i else states[j] for j in range(5))) for i in range(5) if states[i]):
            minimal_truth_sets.append(tuple(f"E{i}" for i in range(5) if states[i]))
    assert set(r.cutsets) == set(minimal_truth_sets)
    for var, importance in r.birnbaum:
        index = int(var[1:])
        def fixed_probability(fixed):
            return sum(math.prod(p if v else 1-p for i, (p, v) in enumerate(zip(probs, states)) if i != index)
                       for states in product((False, True), repeat=5) if states[index] == fixed and truth(states))
        assert importance == pytest.approx(fixed_probability(True) - fixed_probability(False), abs=1e-12)
    # Every reported minimal cut set is sufficient and loses sufficiency if
    # any one member is removed, independently of the BDD implementation.
    for cut in r.cutsets:
        state = tuple(f"E{i}" in cut for i in range(5))
        assert truth(state)
        for member in cut:
            assert not truth(tuple(f"E{i}" in cut and f"E{i}" != member for i in range(5)))


def test_cutset_truncation_does_not_truncate_probability(snapshot):
    full = fta.quantify(snapshot, "FTA-PSV01")
    short = fta.quantify(snapshot, "FTA-PSV01", cutset_limit=1)
    exhausted = fta.quantify(snapshot, "FTA-PSV01", cutset_budget=1)
    assert not short.cutset_output_complete and not exhausted.cutset_output_complete
    assert full.probability == short.probability == exhausted.probability
    assert exhausted.probability_calculation_complete
    with pytest.raises(ValueError, match="node budget"):
        fta.quantify(snapshot, "FTA-PSV01", max_nodes=2)


def test_transfer_and_house_events(raw):
    raw["events"].append({"id": "HOUSE", "kind": "house_event", "house_state": True})
    raw["trees"].append({"id": "SECOND", "top_id": "GATE-PSV-UNAVAILABLE", "context_id": raw["contexts"][0]["id"], "cut_boundary": "protection_demand"})
    raw["gates"].append({"id": "TOP", "kind": "AND", "inputs": ["SECOND", "HOUSE"]})
    raw["trees"][0]["top_id"] = "TOP"
    assert fta.quantify(parse(raw), "FTA-PSV01").probability == pytest.approx(.009976)


def test_frequency_and_missing_events_rejected(raw):
    raw["events"][0]["datum_id"] = "DAT-DEMAND-RATE"
    with pytest.raises(ValueError, match="frequency cannot"):
        fta.quantify(parse(raw), "FTA-PSV01")
    raw["events"][0]["datum_id"] = None
    raw["events"][0]["kind"] = "undeveloped_event"
    with pytest.raises(ValueError, match="cannot be quantified"):
        fta.quantify(parse(raw), "FTA-PSV01")


def test_unknown_independence_rejected(raw):
    raw["contexts"][0]["independence"] = "UNKNOWN"
    with pytest.raises(ValueError, match="independence"):
        fta.quantify(parse(raw), "FTA-PSV01")


def test_explicit_rate_binding_preserves_credit(snapshot):
    result = fta.quantify(snapshot, "FTA-PSV01")
    frequency = fta.demand_gated_frequency(snapshot, result, snapshot.rates[0].id,
                                          target_endpoint="uncontrolled_process_state", included_barrier_ids=("IPL-PSV01",), independence_basis="Synthetic independent demand assumption")
    assert frequency.annual_frequency_per_year == pytest.approx(.0009976)
    assert frequency.included_barrier_ids == ("IPL-PSV01",)
    with pytest.raises(ValueError, match="functions explicitly quantified"):
        fta.demand_gated_frequency(snapshot, result, snapshot.rates[0].id,
                                  target_endpoint="uncontrolled_process_state", included_barrier_ids=("IPL-SIF01",), independence_basis="explicit assumption")


def test_explicit_nonrepairable_exponential_adapter():
    assert fta.exponential_mission_probability(.001, 100.) == pytest.approx(1 - math.exp(-.1))
    for rate, duration in ((-.1, 1.), (.01, 0.), (float("inf"), 1.)):
        with pytest.raises(ValueError):
            fta.exponential_mission_probability(rate, duration)
