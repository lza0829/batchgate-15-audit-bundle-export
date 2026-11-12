from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.db import connect
from app.repositories import audit_exports as export_repo
from app.services import audit_service

router = APIRouter(tags=["audit-exports"])


@router.get("/exports")
def list_all_exports():
    return audit_service.list_exports(None)


@router.get("/orchestrations/{orch_id}/exports")
def list_orchestration_exports(orch_id: int):
    conn = connect()
    try:
        return export_repo.list_exports(conn, orch_id)
    finally:
        conn.close()


@router.post("/orchestrations/{orch_id}/exports")
def create_orchestration_export(orch_id: int):
    try:
        return audit_service.build_audit_bundle(orch_id)
    except audit_service.OrchestrationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except audit_service.OrchestrationRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except audit_service.BundleConsistencyError as exc:  # pragma: no cover - defensive
        raise HTTPException(500, f"audit bundle consistency check failed: {exc}") from exc


@router.get("/exports/{export_id}")
def get_export(export_id: int):
    record = audit_service.get_export(export_id)
    if not record:
        raise HTTPException(404, "export not found")
    return record


@router.get("/exports/{export_id}/manifest")
def get_export_manifest(export_id: int):
    record = audit_service.get_export(export_id)
    if not record:
        raise HTTPException(404, "export not found")
    return record["manifest"]


@router.get("/exports/{export_id}/download")
def download_export(export_id: int):
    record = audit_service.get_export(export_id)
    if not record:
        raise HTTPException(404, "export not found")
    package = Path(record["package_path"])
    if not package.is_file():
        raise HTTPException(410, "package file no longer available on disk")
    return FileResponse(
        package,
        media_type="application/zip",
        filename=record["manifest"]["package"]["filename"],
    )
