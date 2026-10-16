"""The optical stage sequence against the database enums, the worker classes and the SAR map.

A stage or execution class the database does not know fails inside
``m3_enqueue_task``, after the previous stage has already done its work, so
the mismatch would show up as a job stuck mid-run instead of failing here.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from app.schemas.jobs import ProcessingJobStage
from app.services.jobs.publisher import EXECUTION_CLASSES
from app.workers import optical_stages, stages
from app.workers.repository import UserFacingTaskError

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
OPTICAL_MIGRATION = MIGRATIONS / "20261016001000_m9_optical_acquisitions.sql"

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


def _chain() -> list[str]:
    chain = ["fetch_source"]
    while optical_stages.NEXT_STAGE[chain[-1]] is not None:
        chain.append(optical_stages.NEXT_STAGE[chain[-1]][0])
        assert len(chain) <= len(optical_stages.NEXT_STAGE), "the optical stage map has a cycle"
    return chain


def _bare_pipeline() -> stages.M3Pipeline:
    """An M3Pipeline without __init__: no object storage or database needed."""
    return object.__new__(stages.M3Pipeline)


# --------------------------------------------------------------------------
# The map itself
# --------------------------------------------------------------------------


def test_every_optical_stage_exists_in_the_database_and_the_api_enum():
    names = set(optical_stages.NEXT_STAGE) | {n[0] for n in optical_stages.NEXT_STAGE.values() if n}

    assert names <= _labels_for("processing_job_stage")
    assert names <= {member.value for member in ProcessingJobStage}


def test_every_execution_class_a_worker_can_run_exists_in_the_database():
    database = _labels_for("processing_execution_class")
    used = {n[1] for n in optical_stages.NEXT_STAGE.values() if n}

    assert {"cpu", "gpu", "optical"} <= database
    assert EXECUTION_CLASSES <= database
    assert used <= EXECUTION_CLASSES


def test_the_chain_runs_every_handler_once_and_ends_at_finalize():
    chain = _chain()
    handlers = _bare_pipeline().optical_handlers()

    assert chain[-1] == "finalize"
    assert len(chain) == len(set(chain))
    assert set(chain) == set(handlers)
    assert set(optical_stages.NEXT_STAGE) == set(handlers) | {"cleanup"}


def test_progress_only_moves_forward():
    values = [optical_stages.PROGRESS[stage] for stage in _chain()]

    assert values == sorted(values) and values[-1] == 100
    assert set(optical_stages.PROGRESS) == set(optical_stages.NEXT_STAGE)


def test_only_the_model_stage_is_queued_for_the_optical_worker():
    queued = [n[0] for n in optical_stages.NEXT_STAGE.values() if n and n[1] == "optical"]

    assert queued == ["embed_patches"]


def test_the_database_queues_the_first_stage_on_cpu():
    sql = OPTICAL_MIGRATION.read_text(encoding="utf-8")

    assert re.search(
        r"m3_enqueue_task\(\s*job_id,\s*'fetch_source'::public\.processing_job_stage,\s*'cpu'::public\.processing_execution_class",
        sql,
    )


def test_the_sar_pipeline_is_unchanged():
    assert stages._NEXT_STAGE["tile_patches"] == ("embed_patches", "gpu")
    assert stages._NEXT_STAGE["build_overview"] == ("tile_patches", "cpu")
    assert all(n is None or n[1] in {"cpu", "gpu"} for n in stages._NEXT_STAGE.values())


# --------------------------------------------------------------------------
# Dispatch by scene
# --------------------------------------------------------------------------


class _Scenes:
    def __init__(self, metadata):
        self.metadata = metadata

    def scene(self, task):
        return {"id": task["scene_id"], "metadata": self.metadata}


TASK = {"scene_id": "scene-1", "owner_id": "owner-1", "project_id": "project-1"}


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [({"pipeline": "optical"}, "optical"), ({"pipeline": "sar"}, "sar"), ({}, "sar"), (None, "sar"), ("optical", "sar")],
)
def test_the_pipeline_is_read_from_the_scene(metadata, expected):
    pipeline = _bare_pipeline()
    pipeline.repository = _Scenes(metadata)

    assert pipeline._pipeline(TASK) == expected


def test_a_stage_runs_the_handler_and_next_stage_of_its_scene_pipeline(monkeypatch):
    monkeypatch.setattr(stages, "invalidate_project_evidence_cache_sync", lambda **_: None)
    pipeline = _bare_pipeline()
    pipeline._optical_lulc = lambda task: {"ran": "optical"}
    pipeline._embed_patches = lambda task: {"ran": "sar"}

    pipeline.repository = _Scenes({"pipeline": "optical"})
    optical = pipeline.run({**TASK, "stage": "embed_patches"})
    pipeline.repository = _Scenes({})
    sar = pipeline.run({**TASK, "stage": "embed_patches"})

    assert (optical.result, optical.next_stage, optical.progress) == ({"ran": "optical"}, ("build_evidence", "cpu"), 85)
    assert (sar.result, sar.next_stage) == ({"ran": "sar"}, ("index_vectors", "cpu"))


def test_an_optical_scene_refuses_a_sar_only_stage(monkeypatch):
    monkeypatch.setattr(stages, "invalidate_project_evidence_cache_sync", lambda **_: None)
    pipeline = _bare_pipeline()
    pipeline.repository = _Scenes({"pipeline": "optical"})

    with pytest.raises(UserFacingTaskError) as info:
        pipeline.run({**TASK, "stage": "tile_patches"})

    assert info.value.code == "UNSUPPORTED_STAGE"
