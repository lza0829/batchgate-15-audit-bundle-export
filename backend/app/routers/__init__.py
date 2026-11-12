from fastapi import APIRouter

from app.routers import (
    attempts,
    audit_exports,
    dashboard,
    jobs,
    orchestrations,
    settings,
    watermarks,
)

api = APIRouter(prefix="/api")
api.include_router(dashboard.router)
api.include_router(jobs.router)
api.include_router(orchestrations.router)
api.include_router(attempts.router)
api.include_router(audit_exports.router)
api.include_router(watermarks.router)
api.include_router(settings.router)
