#!/usr/bin/env python3
"""Independent verifier for Batchgate audit bundles.

Takes an exported audit zip and re-checks, without importing any application
code:
  1. every manifest row (path / bytes / sha256) matches the actual member;
  2. attempts.csv rows agree with the per-attempt evidence JSON files;
  3. the report success/failed/skipped totals agree with the evidence rows.

Usage: python -I verify_audit_bundle.py <bundle.zip>
Exits 0 on success, 1 on any mismatch.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import zipfile


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main(zip_path: str) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            fail("manifest.json missing from bundle")
        manifest = json.loads(zf.read("manifest.json"))

        entries = {e["path"]: e for e in manifest["files"]}
        if set(entries) != names - {"manifest.json"}:
            fail("manifest file list does not equal zip member list")

        for path, entry in entries.items():
            data = zf.read(path)
            if len(data) != entry["bytes"]:
                fail(f"byte size mismatch: {path}")
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                fail(f"sha256 mismatch: {path}")
        print(f"PASS: {len(entries)} payload files match manifest hashes")

        rows = list(
            csv.DictReader(io.StringIO(zf.read("attempts.csv").decode("utf-8")))
        )
        counts = manifest["counts"]
        if len(rows) != counts["attempt_count"]:
            fail("attempts.csv row count disagrees with manifest")
        if len(rows) != counts["evidence_count"]:
            fail("evidence_count disagrees with attempts.csv")

        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            evidence_path = row["evidence_file"]
            if evidence_path not in entries:
                fail(f"evidence file not in manifest: {evidence_path}")
            evidence = json.loads(zf.read(evidence_path))
            if evidence["status"] != row["status"]:
                fail(f"status disagreement in {evidence_path}")
            if evidence["reason"] != row["reason"]:
                fail(f"reason disagreement in {evidence_path}")
        if status_counts != counts["status_counts"]:
            fail("status counts disagree with manifest")
        print(f"PASS: {len(rows)} attempt rows trace to matching evidence files")

        totals = manifest["report_totals"]
        if status_counts.get("success", 0) != totals["succeeded"]:
            fail("report succeeded total disagrees with evidence")
        if status_counts.get("failed", 0) != totals["failed"]:
            fail("report failed total disagrees with evidence")
        if status_counts.get("skipped_dep", 0) != totals["skipped"]:
            fail("report skipped total disagrees with evidence")
        report = json.loads(zf.read("report_summary.json"))
        if report["report"]["attempt_count"] != totals["attempt_count"]:
            fail("report_summary attempt_count disagrees with manifest totals")
        print("PASS: report success/failed/skipped totals trace to evidence")

        snapshot = json.loads(zf.read("dependencies/jobs_snapshot.json"))
        if snapshot["job_count"] != len(snapshot["jobs"]):
            fail("dependency snapshot job_count inconsistent")
        print(f"PASS: dependency snapshot covers {snapshot['job_count']} jobs")

    print(f"OK: {zip_path} verified ({len(entries)} files, {len(rows)} attempts)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -I verify_audit_bundle.py <bundle.zip>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
