"""Model diagnostics cannot serve as independent investment validation."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.discovery_models import DiscoveryResult
from app.models import ResearchArtifact, ResearchJob
from scripts import evolve_rulers
from tests.test_evolve_rulers import _seed_evolution_db


def test_llm_labels_never_suffice_for_activation():
    from scripts.evolve_rulers import activation_eligibility

    ok, reason = activation_eligibility(proposal_kind='research_hypothesis',
                                       evidence={'fail_count':20,'gray_count':50})
    assert not ok
    assert reason == 'independent_validation_required'


@pytest.mark.parametrize("fail_count", [2, 20])
def test_model_support_never_confirms_or_writes_rulers(tmp_path: Path, fail_count: int):
    db = tmp_path / "research.db"
    _seed_evolution_db(db)
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        row = session.scalars(select(DiscoveryResult).where(
            DiscoveryResult.code == "300627.SZ")).one()
        job = session.scalars(select(ResearchJob).where(
            ResearchJob.code == "300627.SZ")).one()
        artifact = session.scalars(select(ResearchArtifact).where(
            ResearchArtifact.research_job_id == job.id)).one()
        for index in range(fail_count - 1):
            code = f"TEST{index:03}.SZ"
            session.add(DiscoveryResult(run_id=row.run_id, track=row.track,
                stage=row.stage, attempt=1, code=code, verdict="pass",
                metrics_json=row.metrics_json, created_at=row.created_at))
            clone = ResearchJob(user_id=job.user_id, name=f"fixture {index}",
                code=code, status="done", created_at=job.created_at,
                updated_at=job.updated_at)
            session.add(clone)
            session.flush()
            session.add(ResearchArtifact(research_job_id=clone.id,
                structured_json=artifact.structured_json, markdown="fixture"))
        session.commit()
    engine.dispose()
    before_bytes = db.read_bytes()
    with sqlite3.connect(db) as connection:
        before_rows = connection.execute("SELECT * FROM ruler_versions ORDER BY id").fetchall()
    report = evolve_rulers.evolve(db, tracks=("quality_discount",), min_support=1, apply=True)
    assert report["status"] == "blocked"
    assert report["blocked_reasons"] == ["independent_validation_required"]
    assert report["applied"] is False
    track = report["tracks"]["quality_discount"]
    assert track["changed"] is False
    assert track["counts"]["research_disagreement_total"] == fail_count
    assert track["threshold_changes"]["peg_release_max"]["status"] == "research_hypothesis"
    assert "false_positives" not in track
    assert db.read_bytes() == before_bytes
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM ruler_versions ORDER BY id").fetchall() == before_rows


def test_gray_is_a_research_label_not_investment_success(tmp_path: Path):
    db = tmp_path / "gray.db"
    _seed_evolution_db(db)
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        job = session.scalars(select(ResearchJob).where(ResearchJob.code == "300750.SZ")).one()
        artifact = session.scalars(select(ResearchArtifact).where(
            ResearchArtifact.research_job_id == job.id)).one()
        payload = json.loads(artifact.structured_json)
        payload["verdict"] = "gray"
        artifact.structured_json = json.dumps(payload)
        session.commit()
    engine.dispose()
    report = evolve_rulers.evolve(db, tracks=("quality_discount",))
    track = report["tracks"]["quality_discount"]
    assert track["research_agreement"][0]["research_label"] == "gray"
    assert "protected_samples" not in track
    assert track["validation_report"]["eligible"] is False


def test_apply_refusal_does_not_create_missing_database(tmp_path: Path):
    db = tmp_path / "absent.db"
    report = evolve_rulers.evolve(db, apply=True)
    assert report["blocked_reasons"] == ["independent_validation_required", "database_missing"]
    assert not db.exists()


_BINDINGS = ("proposal_sha256", "ruler_hash", "input_hash")
_VARIANTS = ("full_selected", "no_operating_selected", "all_passed")


def _write_document(root, name, payload):
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    (root / name).write_bytes(data)
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}


def _validation_fixture(root, kind):
    artifacts = {}
    for name in ("proposal", "ruler", "input"):
        artifacts[name] = _write_document(root, name + ".json", {"fixture": name})
    bindings = dict(zip(_BINDINGS, (artifacts[n]["sha256"] for n in ("proposal", "ruler", "input"))))
    report = {"schema_version": 1, "proposal_kind": kind,
        "source": "deterministic_reproduction" if kind == "data_error_fix" else "independent_outcomes",
        "author": "fixture producer", "generated_at": "2026-10-21T12:00:00+00:00",
        "release_reason": "Correct retained input parsing" if kind == "data_error_fix" else "Review frozen experiment evidence",
        "bindings": bindings, "artifacts": artifacts}
    if kind == "data_error_fix":
        artifacts["source"] = _write_document(root, "source.json",
            {"origin": "official_records", "records": [{"field": "100"}]})
        artifacts["acceptance"] = _write_document(root, "acceptance.json",
            {"origin": "deterministic_reproduction", "bindings": bindings,
             "source_sha256": artifacts["source"]["sha256"],
             "cases": [{"id": "parse amount", "expected": "100", "actual": "100"}]})
    else:
        benchmarks = {name: _write_document(root, name + ".json",
            {"origin": "official_records", "id": index, "records": [{"total_return": "0.1"}]})
            for name, index in (("primary", "CSI300_TOTAL_RETURN"), ("auxiliary", "CSI500_TOTAL_RETURN"))}
        protocol = {"origin": "preregistered_protocol", "bindings": bindings,
            "registered_at": "2025-01-01T08:00:00+00:00",
            "published_run_at": "2025-01-01T08:00:00+00:00",
            "training_end": "2024-12-31T08:00:00+00:00",
            "observation_start": "2025-01-02T08:00:00+00:00",
            "observation_end": "2026-01-02T08:00:00+00:00",
            "maturity_at": "2026-01-02T08:00:00+00:00", "horizon_months": 12,
            "universe": "frozen universe", "entry_policy": "next complete trading day",
            "cost_policy": "15bp per side", "exit_policy": "independent horizon ledger",
            "variants": list(_VARIANTS), "benchmarks": benchmarks}
        artifacts["protocol"] = _write_document(root, "protocol.json", protocol)
        datasets = {name: _write_document(root, name + ".json",
            {"origin": "official_records", "records": [{"fixture": "retained"}]})
            for name in ("security_universe", "public_financials", "trading_calendar",
                         "raw_prices", "corporate_actions", "trading_status")}
        artifacts["coverage"] = _write_document(root, "coverage.json",
            {"origin": "coverage_manifest", "datasets": datasets,
             "window_start": protocol["observation_start"], "window_end": protocol["observation_end"]})
        artifacts["comparison"] = _write_document(root, "comparison.json",
            {"origin": "independent_outcomes", "protocol_sha256": artifacts["protocol"]["sha256"],
             "coverage_sha256": artifacts["coverage"]["sha256"],
             "benchmark_hashes": {name: ref["sha256"] for name, ref in benchmarks.items()},
             "window_start": protocol["observation_start"], "window_end": protocol["observation_end"],
             "maturity_at": protocol["maturity_at"], "evaluated_at": "2026-10-20T08:00:00+00:00",
             "variants": {name: {"gross_return": "-0.1", "net_return": "-0.13",
                "relative_return": "-0.2", "max_drawdown": "0.3",
                "entry_coverage": "0.9", "exit_completion": "1"} for name in _VARIANTS}})
    return report, bindings


def _seal_report(root, report, bindings):
    ref = _write_document(root, "report.json", report)
    review = _write_document(root, "review.json", {
        "origin": "human_review", "reviewer": "independent fixture reviewer",
        "reviewed_at": "2026-10-21T13:00:00+00:00", "report_sha256": ref["sha256"],
        "decision": "eligible_for_release_review",
        "independence_basis": "Reviewer is independent of proposal and report authorship",
        "provenance_assessment": "Reviewed original records, completeness, and non-model provenance"})
    return {"report_path": str(root / ref["path"]), "report_sha256": ref["sha256"],
        "review_path": str(root / review["path"]), "review_sha256": review["sha256"], **bindings}


@pytest.mark.parametrize("kind", ["data_error_fix", "validated_experiment"])
def test_retained_reviewed_reports_are_eligible_without_publishing(tmp_path, kind):
    report, bindings = _validation_fixture(tmp_path, kind)
    evidence = _seal_report(tmp_path, report, bindings)
    assert evolve_rulers.activation_eligibility(proposal_kind=kind, evidence=evidence) == (True, None)


@pytest.mark.parametrize("kind", ["data_error_fix", "validated_experiment"])
def test_boolean_validation_claims_are_insufficient(kind):
    assert evolve_rulers.activation_eligibility(proposal_kind=kind,
        evidence={"verified": True, "passed": True}) == (False, "validation_report_required")


@pytest.mark.parametrize("mutation,reason", [
    ("report_hash", "validation_report_hash_mismatch"),
    ("artifact_hash", "retained_artifact_hash_mismatch"),
    ("binding", "validation_binding_mismatch"),
    ("review", "independent_review_required"),
    ("model_relabel", "independent_validation_required"),
    ("acceptance", "deterministic_acceptance_required"),
])
def test_deterministic_reports_fail_closed(tmp_path, mutation, reason):
    report, bindings = _validation_fixture(tmp_path, "data_error_fix")
    if mutation == "model_relabel":
        source = {"origin": "model_labels", "records": [{"verdict": "fail"}]}
        report["artifacts"]["source"] = _write_document(tmp_path, "source.json", source)
    if mutation == "acceptance":
        ref = report["artifacts"]["acceptance"]
        payload = json.loads((tmp_path / ref["path"]).read_text())
        payload["cases"][0]["actual"] = "200"
        report["artifacts"]["acceptance"] = _write_document(tmp_path, ref["path"], payload)
    evidence = _seal_report(tmp_path, report, bindings)
    if mutation == "report_hash":
        (tmp_path / "report.json").write_text("{}")
    if mutation == "artifact_hash":
        (tmp_path / "source.json").write_text("{}")
    if mutation == "binding":
        evidence["ruler_hash"] = "0" * 64
    if mutation == "review":
        evidence.pop("review_path")
    assert evolve_rulers.activation_eligibility(proposal_kind="data_error_fix", evidence=evidence) == (False, reason)


@pytest.mark.parametrize("mutation,reason", [
    ("registration", "preregistration_required"),
    ("same_instant_observation", "preregistration_required"),
    ("training_overlap", "time_out_of_sample_required"),
    ("immature", "mature_observation_required"),
    ("benchmark", "frozen_benchmark_required"),
    ("coverage", "data_coverage_required"),
    ("comparison", "loss_return_comparison_required"),
])
def test_experiment_report_requires_bound_independent_mature_evidence(tmp_path, mutation, reason):
    report, bindings = _validation_fixture(tmp_path, "validated_experiment")
    artifact = "protocol" if mutation in {"registration", "same_instant_observation", "training_overlap"} else "comparison"
    if mutation == "coverage":
        artifact = "coverage"
    ref = report["artifacts"][artifact]
    payload = json.loads((tmp_path / ref["path"]).read_text())
    if mutation == "registration":
        payload["registered_at"] = "2025-01-02T08:00:00+00:00"
    if mutation == "same_instant_observation":
        payload["observation_start"] = payload["published_run_at"]
    if mutation == "training_overlap":
        payload["training_end"] = payload["observation_start"]
    if mutation == "immature":
        payload["evaluated_at"] = "2025-12-01T08:00:00+00:00"
    if mutation == "benchmark":
        payload["benchmark_hashes"]["primary"] = "0" * 64
    if mutation == "coverage":
        payload["datasets"].pop("corporate_actions")
    if mutation == "comparison":
        payload["variants"]["all_passed"].pop("max_drawdown")
    report["artifacts"][artifact] = _write_document(tmp_path, ref["path"], payload)
    evidence = _seal_report(tmp_path, report, bindings)
    assert evolve_rulers.activation_eligibility(proposal_kind="validated_experiment", evidence=evidence) == (False, reason)


@pytest.mark.parametrize("kind", ["data_error_fix", "validated_experiment"])
def test_relabelled_model_verdicts_are_still_ineligible(tmp_path, kind):
    report, bindings = _validation_fixture(tmp_path, kind)
    report["artifacts"]["input"] = _write_document(tmp_path, "input.json",
        {"origin": "official_records", "records": [{"verdict": "gray"}]})
    bindings["input_hash"] = report["artifacts"]["input"]["sha256"]
    bound_name = "acceptance" if kind == "data_error_fix" else "protocol"
    reference = report["artifacts"][bound_name]
    bound = json.loads((tmp_path / reference["path"]).read_text())
    bound["bindings"] = bindings
    report["artifacts"][bound_name] = _write_document(tmp_path, reference["path"], bound)
    if kind == "validated_experiment":
        reference = report["artifacts"]["comparison"]
        comparison = json.loads((tmp_path / reference["path"]).read_text())
        comparison["protocol_sha256"] = report["artifacts"]["protocol"]["sha256"]
        report["artifacts"]["comparison"] = _write_document(tmp_path, reference["path"], comparison)
    evidence = _seal_report(tmp_path, report, bindings)
    assert evolve_rulers.activation_eligibility(proposal_kind=kind, evidence=evidence) == (
        False, "independent_validation_required")


def test_apply_refuses_before_initializing_an_empty_sqlite_database(tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('retained')")
    before = db.read_bytes()
    output = tmp_path / "report.json"
    assert evolve_rulers.main(["--db", str(db), "--apply", "--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["blocked_reasons"][0] == "independent_validation_required"
    assert db.read_bytes() == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("sentinel",)]



def test_experiment_cannot_shorten_registered_maturity_to_one_day(tmp_path):
    report, bindings = _validation_fixture(tmp_path, "validated_experiment")
    reference = report["artifacts"]["protocol"]
    protocol = json.loads((tmp_path / reference["path"]).read_text())
    protocol["maturity_at"] = "2025-01-03T08:00:00+00:00"
    report["artifacts"]["protocol"] = _write_document(tmp_path, reference["path"], protocol)
    reference = report["artifacts"]["comparison"]
    comparison = json.loads((tmp_path / reference["path"]).read_text())
    comparison["protocol_sha256"] = report["artifacts"]["protocol"]["sha256"]
    comparison["maturity_at"] = protocol["maturity_at"]
    report["artifacts"]["comparison"] = _write_document(tmp_path, reference["path"], comparison)
    evidence = _seal_report(tmp_path, report, bindings)
    assert evolve_rulers.activation_eligibility(proposal_kind="validated_experiment", evidence=evidence) == (
        False, "mature_observation_required")
