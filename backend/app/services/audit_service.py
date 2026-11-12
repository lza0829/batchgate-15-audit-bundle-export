"""Build downloadable audit bundles from in-DB records.

Every payload inside a bundle is derived from the same SQLite tables the API
serves (orchestrations / job_attempts / jobs); nothing is collected by scanning
directories and nothing is synthesized at request time.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import EXPORTS_DIR
from app.db import connect
from app.repositories import audit_exports as export_repo
from app.repositories import jobs as jobs_repo
from app.repositories import orchestrations as orch_repo

MANIFEST_NAME = "manifest.json"
REPORT_NAME = "report_summary.json"
ATTEMPTS_CSV_NAME = "attempts.csv"
SNAPSHOT_NAME = "dependencies/jobs_snapshot.json"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class OrchestrationNotFound(LookupError):
    pass


class OrchestrationRunning(RuntimeError):
    pass


class BundleConsistencyError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned or "job"


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _attempt_reason(attempt: dict[str, Any]) -> str:
    detail = attempt.get("detail") or {}
    if attempt["status"] == "skipped_dep":
        return str(detail.get("reason", ""))
    if attempt["status"] == "failed":
        return str(detail.get("error", ""))
    return "ok"


def evidence_path(attempt: dict[str, Any]) -> str:
    return f"attempts/attempt_{attempt['id']:04d}_{_safe_name(attempt['job_name'])}.json"


def _evidence_payload(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": attempt["id"],
        "orchestration_id": attempt["orchestration_id"],
        "job_id": attempt["job_id"],
        "job_name": attempt["job_name"],
        "attempt_no": attempt["attempt_no"],
        "status": attempt["status"],
        "reason": _attempt_reason(attempt),
        "detail": attempt["detail"],
        "created_at": attempt["created_at"],
        "evidence_source": "table:job_attempts",
    }


def _attempts_csv_bytes(attempts: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "attempt_id",
            "orchestration_id",
            "job_id",
            "job_name",
            "attempt_no",
            "status",
            "reason",
            "detail_json",
            "created_at",
            "evidence_file",
        ]
    )
    for a in attempts:
        writer.writerow(
            [
                a["id"],
                a["orchestration_id"],
                a["job_id"],
                a["job_name"],
                a["attempt_no"],
                a["status"],
                _attempt_reason(a),
                json.dumps(a["detail"], ensure_ascii=False, sort_keys=True),
                a["created_at"],
                evidence_path(a),
            ]
        )
    return buf.getvalue().encode("utf-8")


def _status_counts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in attempts:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    return dict(sorted(counts.items()))


def _collect_files(
    orch: dict[str, Any],
    attempts: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    version: int,
    created_at: str,
) -> dict[str, Any]:
    """Build every payload file (name -> bytes) from DB-derived data."""
    files: dict[str, bytes] = {}

    report_summary = {
        "orchestration_id": orch["id"],
        "status": orch["status"],
        "started_at": orch["started_at"],
        "finished_at": orch["finished_at"],
        "report": orch["report"],
        "evidence_source": "table:orchestrations",
    }
    files[REPORT_NAME] = _json_bytes(report_summary)

    files[ATTEMPTS_CSV_NAME] = _attempts_csv_bytes(attempts)

    for a in attempts:
        files[evidence_path(a)] = _json_bytes(_evidence_payload(a))

    files[SNAPSHOT_NAME] = _json_bytes(
        {
            "orchestration_id": orch["id"],
            "snapshot_at": created_at,
            "job_count": len(jobs),
            "evidence_source": "table:jobs",
            "jobs": jobs,
        }
    )
    for job in jobs:
        path = f"dependencies/job_{job['id']:04d}_{_safe_name(job['name'])}.json"
        files[path] = _json_bytes(job)

    manifest_files = [
        {"path": path, "bytes": len(data), "sha256": _sha256_bytes(data)}
        for path, data in sorted(files.items())
    ]
    report = orch["report"]
    manifest = {
        "manifest_version": 1,
        "orchestration_id": orch["id"],
        "export_version": version,
        "created_at": created_at,
        "hash_alg": "sha256",
        "package": {
            "filename": f"audit_orch_{orch['id']}_v{version}.zip",
            "note": "package sha256/bytes are recorded in the audit_exports table",
        },
        "files": manifest_files,
        "counts": {
            "file_count": len(manifest_files),
            "attempt_count": len(attempts),
            "evidence_count": len(attempts),
            "status_counts": _status_counts(attempts),
        },
        "report_totals": {
            "succeeded": report.get("succeeded"),
            "failed": report.get("failed"),
            "skipped": report.get("skipped"),
            "attempt_count": report.get("attempt_count"),
        },
    }
    return {"files": files, "manifest": manifest}


def _verify_bundle(bundle: dict[str, Any], orch: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    """Cross-check manifest entries against payloads and report totals."""
    files: dict[str, bytes] = bundle["files"]
    manifest = bundle["manifest"]

    # 1. every manifest row must match an actual payload file and its hash
    if len(manifest["files"]) != len(files):
        raise BundleConsistencyError("manifest file count does not match payload count")
    for entry in manifest["files"]:
        data = files.get(entry["path"])
        if data is None:
            raise BundleConsistencyError(f"manifest lists missing file: {entry['path']}")
        if len(data) != entry["bytes"] or _sha256_bytes(data) != entry["sha256"]:
            raise BundleConsistencyError(f"hash mismatch for {entry['path']}")

    # 2. one evidence file per attempt row, reachable from attempts.csv
    evidence_paths = {evidence_path(a) for a in attempts}
    listed = {e["path"] for e in manifest["files"] if e["path"].startswith("attempts/")}
    if listed != evidence_paths:
        raise BundleConsistencyError("evidence files do not match attempt rows")

    # 3. report success/fail/skip totals must equal the evidence status counts
    counts = manifest["counts"]["status_counts"]
    totals = manifest["report_totals"]
    if counts.get("success", 0) != totals["succeeded"]:
        raise BundleConsistencyError("success evidence count disagrees with report")
    if counts.get("failed", 0) != totals["failed"]:
        raise BundleConsistencyError("failed evidence count disagrees with report")
    if counts.get("skipped_dep", 0) != totals["skipped"]:
        raise BundleConsistencyError("skipped evidence count disagrees with report")
    if totals["attempt_count"] != len(attempts):
        raise BundleConsistencyError("report attempt_count disagrees with attempt rows")

    # 4. dependency snapshot covers every defined job
    snapshot_entries = [e for e in manifest["files"] if e["path"].startswith("dependencies/")]
    if len(snapshot_entries) != orch["report"].get("job_total", 0) + 1:
        raise BundleConsistencyError("dependency snapshot does not cover every job")


def _write_zip(zip_path: Path, bundle: dict[str, Any]) -> None:
    manifest_bytes = _json_bytes(bundle["manifest"])
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, data in sorted(bundle["files"].items()):
            info = zipfile.ZipInfo(path, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=_ZIP_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, manifest_bytes)


def _verify_zip(zip_path: Path, bundle: dict[str, Any]) -> None:
    """Reopen the finished zip and verify every member against the manifest."""
    manifest_bytes = _json_bytes(bundle["manifest"])
    expected = {entry["path"]: entry for entry in bundle["manifest"]["files"]}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if names != set(expected) | {MANIFEST_NAME}:
            raise BundleConsistencyError("zip members do not match manifest")
        for path, entry in expected.items():
            data = zf.read(path)
            if len(data) != entry["bytes"] or _sha256_bytes(data) != entry["sha256"]:
                raise BundleConsistencyError(f"zip content hash mismatch: {path}")
        if _sha256_bytes(zf.read(MANIFEST_NAME)) != _sha256_bytes(manifest_bytes):
            raise BundleConsistencyError("zip manifest corrupted")


def build_audit_bundle(orchestration_id: int) -> dict[str, Any]:
    """Create an export record + zip bundle for one orchestration."""
    conn = connect()
    try:
        orch = orch_repo.get_orchestration(conn, orchestration_id)
        if not orch:
            raise OrchestrationNotFound(f"orchestration {orchestration_id} not found")
        if orch["status"] == "running":
            raise OrchestrationRunning(
                f"orchestration {orchestration_id} is still running"
            )

        attempts = orch_repo.list_attempts(conn, orchestration_id)
        jobs = jobs_repo.list_jobs(conn)
        version = export_repo.next_version(conn, orchestration_id)
        created_at = _now()

        bundle = _collect_files(orch, attempts, jobs, version, created_at)
        _verify_bundle(bundle, orch, attempts)

        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = EXPORTS_DIR / bundle["manifest"]["package"]["filename"]
        tmp_path = zip_path.with_name(f".{zip_path.name}.tmp")
        try:
            _write_zip(tmp_path, bundle)
            _verify_zip(tmp_path, bundle)
            tmp_path.replace(zip_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        package_bytes = zip_path.stat().st_size
        package_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        manifest_sha = _sha256_bytes(_json_bytes(bundle["manifest"]))

        export_id = export_repo.create_export(
            conn,
            orchestration_id=orchestration_id,
            version=version,
            package_path=str(zip_path),
            package_bytes=package_bytes,
            package_sha256=package_sha,
            manifest_sha256=manifest_sha,
            manifest=bundle["manifest"],
        )
        conn.commit()
        record = export_repo.get_export(conn, export_id)
        assert record is not None
        return record
    finally:
        conn.close()


def list_exports(orchestration_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return export_repo.list_exports(conn, orchestration_id)
    finally:
        conn.close()


def get_export(export_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        return export_repo.get_export(conn, export_id)
    finally:
        conn.close()
