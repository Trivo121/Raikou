"""Scene-scoped chat for optical runs: routing, refusals, wording, citations and message modes."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from app.schemas.evidence import EvidenceCitation
from app.services.optical.record import build_optical_record
from app.services.processing import optical_chat as chat

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
SCENE_ID = "6079c678-e128-49be-91cf-d53b21ecbec8"
RECORD_ARTIFACT = "0b0e0c4a-5d0c-4c55-9a1d-2b3c4d5e6f70"
CLASS_NAMES = ["Built-up", "Agriculture", "Forest & natural vegetation", "Barren / wasteland", "Water bodies & wetlands"]
SHARES = {"Built-up": 37.45, "Agriculture": 2.07, "Forest & natural vegetation": 54.77, "Barren / wasteland": 2.58, "Water bodies & wetlands": 3.13}


def _summary(*, spectral: bool = True, embeddings: bool = True) -> dict:
    stats = [
        {
            "class_id": str(i), "class_name": name, "area_sqkm": str(round(SHARES[name] * 1.0322, 4)),
            "area_hectares": str(round(SHARES[name] * 103.22, 2)), "share_of_mapped_%": str(SHARES[name]),
            "mean_score_%": "88.0", "worldcover_2021_area_sqkm": "10.0", "same_class_in_worldcover_2021_%": "80.0",
        }
        for i, name in enumerate(CLASS_NAMES, start=1)
    ]
    matrix = [[0.0] * 5 for _ in range(5)]
    for i in range(5):
        matrix[i][i] = 18.0
    matrix[1][0] = 1.2  # WorldCover Agriculture, mapped Built-up
    matrix[2][1] = 0.4
    return {
        "grid": {"epsg": 32644, "width": 1016, "height": 1016, "res": 10.0},
        "corners_lonlat": [[83.19, 17.77], [83.29, 17.77], [83.29, 17.67], [83.19, 17.67]],
        "timesteps": [{"index": 0, "sensing_start": "2026-04-13T04:52:41+00:00", "valid_fraction": 1.0, "season": "summer"}],
        "timesteps_used": [0],
        "run": {"valid_fraction": 0.9998},
        "stats": stats,
        "agreement": {"classes": CLASS_NAMES, "matrix_km2": matrix},
        "spectral_indices": [
            {"date": "2026-04-13", "used_by_model": True, "class_id": 1, "class_name": "Built-up", "clear_pixels": 380000,
             "clear_area_sqkm": 38.0, "ndvi_median": 0.18, "mndwi_median": -0.12, "ndbi_median": 0.04},
        ] if spectral else [],
        "embeddings": {"dims": 64, "energy_captured": 0.93, "token_m": 40.0} if embeddings else {},
        "windows": {"count": 225, "window_m": 640.0} if embeddings else {},
        "model": {
            "name": "Raikou OlmoEarth India LULC (5 classes)", "version": "0.1.0", "base_model": "allenai/OlmoEarth-v1_2-Base",
            "licence": "OlmoEarth Artifact License (see LICENSE_OlmoEarth_Artifact.txt)",
            "single_date": {"agreement_overall": 0.8592, "mean_iou": 0.6385, "macro_f1": 0.7384,
                            "per_class_iou": {"Built-up": 0.78, "Water bodies & wetlands": 0.124}},
            "seasonal_stack": {"agreement_overall": 0.872, "mean_iou": 0.6555, "macro_f1": 0.752, "per_class_iou": {}},
            "reference": "label_raw = WorldCover 2021 remapped, unfiltered (agreement, not ground truth)",
            "temperature": 2.1018,
            "calibration_meaning": "calibrated probability that a pixel agrees with the WorldCover-2021-derived class on held-out Indian tiles",
        },
        "artifacts": {
            "lulc": "11111111-1111-4111-8111-111111111111", "true_color": "22222222-2222-4222-8222-222222222222",
            "worldcover2021": "33333333-3333-4333-8333-333333333333", "score": "44444444-4444-4444-8444-444444444444",
            "similarity": "55555555-5555-4555-8555-555555555555", "bundle_zip": "66666666-6666-4666-8666-666666666666",
            "land_cover_2026_04_13_lulc_10m.tif": "77777777-7777-4777-8777-777777777777",
        },
    }


def _record(**kwargs) -> dict:
    return build_optical_record(
        scene={"id": SCENE_ID, "name": "Land cover 2026-04-13 (17.720, 83.240)"},
        acquisition={"processing_units": 17.0636},
        summary=_summary(**kwargs),
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("Is there a military base here?", "refuse_security"),
        ("Can this watch the India-Pakistan border?", "refuse_security"),
        ("Is this area near the border?", "refuse_security"),
        ("Where should we mine for sand?", "refuse_security"),
        ("How many buildings are in this area?", "refuse_objects"),
        ("Count the trees please", "refuse_objects"),
        ("Who owns this land?", "refuse_ownership"),
        ("Can I open this in QGIS?", "download"),
        ("What is the NDVI of the farmland?", "spectral"),
        ("Find areas similar to this lake", "similarity"),
        ("How much forest was lost since 2021?", "change"),
        ("Explain the agreement matrix", "change"),
        ("What happened between 2021 and 2026?", "change"),
        ("How accurate is this map?", "trust"),
        ("What does the score mean?", "trust"),
        ("Describe this area", "summary"),
        ("When was the image taken?", "imagery"),
        ("How was this map made?", "method"),
        ("What do the classes mean?", "method"),
        ("How much water is there?", "area"),
        ("Which class is dominant?", "area"),
        ("Where is the water?", "area"),
        ("How many hectares of forest are there?", "area"),
        ("Write me a poem about this place", "general"),
    ],
)
def test_questions_are_routed_to_what_can_answer_them(question, intent):
    assert chat.classify_optical_query(question) == intent


def test_message_modes_name_their_path():
    assert chat.message_mode("refuse_objects") == "optical_refusal"
    assert chat.message_mode("general") == "optical_grounded"
    assert chat.message_mode("area") == "optical_record_area"


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------


def _all_answers(record: dict) -> dict[str, str]:
    intents = [*chat.ANSWER_INTENTS, "refuse_security", "refuse_objects", "refuse_ownership"]
    return {intent: chat.compose_optical_answer(intent, record, "how much water is there") for intent in intents}


def test_every_record_answer_says_agreement_and_never_claims_accuracy_or_change():
    for intent, answer in _all_answers(_record()).items():
        assert answer, intent
        cleaned = answer.lower().replace("not field-verified accuracy", "")
        assert "accuracy" not in cleaned, (intent, answer)
        assert "change detected" not in cleaned and "has changed" not in cleaned, (intent, answer)


def test_area_answer_quotes_the_record_and_leads_with_the_asked_class():
    answer = chat.compose_optical_answer("area", _record(), "How much water is there?")

    assert answer.splitlines()[0] == "Water bodies & wetlands covers 3.1% of the mapped area (3.23 km²)."
    assert "Forest & natural vegetation: 54.8%" in answer
    assert "The largest class is Forest & natural vegetation." in answer


def test_change_answer_is_an_agreement_matrix_with_the_heldout_caveat():
    answer = chat.compose_optical_answer("change", _record())

    assert "not change detection" in answer
    assert "WorldCover 2021 says Agriculture, this map says Built-up: 1.20 km²" in answer
    assert "disagrees with WorldCover on about 14% of pixels" in answer  # 1 - 0.8592
    assert "mixes real change with errors in both maps" in answer


def test_trust_answer_gives_benchmark_scores_and_an_audit_path():
    answer = chat.compose_optical_answer("trust", _record())

    assert "agrees with WorldCover-2021-derived labels on 85.9% of pixels (single-date benchmark" in answer
    assert "Water bodies & wetlands 0.12" in answer
    assert "QGIS" in answer and "land_cover_2026_04_13_lulc_10m.tif" in answer
    assert "floods seasonally" in answer  # the record's water caveat travels with the scores


def test_spectral_and_similarity_answers_admit_when_a_run_predates_them():
    old = _record(spectral=False, embeddings=False)

    assert "Reprocess it" in chat.compose_optical_answer("spectral", old)
    assert "Reprocess it" in chat.compose_optical_answer("similarity", old)
    assert "NDVI 0.18" in chat.compose_optical_answer("spectral", _record())
    assert "640 m windows" in chat.compose_optical_answer("similarity", _record())


def test_imagery_method_download_and_summary_answers():
    record = _record()

    imagery = chat.compose_optical_answer("imagery", record)
    assert "2026-04-13, summer: 100% clear, used" in imagery and "17.06" in imagery
    method = chat.compose_optical_answer("method", record)
    assert "OlmoEarth Artifact License" in method and "Indian" in method and "WorldCover-2021" in method
    assert "land_cover_2026_04_13_lulc_10m.tif" in chat.compose_optical_answer("download", record)
    assert chat.compose_optical_answer("summary", record).startswith("Sentinel-2 land cover (single date, 2026-04-13)")


def test_refusals_say_what_can_be_answered_instead():
    for intent in ("refuse_security", "refuse_objects", "refuse_ownership"):
        answer = chat.compose_optical_answer(intent, _record())
        assert "I can" in answer


def test_only_the_fallback_reaches_the_language_model():
    assert chat.compose_optical_answer("general", _record()) is None


def test_fallback_context_is_bounded_and_carries_the_numbers():
    text = chat.optical_context_text(_record(), limit=4000)

    assert len(text) <= 4000
    assert "Forest & natural vegetation: 54.8%" in text
    assert "do not add numbers" in text
    assert len(chat.optical_context_text(_record(), limit=300)) == 300


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("intent", [*chat.ANSWER_INTENTS, "general"])
def test_citations_validate_and_name_the_layer_the_answer_leans_on(intent):
    citations = chat.optical_citations(_record(), scene_id=SCENE_ID, record_artifact_id=RECORD_ARTIFACT, intent=intent)

    parsed = [EvidenceCitation.model_validate(c) for c in citations]
    types = [(c.source_type, c.provenance.get("layer")) for c in parsed]
    assert types[0] == ("metadata", None)
    assert ("lulc_map", "lulc") in types and ("optical_overview", "true_color") in types
    assert (("lulc_map", "worldcover2021") in types) is (intent == "change")
    assert (("lulc_map", "score") in types) is (intent == "trust")
    assert (("similarity_map", "similarity") in types) is (intent == "similarity")
    assert all(c.provenance.get("is_detector_evidence") is False for c in parsed[1:])


def test_a_refusal_cites_only_the_record():
    citations = chat.optical_citations(_record(), scene_id=SCENE_ID, record_artifact_id=RECORD_ARTIFACT, intent="refuse_objects")

    assert [c["source_type"] for c in citations] == ["metadata"]


# --------------------------------------------------------------------------
# The database must accept every mode, and keep every older one
# --------------------------------------------------------------------------


def _modes(migration: str) -> set[str]:
    sql = (MIGRATIONS / migration).read_text(encoding="utf-8")
    body = sql.split("messages_mode_check", 2)[-1]
    return set(re.findall(r"'([a-z_]+)'", body))


def test_every_optical_mode_is_allowed_and_no_older_mode_is_lost():
    newest = _modes("20261016002000_m9_optical_chat_message_modes.sql")

    assert set(chat.MESSAGE_MODES) <= newest
    assert _modes("20260723000000_m5_scene_record_message_modes.sql") <= newest
    assert _modes("20260722002000_m5_grounded_chat_message_modes.sql") <= newest


def test_the_deploy_never_replays_a_narrower_mode_set():
    script = (MIGRATIONS.parents[1] / "deployment" / "deploy-from-github.sh").read_text(encoding="utf-8")
    listed = re.findall(r'^\s*"(\d{14}_[a-z0-9_]+\.sql)",', script, re.MULTILINE)

    defining = [name for name in listed if "messages_mode_check" in (MIGRATIONS / name).read_text(encoding="utf-8")]
    assert defining == ["20261016002000_m9_optical_chat_message_modes.sql"]
