from app.services.processing.chat_policy import (
    classify_scene_query,
    detector_answer,
    environment_answer,
)


def test_environment_questions_do_not_route_to_patch_retrieval() -> None:
    assert classify_scene_query("Is there any vegetation in the image?", ["bridge"]) == "environment"


def test_detector_count_uses_detector_route() -> None:
    assert classify_scene_query("How many bridges are there?", ["bridge"]) == "detector_count"


def test_location_question_can_use_patch_as_supporting_evidence() -> None:
    assert classify_scene_query("Where is the bridge?", ["bridge"]) == "detector_location"


def test_detector_answer_never_turns_missing_detection_into_absence() -> None:
    answer = detector_answer(
        query="Are there any buildings?",
        facts=[{"label": "bridge", "confidence": 0.92}],
        detector={"status": "completed"},
    )

    assert answer is not None
    assert "No building detections are recorded" in answer
    assert "not proof" in answer


def test_detector_question_without_output_is_explicitly_unanswerable() -> None:
    answer = detector_answer(query="Are there any ships?", facts=[], detector={})

    assert answer is not None
    assert "cannot answer" in answer
    assert "not sufficient" in answer


def test_detector_count_is_grounded_in_facts() -> None:
    answer = detector_answer(
        query="How many bridges are there?",
        facts=[
            {"label": "bridge", "confidence": 0.91},
            {"label": "bridge", "confidence": 0.76},
        ],
        detector={"status": "completed"},
    )

    assert answer is not None
    assert "2 bridge candidates" in answer
    assert "76%–91%" in answer


def test_detector_location_keeps_detector_as_the_authority() -> None:
    answer = detector_answer(
        query="Where is the bridge?",
        facts=[{"label": "bridge", "confidence": 0.91}],
        detector={"status": "completed"},
        spatial_groups=[{"label": "bridge", "region": "lower-left", "count": 1}],
    )

    assert answer is not None
    assert "lower-left" in answer
    assert "detector-backed evidence" in answer


def test_environment_answer_exposes_limitations() -> None:
    answer = environment_answer(
        "Is there any vegetation?",
        {"label": "likely_water_dominant"},
    )

    assert "cannot confirm vegetation" in answer
    assert "water-dominant" in answer
    assert "not land-cover classification" in answer
