from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # Import app modules only after DATA_DIR points at the temp directory.
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.seed as seed
    importlib.reload(seed)
    import app.repositories.audit_exports as export_repo
    importlib.reload(export_repo)
    import app.repositories.orchestrations as orch_repo
    importlib.reload(orch_repo)
    import app.services.audit_service as audit_service
    importlib.reload(audit_service)
    import app.routers.audit_exports as audit_router
    importlib.reload(audit_router)
    import app.main as main_module
    importlib.reload(main_module)

    seed.init_db()
    return {
        "data_dir": data_dir,
        "db": db,
        "seed": seed,
        "orch_repo": orch_repo,
        "export_repo": export_repo,
        "audit_service": audit_service,
        "main": main_module,
    }


def _run_one_orchestration(isolated_data) -> dict:
    from app.services.orchestrate_service import run_orchestration

    return run_orchestration()


def _manifest_paths(manifest: dict) -> set[str]:
    return {entry["path"] for entry in manifest["files"]}


def test_bundle_matches_manifest_and_report(isolated_data):
    audit_service = isolated_data["audit_service"]
    orch = _run_one_orchestration(isolated_data)
    record = audit_service.build_audit_bundle(orch["id"])

    # export record is persisted and queryable
    assert record["version"] == 1
    assert record["package_bytes"] > 0
    package = Path(record["package_path"])
    assert package.is_file()
    assert hashlib.sha256(package.read_bytes()).hexdigest() == record["package_sha256"]

    manifest = record["manifest"]
    # manifest rows must equal the actual zip members (minus manifest.json)
    with zipfile.ZipFile(package) as zf:
        names = set(zf.namelist())
        assert names == _manifest_paths(manifest) | {"manifest.json"}
        for entry in manifest["files"]:
            data = zf.read(entry["path"])
            assert len(data) == entry["bytes"]
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert hashlib.sha256(
            zf.read("manifest.json")
        ).hexdigest() == record["manifest_sha256"]

    # required artifacts are present
    paths = _manifest_paths(manifest)
    assert "report_summary.json" in paths
    assert "attempts.csv" in paths
    assert "dependencies/jobs_snapshot.json" in paths

    # manifest counts line up: 3 attempts + report + csv + snapshot + 3 job files
    assert manifest["counts"]["file_count"] == len(paths)
    assert manifest["counts"]["attempt_count"] == 3
    assert manifest["counts"]["evidence_count"] == 3
    assert manifest["counts"]["status_counts"] == {"success": 3}

    # report totals are traceable to evidence rows
    with zipfile.ZipFile(package) as zf:
        report = json.loads(zf.read("report_summary.json"))
        assert report["report"]["succeeded"] == 3
        rows = list(csv.DictReader(io.StringIO(zf.read("attempts.csv").decode("utf-8"))))
        assert len(rows) == 3
        assert all(r["status"] == "success" and r["reason"] == "ok" for r in rows)
        # every csv row points at an evidence file that exists and carries the same status
        for r in rows:
            assert r["evidence_file"] in names
            evidence = json.loads(zf.read(r["evidence_file"]))
            assert evidence["status"] == r["status"]
            assert evidence["job_name"] == r["job_name"]
        snapshot = json.loads(zf.read("dependencies/jobs_snapshot.json"))
        assert snapshot["job_count"] == 3
        job_names = {j["name"] for j in snapshot["jobs"]}
        assert job_names == {"ingest_orders", "dim_customer", "fact_order_daily"}
        # fact job dependency snapshot captures its upstream gates
        fact = next(j for j in snapshot["jobs"] if j["name"] == "fact_order_daily")
        assert fact["depends_on"] == ["ingest_orders", "dim_customer"]


def test_repeat_download_is_byte_identical_and_reexport_versions(isolated_data):
    audit_service = isolated_data["audit_service"]
    orch = _run_one_orchestration(isolated_data)

    first = audit_service.build_audit_bundle(orch["id"])
    second = audit_service.build_audit_bundle(orch["id"])
    assert second["version"] == first["version"] + 1

    # same version keeps one immutable record/file that can be downloaded repeatedly
    again = audit_service.get_export(first["id"])
    assert again["package_sha256"] == first["package_sha256"]
    p1 = Path(first["package_path"])
    downloaded = p1.read_bytes()
    assert hashlib.sha256(downloaded).hexdigest() == first["package_sha256"]

    records = audit_service.list_exports(orch["id"])
    assert [r["version"] for r in records] == [2, 1]


def test_skipped_and_failed_statuses_have_evidence_rows(isolated_data):
    orch_repo = isolated_data["orch_repo"]
    audit_service = isolated_data["audit_service"]
    db = isolated_data["db"]

    # hand-craft one orchestration containing skipped_dep, failed and success evidence
    conn = db.connect()
    try:
        orch_id = orch_repo.create_orchestration(conn)
        jobs = conn.execute("SELECT id, name FROM jobs ORDER BY id").fetchall()
        by_name = {j["name"]: j for j in jobs}
        orch_repo.insert_attempt(
            conn, orchestration_id=orch_id, job_id=by_name["ingest_orders"]["id"],
            attempt_no=1, status="success", detail={"ok": True},
        )
        orch_repo.insert_attempt(
            conn, orchestration_id=orch_id, job_id=by_name["dim_customer"]["id"],
            attempt_no=1, status="failed", detail={"error": "boom"},
        )
        orch_repo.insert_attempt(
            conn, orchestration_id=orch_id, job_id=by_name["fact_order_daily"]["id"],
            attempt_no=1, status="skipped_dep",
            detail={"reason": "upstream not success: dim_customer"},
        )
        report = {
            "orchestration_id": orch_id,
            "job_total": 3,
            "succeeded": 1,
            "failed": 1,
            "skipped": 1,
            "attempt_count": 3,
            "watermarked_jobs": 1,
            "layer_count": 2,
        }
        orch_repo.finish_orchestration(conn, orch_id, "partial", report)
        conn.commit()
    finally:
        conn.close()

    record = audit_service.build_audit_bundle(orch_id)
    assert record["manifest"]["counts"]["status_counts"] == {
        "success": 1,
        "failed": 1,
        "skipped_dep": 1,
    }
    with zipfile.ZipFile(record["package_path"]) as zf:
        rows = list(csv.DictReader(io.StringIO(zf.read("attempts.csv").decode("utf-8"))))
        by_job = {r["job_name"]: r for r in rows}
        assert by_job["dim_customer"]["reason"] == "boom"
        assert by_job["fact_order_daily"]["reason"] == "upstream not success: dim_customer"
        for r in rows:
            evidence = json.loads(zf.read(r["evidence_file"]))
            assert evidence["reason"] == r["reason"]
            assert evidence["status"] == r["status"]


def test_missing_and_running_orchestration_rejected(isolated_data):
    audit_service = isolated_data["audit_service"]
    db = isolated_data["db"]
    orch_repo = isolated_data["orch_repo"]

    with pytest.raises(audit_service.OrchestrationNotFound):
        audit_service.build_audit_bundle(999)

    conn = db.connect()
    try:
        running_id = orch_repo.create_orchestration(conn)
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(audit_service.OrchestrationRunning):
        audit_service.build_audit_bundle(running_id)


def test_export_http_endpoints_roundtrip(isolated_data):
    from fastapi.testclient import TestClient

    main_module = isolated_data["main"]
    orch = _run_one_orchestration(isolated_data)

    with TestClient(main_module.app) as client:
        # empty export history first
        assert client.get(f"/api/orchestrations/{orch['id']}/exports").json() == []

        created = client.post(f"/api/orchestrations/{orch['id']}/exports")
        assert created.status_code == 200
        export_id = created.json()["id"]

        listing = client.get(f"/api/orchestrations/{orch['id']}/exports").json()
        assert len(listing) == 1 and listing[0]["id"] == export_id

        manifest_resp = client.get(f"/api/exports/{export_id}/manifest")
        assert manifest_resp.status_code == 200
        assert manifest_resp.json()["orchestration_id"] == orch["id"]

        download = client.get(f"/api/exports/{export_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert "attachment; filename=" in download.headers["content-disposition"]
        assert hashlib.sha256(download.content).hexdigest() == created.json()["package_sha256"]

        assert client.get("/api/exports/999").status_code == 404
        assert client.post("/api/orchestrations/999/exports").status_code == 404
