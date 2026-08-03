"""Deterministic policy for scene-scoped SAR chat.

This module intentionally does *not* classify SAR pixels.  It decides which
kind of evidence can answer a user's question, so a visually similar SARCLIP
patch is never promoted into an object fact.  The chat route owns I/O and model
calls; these helpers are pure to keep the safety policy easy to test.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal


ChatIntent = Literal[
    "detector_count",
    "detector_presence",
    "detector_location",
    "environment",
    "scene_description",
    "visual_evidence",
]

_COUNT_PATTERNS = (
    r"\bhow many\b",
    r"\bnumber of\b",
    r"\bcount(?:\s+the)?\b",
)
_DESCRIPTION_PATTERNS = (
    r"\bdescribe\b",
    r"\bexplain\b",
    r"\bwhat(?:'s| is) happening\b",
    r"\bwhat(?:'s| is) in (?:the )?(?:image|scene)\b",
    r"\boverview\b",
    r"\bsummar(?:y|ize)\b",
    r"\bgeneral context\b",
    r"\banything else\b",
)
_VISUAL_EVIDENCE_PATTERNS = (
    r"\bwhere\b",
    r"\blocate\b",
    r"\bfind\b",
    r"\bshow (?:me )?(?:a )?(?:patch|evidence)\b",
    r"\bsimilar\b",
)
_ENVIRONMENT_TERMS = {
    "vegetation", "vegetated", "forest", "woodland", "trees", "tree",
    "agriculture", "agricultural", "farmland", "farm", "crop", "crops",
    "water", "coast", "coastal", "shore", "shoreline", "land", "terrain",
    "urban", "city", "flood", "flooded", "ice", "snow", "bare ground",
}
_OBJECT_ALIASES: dict[str, set[str]] = {
    "ship": {"ship", "ships", "vessel", "vessels", "boat", "boats"},
    "aircraft": {"aircraft", "plane", "planes", "airplane", "airplanes"},
    "vehicle": {"vehicle", "vehicles", "car", "cars", "truck", "trucks"},
    "bridge": {"bridge", "bridges"},
    "port": {"port", "ports", "harbor", "harbours", "harbour"},
    "tank": {"tank", "tanks"},
    "building": {"building", "buildings"},
}


def normalize_label(value: str) -> str:
    """Return a small, predictable singular form suitable for label matching."""
    label = " ".join(value.casefold().split())
    if label.endswith("ies") and len(label) > 3:
        return label[:-3] + "y"
    if label.endswith("s") and not label.endswith("ss") and len(label) > 3:
        return label[:-1]
    return label


def detector_target(query: str, detector_labels: Iterable[str] = ()) -> str | None:
    """Find the object class the user asks about, if any.

    Known aliases cover the product's initial object taxonomy.  Detector labels
    are included as an extension point so a validated future detector class can
    be queried without teaching the language model a new rule.
    """
    normalized_query = " ".join(query.casefold().split())
    words = set(re.findall(r"[a-z0-9]+", normalized_query))
    for canonical, aliases in _OBJECT_ALIASES.items():
        if words.intersection(aliases):
            return canonical

    for raw_label in detector_labels:
        if not isinstance(raw_label, str) or not raw_label.strip():
            continue
        label = normalize_label(raw_label)
        label_words = set(re.findall(r"[a-z0-9]+", label))
        if label_words and label_words.issubset(words):
            return label
    return None


def classify_scene_query(query: str, detector_labels: Iterable[str] = ()) -> ChatIntent:
    """Route a scene question by the evidence needed to answer it.

    Detector facts beat vector retrieval for existence/count questions.  Land
    cover questions are deliberately treated as environmental context: without
    calibrated segmentation they should return an uncertainty-aware answer,
    never a retrieved-patch object claim.
    """
    normalized_query = " ".join(query.casefold().split())
    target = detector_target(normalized_query, detector_labels)
    if target:
        if any(re.search(pattern, normalized_query) for pattern in _COUNT_PATTERNS):
            return "detector_count"
        if any(re.search(pattern, normalized_query) for pattern in _VISUAL_EVIDENCE_PATTERNS):
            return "detector_location"
        return "detector_presence"

    if any(term in normalized_query for term in _ENVIRONMENT_TERMS):
        return "environment"
    if any(re.search(pattern, normalized_query) for pattern in _DESCRIPTION_PATTERNS):
        return "scene_description"
    if any(re.search(pattern, normalized_query) for pattern in _VISUAL_EVIDENCE_PATTERNS):
        return "visual_evidence"
    return "scene_description"


def detector_answer(
    *,
    query: str,
    facts: Iterable[dict[str, Any]],
    detector: dict[str, Any] | None,
    spatial_groups: Iterable[dict[str, Any]] = (),
) -> str | None:
    """Answer a count/presence question only from detector-backed facts.

    A negative result deliberately means *no recorded detection*, not proof of
    absence.  The response also makes a missing detector sidecar explicit.
    """
    fact_list = [item for item in facts if isinstance(item, dict)]
    target = detector_target(query, (str(item.get("label") or "") for item in fact_list))
    if target is None:
        return None

    target_aliases = _OBJECT_ALIASES.get(target, {target})
    matches = [
        item for item in fact_list
        if normalize_label(str(item.get("label") or "")) in {normalize_label(alias) for alias in target_aliases}
    ]
    status = str((detector or {}).get("status") or "unknown")
    label = target.replace("_", " ")
    is_count = any(re.search(pattern, query.casefold()) for pattern in _COUNT_PATTERNS)
    if matches:
        confidences = [
            float(item["confidence"])
            for item in matches
            if isinstance(item.get("confidence"), (int, float))
        ]
        confidence_text = ""
        if confidences:
            confidence_text = " Detector confidence range: {:.0%}–{:.0%}.".format(min(confidences), max(confidences))
        noun = label
        location_text = ""
        if any(re.search(pattern, query.casefold()) for pattern in _VISUAL_EVIDENCE_PATTERNS):
            regions = [
                str(item.get("region"))
                for item in spatial_groups
                if isinstance(item, dict)
                and normalize_label(str(item.get("label") or "")) in {normalize_label(alias) for alias in target_aliases}
                and isinstance(item.get("region"), str)
            ]
            if regions:
                location_text = " Coarse scene region(s): " + ", ".join(sorted(set(regions))) + "."
        if is_count:
            return (
                f"The current detector record contains {len(matches)} {noun} candidate"
                f"{'s' if len(matches) != 1 else ''}.{confidence_text}{location_text} "
                "This is a detector count, not proof that every such object in the scene was found."
            )
        return (
            f"Yes — the current detector record contains {len(matches)} {noun} candidate"
            f"{'s' if len(matches) != 1 else ''}.{confidence_text}{location_text} "
            "This is detector-backed evidence, not a claim from retrieval alone."
        )

    if status in {"awaiting_detector_output", "unknown"}:
        return (
            f"I cannot answer whether {label}s are present: this scene has no detector output yet. "
            "A retrieved SAR patch is not sufficient to establish an object class."
        )
    return (
        f"No {label} detections are recorded for this scene. This is not proof that no {label} is present: "
        "the current detector record may not cover that class or may miss objects."
    )


def environment_answer(
    query: str,
    land_water: dict[str, Any] | None,
    land_cover: dict[str, Any] | None = None,
    scattering: dict[str, Any] | None = None,
) -> str:
    """Answer an environment question from the best basis the scene actually has.

    Three bases in descending order of what they can say:

    ``land_cover`` names CORINE classes, but only inside the classifier's
    training domain.  A block marked ``out_of_domain`` is not a weaker signal to
    hedge; it is a European label space applied where no correct answer exists.

    ``scattering`` names the mechanism -- surface, volume, double-bounce -- from
    calibrated dual-pol intensity.  It cannot name a land use, but it is a
    measurement rather than a classification and it carries no training domain,
    so it answers where land cover cannot.  That makes it the right fallback for
    the common case of a scene outside Europe, which previously got a refusal.

    ``land_water`` is a brightness threshold and answers only when neither of
    the above is available.
    """
    normalized_query = query.casefold()
    requested = next((term for term in _ENVIRONMENT_TERMS if term in normalized_query), "that environment class")

    cover = land_cover if isinstance(land_cover, dict) else {}
    classes = [item for item in (cover.get("classes") or []) if isinstance(item, dict)]
    if cover.get("status") == "available" and classes:
        return _land_cover_answer(requested, cover, classes)

    mechanisms = _scattering_sentence(scattering)
    heuristic = _land_water_sentence(land_water)
    basis = mechanisms or heuristic
    domain = cover.get("domain") or {}
    if cover.get("status") == "out_of_domain":
        return (
            f"I cannot name land-cover classes for this scene: it lies outside the training footprint of "
            f"Raikou's only land-cover model ({domain.get('training_region', 'its training region')}, "
            f"{domain.get('label_space', 'a regional label space')}), so those classes do not describe this "
            f"location and are withheld. {basis}"
        )
    if cover.get("status") == "domain_unverified":
        return (
            f"I cannot name land-cover classes for this scene: Raikou's land-cover model is only valid inside "
            f"{domain.get('training_region', 'its training region')}, and this scene carries no georeference to "
            f"check that against, so those classes are withheld. {basis}"
        )
    if mechanisms:
        return (
            f"Raikou has no land-cover classifier that applies to this scene, so I cannot name a class for "
            f"{requested}. {mechanisms}"
        )
    return (
        f"I cannot confirm {requested} from the current evidence. Raikou has no calibrated {requested} "
        "segmentation or validated detector for this scene, and a SARCLIP patch cannot establish it. "
        f"{heuristic} It is only a backscatter heuristic, not land-cover classification."
    )


def _scattering_sentence(scattering: dict[str, Any] | None) -> str:
    """Report the scattering mechanism split, with what it cannot say attached."""
    block = scattering if isinstance(scattering, dict) else {}
    mechanisms = [
        item
        for item in (block.get("mechanisms") or [])
        if isinstance(item, dict) and isinstance(item.get("mechanism"), str)
    ]
    if not mechanisms:
        return ""
    ordered = sorted(mechanisms, key=lambda item: -float(item.get("fraction") or 0.0))
    named = "; ".join(
        "{:.0%} {}".format(float(item.get("fraction") or 0.0), item.get("description") or item["mechanism"])
        for item in ordered
    )
    denoised = (
        "calibrated VV/VH intensity after thermal-noise removal"
        if block.get("is_denoised")
        else "calibrated VV/VH intensity"
    )
    return (
        f"What I can measure is how the surface scatters, from {denoised}: {named}. "
        "That is set by surface geometry rather than by region, so it holds here, but it describes a "
        "mechanism and not a land use -- a canopy scatters this way whether it is plantation, mangrove "
        "or tall crop, and those cannot be separated from radar intensity alone."
    )


def _land_cover_answer(
    requested: str,
    cover: dict[str, Any],
    classes: list[dict[str, Any]],
) -> str:
    named = ", ".join(
        "{} (confident across {:.0%} of sampled windows)".format(
            str(item.get("label")), float(item.get("present_in_window_fraction") or 0.0)
        )
        for item in classes[:4]
    )
    metrics = ((cover.get("provenance") or {}).get("reported_metrics") or {})
    macro_ap = metrics.get("average_precision_macro")
    accuracy_text = (
        f" The model reports {float(macro_ap):.2f} macro average precision on its own benchmark,"
        " so treat weak or absent classes as inconclusive rather than as evidence of absence."
        if isinstance(macro_ap, (int, float))
        else ""
    )
    return (
        f"Scene-level land cover for this scene: {named}. That is multi-label scene context, "
        f"not a segmentation: it indicates what the scene broadly contains, not where a boundary lies, "
        f"and it is not detector evidence for any object.{accuracy_text} "
        f"Whether that answers your question about {requested} is a judgement the classes above should "
        "support explicitly, not by inference."
    )


def _land_water_sentence(land_water: dict[str, Any] | None) -> str:
    context = land_water if isinstance(land_water, dict) else {}
    label = str(context.get("label") or "indeterminate")
    if label == "likely_water_dominant":
        return "The scene's low-backscatter heuristic is water-dominant."
    if label == "likely_land_dominant":
        return "The scene's low-backscatter heuristic is land-dominant."
    if label == "mixed_or_indeterminate":
        return "The scene's land/water heuristic is mixed or indeterminate."
    return "No usable land/water context estimate is available."
