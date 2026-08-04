"""Every database job-stage label must exist in the strict Pydantic enum.

``ProcessingJobStage`` rejects unknown values, so a stage that exists in
PostgreSQL but not in ``schemas/jobs.py`` turns ``GET /jobs/{id}`` and
``/jobs/{id}/events`` into a 500 for the entire time a job sits in it. The
workspace polls both every 1.5-12s, so the failure is total and its cause is
several layers away from the symptom.
"""

from __future__ import annotations

from pathlib import Path
import re

from app.schemas.jobs import ProcessingJobStage, ProcessingJobStatus


MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

_CREATE_ENUM = re.compile(
    r"create\s+type\s+public\.(\w+)\s+as\s+enum\s*\((?P<body>[^)]*)\)", re.IGNORECASE | re.DOTALL
)
_ADD_VALUE = re.compile(
    r"alter\s+type\s+public\.(\w+)\s+add\s+value\s+(?:if\s+not\s+exists\s+)?'(?P<label>[^']+)'",
    re.IGNORECASE,
)


def _labels_for(enum_name: str) -> set[str]:
    labels: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for match in _CREATE_ENUM.finditer(sql):
            if match.group(1).lower() == enum_name:
                labels.update(re.findall(r"'([^']+)'", match.group("body")))
        for match in _ADD_VALUE.finditer(sql):
            if match.group(1).lower() == enum_name:
                labels.add(match.group("label"))
    return labels


def test_the_migration_directory_is_discoverable():
    assert MIGRATIONS.is_dir(), f"migrations not found at {MIGRATIONS}"
    assert list(MIGRATIONS.glob("*.sql"))


def test_every_database_job_stage_has_a_pydantic_member():
    database_labels = _labels_for("processing_job_stage")
    model_labels = {member.value for member in ProcessingJobStage}

    assert database_labels, "no processing_job_stage labels were parsed"
    assert "fetch_source" in database_labels
    assert not database_labels - model_labels, (
        "these job stages exist in the database but not in ProcessingJobStage: "
        f"{sorted(database_labels - model_labels)}"
    )


def test_every_database_job_status_has_a_pydantic_member():
    database_labels = _labels_for("processing_job_status")
    model_labels = {member.value for member in ProcessingJobStatus}

    # 'running' and 'succeeded' are M2-era labels retained for historic rows;
    # the M3 migration rewrote the data but deliberately left the labels.
    assert not database_labels - model_labels - {"running", "succeeded"}, (
        "these job statuses exist in the database but not in ProcessingJobStatus: "
        f"{sorted(database_labels - model_labels)}"
    )
