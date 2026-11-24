import json
import sqlite3

import pytest

from pha_core.application import run_study
from pha_core.domain import StudySnapshot
from pha_core.storage import Store
from pha_core.tables import convert, export_table, preview_table
from conftest import parse


@pytest.mark.parametrize("mutation,match", [
    (lambda r: r["assets"][0].update(tag=r["assets"][1]["tag"]), "duplicate active"),
    (lambda r: r["events"][0].update(asset_id="missing"), "unknown assets"),
    (lambda r: r["gates"][0].update(inputs=[r["trees"][0]["id"]]), "cycle"),
    (lambda r: r["data"][0].update(value=None), "missing_reason"),
    (lambda r: r["data"][0].update(unit="year^-1"), "unit mismatch"),
    (lambda r: r["data"][0].update(value=2.), "probability"),
    (lambda r: r["data"][0].update(value=float("nan")), "finite"),
    (lambda r: r["data"][0].update(value=True), "number"),
    (lambda r: r.update(unknown_field="rejected"), "Extra inputs"),
    (lambda r: r.update(mode="reviewed"), "reviewed"),
    (lambda r: r["assets"][0].update(review="REVIEWED"), "evidence"),
])
def test_input_guards(raw, mutation, match):
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        parse(raw)


def test_frozen_roundtrip(snapshot):
    assert StudySnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert isinstance(snapshot.assets, tuple)
    with pytest.raises(ValueError):
        snapshot.assets[0].tag = "wrong"


def test_process_topology_is_separate_and_checks_phase(raw):
    raw["ports"] += [{"id": "INLET", "asset_id": "AST-PCV01", "purpose": "normal_process", "direction": "in", "phase": "liquid", "property_basis": raw["ports"][0]["property_basis"]}]
    raw["connections"] = [{"id": "PIPE-CONNECTION", "source_port_id": "PORT-TK-ACCIDENT", "target_port_id": "INLET"}]
    with pytest.raises(ValueError, match="phase mismatch"):
        parse(raw)


def test_baseline_and_entity_revisions_are_immutable(tmp_path, raw):
    store = Store(tmp_path / "project.sqlite")
    s = parse(raw)
    sha = store.save(s)
    assert store.save(s) == sha
    assert store.load(sha) == s
    raw["revision"] = 2
    raw["assets"][0]["tag"] = "TK-NEW-TAG"
    with pytest.raises(ValueError, match="entity revision"):
        store.save(parse(raw))
    raw["assets"][0]["revision"] = 2
    newer = store.save(parse(raw))
    assert newer != sha and store.load(sha).assets[0].tag == "TK-01"
    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE baselines SET revision=99 WHERE digest=?", (sha,))


def test_run_dependencies_and_staleness(tmp_path, raw):
    store = Store(tmp_path / "p.sqlite")
    sha = store.save(parse(raw))
    result = run_study(store, sha)
    assert result["status"] == "SUCCEEDED" and not result["stale"]
    assert len(result["dependency_run_ids"]) == 4
    assert result["result"]["lopa_proposed"]
    with pytest.raises(ValueError, match="immutable"):
        store.finish_run(result["id"], result={})
    raw.update(revision=2, change_reason="criteria revision")
    raw["criteria"][0].update(value_per_year=1e-7, revision=2)
    new_sha = store.save(parse(raw))
    assert store.run(result["id"])["stale"]
    with pytest.raises(ValueError, match="same baseline"):
        store.start_run(new_sha, "bad", (result["id"],))
    assert store.run(result["id"])["result"] == result["result"]


def test_evidence_hash_is_checked(tmp_path, raw):
    store = Store(tmp_path / "p.sqlite")
    source = tmp_path / "engineering-note.txt"
    source.write_text("reviewed source", encoding="utf-8")
    evidence = store.ingest_evidence(source)
    raw["evidence"][0]["sha256"] = evidence["sha256"]
    store.save(parse(raw))
    from pathlib import Path
    Path(evidence["managed_path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load(parse(raw).digest())
    with pytest.raises(ValueError, match="hash mismatch"):
        store.save(parse(raw))
    raw.update(revision=2, change_reason="hash test")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.save(parse(raw))


@pytest.mark.parametrize("suffix", ["json", "csv", "xlsx"])
def test_table_roundtrip_preview_is_not_commit(tmp_path, snapshot, suffix):
    path = tmp_path / f"assets.{suffix}"
    export_table(snapshot, "assets", path)
    candidate, preview = preview_table(snapshot, "assets", path, new_revision=2, reason="roundtrip")
    assert candidate.assets == snapshot.assets
    assert preview["changed"] == [] and not preview["committed"]
    assert candidate.revision == 2


def test_ambiguous_pressure_and_fraction_units_rejected():
    assert convert(2., "bar_g", ambient_pa_abs=101325.)["value"] == 301325.
    assert convert(20., "degC")["value"] == 293.15
    assert convert(5., "percent")["value"] == .05
    for value, unit in ((2., "bar"), (2., "bar_g"), (101., "percent"), (-300., "degC")):
        with pytest.raises(ValueError):
            convert(value, unit)


def test_reviewed_input_mode_requires_managed_sources_and_stays_separate_from_certification(tmp_path, raw):
    from pha_core.application import analyze
    store = Store(tmp_path / "review.sqlite")
    source = tmp_path / "review-note.txt"
    source.write_text("synthetic unit-test review record; no field claim", encoding="utf-8")
    managed = store.ingest_evidence(source)
    raw.update(mode="reviewed")
    for key, rows in raw.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            row["review"] = "REVIEWED"
            if key != "evidence":
                row["evidence_ids"] = ["EVID-DESIGN"]
            if key == "data":
                row["source_kind"] = "observed"
    raw["evidence"][0].update(source_kind="observed", sha256=managed["sha256"])
    for ipl in raw["ipls"]:
        ipl.update(effectiveness="REVIEWED", timeliness="REVIEWED", proof_testing="REVIEWED")
    raw["independence"][0]["judgment"] = "REVIEWED"
    raw["transitions"][0]["physical_model_ref"] = "unit-test-evidence-reference"
    for sif in raw["sifs"]:
        sif["cross_block_independence"] = "REVIEWED"
        for block in sif["blocks"]:
            block.update(review="REVIEWED", evidence_ids=["EVID-DESIGN"])
    snapshot = parse(raw)
    assert store.load(store.save(snapshot)) == snapshot
    result = analyze(snapshot)
    assert result["evidence_status"] == "REVIEWED_INPUTS"
    assert result["sil"]["SIF-TK01"]["overall_verification"] == "REVIEW_REQUIRED"


def test_code_change_requires_restart_and_marks_old_run_stale(tmp_path, snapshot, monkeypatch):
    import pha_core.storage as storage
    store = Store(tmp_path / "p.sqlite")
    result = run_study(store, store.save(snapshot))
    monkeypatch.setattr(storage, "source_code_digest", lambda: "0" * 64)
    assert store.run(result["id"])["stale"]
    with pytest.raises(ValueError, match="restart"):
        store.start_run(snapshot.digest(), "should-not-execute")
