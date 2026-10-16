"""The optical branch of POST /conversations/{id}/stream, with its I/O replaced by fakes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.api.deps import CurrentUser
from app.api.routes import evidence
from app.schemas.evidence import ChatStreamRequest
from app.services.optical.record import build_optical_record

OWNER = "9d3d658e-47e0-429c-8463-c256c9e49f9b"
PROJECT = UUID("ca58d2fe-234e-4813-9d77-160bb0d190c6")
SCENE = "6079c678-e128-49be-91cf-d53b21ecbec8"
CONVERSATION = UUID("1f3a8c2e-5b7d-4e9f-8a1b-2c3d4e5f6a7b")
RECORD_ARTIFACT = "0b0e0c4a-5d0c-4c55-9a1d-2b3c4d5e6f70"
USER = CurrentUser(id=OWNER, role="authenticated")
OPTICAL_SCENE = {"id": SCENE, "name": "Land cover 2026-04-13", "status": "ready", "metadata": {"pipeline": "optical"}}


def _record() -> dict:
    names = ["Built-up", "Agriculture", "Forest & natural vegetation", "Barren / wasteland", "Water bodies & wetlands"]
    shares = [37.45, 2.07, 54.77, 2.58, 3.13]
    return build_optical_record(
        scene={"id": SCENE, "name": "Land cover 2026-04-13"},
        acquisition={"processing_units": 17.06},
        summary={
            "grid": {"epsg": 32644, "width": 1016, "height": 1016, "res": 10.0},
            "corners_lonlat": [[83.19, 17.77], [83.29, 17.77], [83.29, 17.67], [83.19, 17.67]],
            "timesteps": [{"index": 0, "sensing_start": "2026-04-13T04:52:41+00:00", "valid_fraction": 1.0}],
            "timesteps_used": [0],
            "run": {"valid_fraction": 0.9998},
            "stats": [
                {"class_id": str(i + 1), "class_name": n, "area_sqkm": str(s), "area_hectares": str(s * 100),
                 "share_of_mapped_%": str(s), "mean_score_%": "88.0"}
                for i, (n, s) in enumerate(zip(names, shares))
            ],
            "model": {"name": "Raikou OlmoEarth India LULC (5 classes)", "version": "0.1.0",
                      "single_date": {"agreement_overall": 0.8592, "mean_iou": 0.6385, "macro_f1": 0.7384}},
            "artifacts": {"lulc": "11111111-1111-4111-8111-111111111111", "true_color": "22222222-2222-4222-8222-222222222222"},
        },
    )


class _Chunk:
    def __init__(self, text: str) -> None:
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=text))]


@pytest.fixture
def io(monkeypatch):
    state = SimpleNamespace(record=_record(), inserts=[], llm_text=["Rivers ", "run here."], llm_error=None, llm_calls=[])

    async def fake_execute(operation, detail):
        return SimpleNamespace(data=[{"id": "ev-1", "scene_id": SCENE, "metadata": {"record_artifact_id": RECORD_ARTIFACT}}])

    async def fake_records(*, evidence_by_scene, project_id, current_user):
        return {SCENE: state.record} if state.record is not None else {}

    async def fake_insert(**kwargs):
        state.inserts.append(kwargs)
        return {"id": f"m{len(state.inserts)}"}

    async def fake_history(conversation_id, current_user):
        return []

    async def fake_stream(*, system, history, text_part, images):
        state.llm_calls.append({"system": system, "text": text_part["text"], "images": images})
        if state.llm_error:
            raise state.llm_error

        async def chunks():
            for piece in state.llm_text:
                yield _Chunk(piece)

        return chunks()

    monkeypatch.setattr(evidence, "_execute", fake_execute)
    monkeypatch.setattr(evidence, "_load_scene_records", fake_records)
    monkeypatch.setattr(evidence, "_insert_message", fake_insert)
    monkeypatch.setattr(evidence, "_conversation_history", fake_history)
    monkeypatch.setattr(evidence, "_open_grounded_stream", fake_stream)
    return state


def _ask(query: str) -> list[dict]:
    async def go():
        response = await evidence._stream_optical_chat(
            conversation_id=CONVERSATION, project_id=PROJECT, scene=OPTICAL_SCENE,
            request=ChatStreamRequest(query=query), current_user=USER,
        )
        return [json.loads(chunk) async for chunk in response.body_iterator]

    return asyncio.run(go())


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def test_a_record_question_is_answered_without_the_language_model(io):
    events = _ask("How much water is there?")

    assert _types(events) == ["conversation", "status", "citations", "text", "done"]
    assert events[1]["data"]["state"] == "optical_record"
    assert events[3]["data"].startswith("Water bodies & wetlands covers 3.1%")
    assert [i["mode"] for i in io.inserts] == ["grounded", "optical_record_area"]
    assert [c["source_type"] for c in events[2]["data"]] == ["metadata", "lulc_map", "optical_overview"]
    assert io.llm_calls == []


def test_an_object_count_is_refused_with_only_the_record_cited(io):
    events = _ask("How many buildings are there?")

    assert io.inserts[-1]["mode"] == "optical_refusal"
    assert "does not detect, count or locate" in events[3]["data"]
    assert [c["source_type"] for c in events[2]["data"]] == ["metadata"]


def test_the_fallback_streams_from_the_record_and_says_so(io):
    events = _ask("Write me a poem about this place")

    texts = [e["data"] for e in events if e["type"] == "text"]
    assert texts[:2] == ["Rivers ", "run here."]
    assert texts[-1].startswith("\n\n(Written by the language model from this run's record.")
    assert io.inserts[-1]["mode"] == "optical_grounded" and io.inserts[-1]["status_value"] == "complete"
    (call,) = io.llm_calls
    assert call["images"] == [] and "do not add numbers" in call["text"] and "Never invent numbers" in call["system"]


def test_a_failed_fallback_is_saved_as_failed(io):
    io.llm_error = RuntimeError("vLLM down")

    events = _ask("Write me a poem about this place")

    assert "error" in _types(events) and events[-1]["data"]["status"] == "failed"
    assert io.inserts[-1]["mode"] == "optical_grounded_failed"


def test_a_run_without_a_record_says_there_is_nothing_to_answer_from(io):
    io.record = None

    events = _ask("How much water is there?")

    assert events[1]["data"]["state"] == "empty"
    assert io.inserts[-1]["mode"] == "optical_insufficient"
    assert events[2]["data"] == []


def test_a_sar_record_is_never_read_as_an_optical_one(io):
    io.record = {"record_type": "sar_scene_analysis", "scene": {}}

    _ask("How much water is there?")

    assert io.inserts[-1]["mode"] == "optical_insufficient"


def test_the_stream_route_hands_optical_scenes_to_the_optical_branch(monkeypatch):
    calls = []

    async def scope(*, conversation_id, requested_scene_id, current_user):
        return {}, PROJECT, UUID(SCENE)

    async def scenes(*, project_id, selected_scene_id, filters, current_user):
        return [OPTICAL_SCENE]

    async def optical_branch(**kwargs):
        calls.append(kwargs)
        return "optical-response"

    async def no_sar(*args, **kwargs):
        raise AssertionError("SAR retrieval must not run for an optical scene")

    monkeypatch.setattr(evidence, "_authorized_conversation_scope", scope)
    monkeypatch.setattr(evidence, "_owned_search_scenes", scenes)
    monkeypatch.setattr(evidence, "_stream_optical_chat", optical_branch)
    monkeypatch.setattr(evidence, "_load_rag_context", no_sar)
    monkeypatch.setattr(evidence, "_search_authorized", no_sar)

    result = asyncio.run(evidence.stream_grounded_chat(CONVERSATION, ChatStreamRequest(query="How much forest?"), USER))

    assert result == "optical-response"
    assert calls[0]["scene"]["id"] == SCENE


@pytest.mark.parametrize(("metadata", "expected"), [({"pipeline": "optical"}, True), ({"pipeline": "sar"}, False), ({}, False), (None, False)])
def test_optical_scenes_are_told_apart_by_their_pipeline(metadata, expected):
    assert evidence._is_optical_scene({"id": SCENE, "metadata": metadata}) is expected
