from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def next_version(conn: sqlite3.Connection, orchestration_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM audit_exports WHERE orchestration_id=?",
        (orchestration_id,),
    ).fetchone()
    return int(row["v"]) + 1


def create_export(
    conn: sqlite3.Connection,
    *,
    orchestration_id: int,
    version: int,
    package_path: str,
    package_bytes: int,
    package_sha256: str,
    manifest_sha256: str,
    manifest: dict[str, Any],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_exports(
            orchestration_id, version, package_path, package_bytes,
            package_sha256, manifest_sha256, manifest_json, created_at
        )
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            orchestration_id,
            version,
            package_path,
            package_bytes,
            package_sha256,
            manifest_sha256,
            json.dumps(manifest, ensure_ascii=False, indent=2),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["manifest"] = json.loads(d.pop("manifest_json"))
    return d


_SELECT = """
        SELECT id, orchestration_id, version, package_path, package_bytes,
               package_sha256, manifest_sha256, manifest_json, created_at
        FROM audit_exports
"""


def get_export(conn: sqlite3.Connection, export_id: int) -> dict[str, Any] | None:
    r = conn.execute(
        _SELECT + " WHERE id=?",
        (export_id,),
    ).fetchone()
    return _row(r)


def get_export_by_version(
    conn: sqlite3.Connection, orchestration_id: int, version: int
) -> dict[str, Any] | None:
    r = conn.execute(
        _SELECT + " WHERE orchestration_id=? AND version=?",
        (orchestration_id, version),
    ).fetchone()
    return _row(r)


def list_exports(
    conn: sqlite3.Connection, orchestration_id: int | None = None
) -> list[dict[str, Any]]:
    if orchestration_id is None:
        rows = conn.execute(_SELECT + " ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            _SELECT + " WHERE orchestration_id=? ORDER BY version DESC",
            (orchestration_id,),
        ).fetchall()
    return [d for r in rows if (d := _row(r)) is not None]
