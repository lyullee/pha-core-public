"""Versioned, immutable inputs. Physical, functional and Boolean graphs stay separate."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:/-]+$")]
Nonnegative = Annotated[float, Field(ge=0)]
Positive = Annotated[float, Field(gt=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Review = Literal["DRAFT", "SYNTHETIC", "REVIEW_REQUIRED", "REVIEWED", "REJECTED"]
Endpoint = Literal["protection_demand", "uncontrolled_process_state", "loss_of_containment", "consequence_outcome"]
Measure = Literal["demand_failure_probability", "point_unavailability", "average_unavailability", "mission_probability", "conditional_probability"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Record(Model):
    id: ID
    revision: Annotated[int, Field(ge=1)] = 1
    review: Review = "DRAFT"
    evidence_ids: tuple[ID, ...] = ()

    @model_validator(mode="after")
    def reviewed_has_evidence(self):
        if self.review == "REVIEWED" and not self.evidence_ids:
            raise ValueError("REVIEWED requires evidence references")
        return self


class Evidence(Model):
    id: ID
    revision: Annotated[int, Field(ge=1)] = 1
    review: Review = "DRAFT"
    original_name: str
    sha256: SHA256
    section: str
    source_kind: Literal["published", "vendor", "observed", "derived", "synthetic"]


class Asset(Record):
    tag: Annotated[str, Field(min_length=1)]
    asset_class: Literal["process_equipment", "instrument", "logic", "electrical", "utility", "support"]
    asset_type: str
    parent_asset_id: ID | None = None
    prism_component_id: ID | None = None


class Parameter(Record):
    asset_id: ID
    name: str
    value: float
    unit: Literal["Pa_abs", "K", "m", "m2", "m3", "kg", "kg/s", "s", "1"]
    original_value: float
    original_unit: str
    operating_case_id: ID

    @model_validator(mode="after")
    def physical_value(self):
        if self.value < 0 or (self.unit in {"Pa_abs", "K"} and self.value == 0):
            raise ValueError("absolute state and dimensional parameters require physically valid nonnegative values")
        return self


class Port(Record):
    asset_id: ID
    purpose: Literal["normal_process", "normal_vent", "accident_release", "relief_discharge"]
    direction: Literal["in", "out", "bidirectional"]
    phase: Literal["liquid", "vapour", "two_phase", "any"]
    property_basis: str
    prism_port_id: ID | None = None


class Connection(Record):
    source_port_id: ID
    target_port_id: ID


class FunctionalLink(Record):
    source_id: ID
    target_id: ID
    relation: Literal["measures", "signal_to", "commands", "power_supply", "instrument_air", "protects", "shared_environment"]


class OperatingCase(Record):
    mode: str
    annual_exposure_basis: str


class Context(Record):
    measure: Measure
    operating_case_id: ID
    time_model: str
    mission_duration_h: Positive | None = None
    evaluation_time_h: Nonnegative | None = None
    independence: Literal["UNKNOWN", "INDEPENDENT", "EXPLICIT_COMMON_EVENTS"] = "UNKNOWN"
    independence_basis: str
    average_measure_model: Literal["UNSPECIFIED", "independent_random_test_phases"] = "UNSPECIFIED"

    @model_validator(mode="after")
    def time_required(self):
        if self.measure == "mission_probability" and self.mission_duration_h is None:
            raise ValueError("mission probability requires mission duration")
        if self.measure == "point_unavailability" and self.evaluation_time_h is None:
            raise ValueError("point unavailability requires evaluation time")
        return self


class Datum(Record):
    metric: Literal["occurrence_rate", "failure_rate", "demand_failure_probability", "point_unavailability", "average_unavailability", "mission_probability", "conditional_probability"]
    value: Nonnegative | None
    unit: Literal["year^-1", "h^-1", "1"]
    context_id: ID
    source_kind: Literal["published", "vendor", "observed", "derived", "synthetic"]
    population_basis: str
    exposure_basis: str
    failure_mode: str
    missing_reason: str | None = None

    @model_validator(mode="after")
    def dimensions(self):
        expected = "year^-1" if self.metric == "occurrence_rate" else "h^-1" if self.metric == "failure_rate" else "1"
        if self.unit != expected:
            raise ValueError("metric/unit mismatch; explicit conversion required")
        if self.unit == "1" and self.value is not None and self.value > 1:
            raise ValueError("probability must be in [0,1]")
        if self.value is None and not self.missing_reason:
            raise ValueError("missing datum needs missing_reason; missing is not zero")
        return self


class BasicEvent(Record):
    asset_id: ID | None = None
    datum_id: ID | None = None
    house_state: bool | None = None
    kind: Literal["basic_failure", "external_event", "house_event", "undeveloped_event"] = "basic_failure"

    @model_validator(mode="after")
    def source(self):
        if self.kind == "house_event":
            if self.house_state is None or self.datum_id is not None:
                raise ValueError("house event needs only an explicit true/false state")
        elif self.house_state is not None:
            raise ValueError("only house events accept house_state")
        if self.kind != "undeveloped_event" and self.kind != "house_event" and self.datum_id is None:
            raise ValueError("basic event requires datum")
        return self


class Gate(Record):
    kind: Literal["AND", "OR", "K_OUT_OF_N", "TRANSFER"]
    inputs: tuple[ID, ...]
    k: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def voting(self):
        if not self.inputs or len(set(self.inputs)) != len(self.inputs):
            raise ValueError("gate inputs must be nonempty and unique; repeated events remain shared across gates")
        if self.kind == "TRANSFER" and len(self.inputs) != 1:
            raise ValueError("transfer requires exactly one reference")
        if self.kind == "K_OUT_OF_N":
            if self.k is None or self.k > len(self.inputs):
                raise ValueError("invalid k-out-of-n")
        elif self.k is not None:
            raise ValueError("k is only valid for voting gates")
        return self


class FaultTree(Record):
    top_id: ID
    context_id: ID
    cut_boundary: Endpoint
    quantified_function_ids: tuple[ID, ...] = ()


class Rate(Record):
    asset_id: ID
    datum_id: ID
    operating_case_id: ID
    endpoint: Endpoint
    included_barrier_ids: tuple[ID, ...] = ()
    included_transition_ids: tuple[ID, ...] = ()
    conditioning: str


class ResultRef(Model):
    kind: Literal["datum", "fta", "sif"]
    id: ID


class IPL(Record):
    asset_ids: tuple[ID, ...]
    failure_source: ResultRef
    effectiveness: Literal["UNKNOWN", "ASSUMED", "REVIEWED"] = "UNKNOWN"
    timeliness: Literal["UNKNOWN", "ASSUMED", "REVIEWED"] = "UNKNOWN"
    proof_testing: Literal["UNKNOWN", "ASSUMED", "REVIEWED"] = "UNKNOWN"
    qualification_basis: str


class Independence(Record):
    scenario_id: ID
    participant_ids: tuple[ID, ...]
    judgment: Literal["UNKNOWN", "DEPENDENT", "ASSUMED", "REVIEWED"]
    rationale: str


class Transition(Record):
    source_endpoint: Endpoint
    target_endpoint: Endpoint
    probability_datum_id: ID
    conditioning: str
    physical_model_ref: str | None = None


class Criterion(Record):
    endpoint: Endpoint
    value_per_year: Positive
    scope: str
    basis: str


class SifBlock(Record):
    asset_ids: tuple[ID, ...]
    role: Literal["sensor", "logic", "final_element"]
    architecture: Literal["1oo1", "1oo2", "2oo3"] = "1oo1"
    lambda_du_per_h: Nonnegative
    lambda_dd_per_h: Nonnegative
    proof_interval_h: Positive
    mttr_h: Nonnegative
    proof_coverage: Probability = 1.0
    beta_du: Probability = 0.0
    redundant_model_assumptions: bool = False
    repair_policy: Literal["degraded_until_repaired"] = "degraded_until_repaired"


class SIF(Record):
    context_id: ID
    blocks: tuple[SifBlock, ...]
    demand_mode: Literal["low", "high", "continuous"] = "low"
    as_good_as_new: bool = True
    uniform_random_demand: bool = True
    cross_block_independence: Literal["UNKNOWN", "ASSUMED", "REVIEWED"] = "UNKNOWN"
    detection_s: Nonnegative
    logic_s: Nonnegative
    final_action_s: Nonnegative
    process_recovery_s: Nonnegative = 0.0
    process_safety_time_s: Positive
    architecture_review: Literal["REVIEW_REQUIRED", "REVIEWED"] = "REVIEW_REQUIRED"
    systematic_review: Literal["REVIEW_REQUIRED", "REVIEWED"] = "REVIEW_REQUIRED"


class Scenario(Record):
    asset_id: ID
    operating_case_id: ID
    deviation: str
    initiating_rate_id: ID
    criterion_id: ID
    existing_ipl_ids: tuple[ID, ...] = ()
    proposed_ipl_ids: tuple[ID, ...] = ()
    post_barrier_endpoint: Endpoint = "uncontrolled_process_state"
    transition_ids: tuple[ID, ...] = ()
    hazop_row_id: str | None = None


class StudySnapshot(Model):
    schema_version: Literal["pha.snapshot.v1"] = "pha.snapshot.v1"
    project_id: ID
    study_id: ID
    baseline_id: ID
    revision: Annotated[int, Field(ge=1)] = 1
    mode: Literal["synthetic", "reviewed"] = "synthetic"
    change_reason: Annotated[str, Field(min_length=1)]
    evidence: tuple[Evidence, ...] = ()
    assets: tuple[Asset, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    ports: tuple[Port, ...] = ()
    connections: tuple[Connection, ...] = ()
    functional_links: tuple[FunctionalLink, ...] = ()
    operating_cases: tuple[OperatingCase, ...] = ()
    contexts: tuple[Context, ...] = ()
    data: tuple[Datum, ...] = ()
    events: tuple[BasicEvent, ...] = ()
    gates: tuple[Gate, ...] = ()
    trees: tuple[FaultTree, ...] = ()
    rates: tuple[Rate, ...] = ()
    ipls: tuple[IPL, ...] = ()
    independence: tuple[Independence, ...] = ()
    transitions: tuple[Transition, ...] = ()
    criteria: tuple[Criterion, ...] = ()
    sifs: tuple[SIF, ...] = ()
    scenarios: tuple[Scenario, ...] = ()

    def index(self, field: str):
        return {x.id: x for x in getattr(self, field)}

    @model_validator(mode="after")
    def integrity(self):
        from .validation import validate_snapshot
        validate_snapshot(self)
        return self
