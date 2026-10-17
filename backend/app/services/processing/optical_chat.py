"""Deterministic chat over one optical land-cover run (scene-scoped).

Most answers are composed here, in code, from the run's scene record, so every
number in them is a field of that record. The language model is only the
fallback for questions none of these intents covers; it gets the same record
as bounded context and an instruction never to add numbers.

Wording rules, shared with ``services/optical/record.py``:
* model figures are agreement with WorldCover-2021-derived labels on held-out
  Indian tiles, never "accuracy";
* where the map and WorldCover 2021 differ, those are differences, never
  "change": the comparison is an agreement matrix;
* no object counting, ownership, or military, border, surveillance, policing
  or extractive-planning questions (the last group is barred by the OlmoEarth
  Artifact License as well as being out of scope).

Pure functions only: the chat route owns I/O and the model call.
"""

from __future__ import annotations

import re
from typing import Any, Literal

OPTICAL_RECORD_TYPE = "optical_scene_analysis"

OpticalIntent = Literal[
    "refuse_security",
    "refuse_objects",
    "refuse_ownership",
    "download",
    "spectral",
    "similarity",
    "change",
    "trust",
    "imagery",
    "method",
    "area",
    "summary",
    "general",
]

# Every message mode this module can produce; the messages_mode_check
# migration must list each of them (tests/test_optical_chat.py checks).
ANSWER_INTENTS = ("download", "spectral", "similarity", "change", "trust", "imagery", "method", "area", "summary")
MESSAGE_MODES = (
    *(f"optical_record_{intent}" for intent in ANSWER_INTENTS),
    "optical_refusal",
    "optical_insufficient",
    "optical_grounded",
    "optical_grounded_failed",
)

_SECURITY = (
    r"\bmilitar(?:y|ies)\b", r"\barmy\b", r"\btroops?\b", r"\bweapons?\b", r"\bmissiles?\b", r"\bbunkers?\b",
    r"\b(?:border|frontier) (?:security|patrol\w*|crossing\w*|posts?|outposts?|fences?|surveillance|monitoring|incursions?|infiltration)\b",
    r"\b(?:international|national|country|state|indian?|pakistan\w*|china|chinese|bangladesh\w*) border\b",
    r"\b(?:near|along|across|at|on|crossing|over) the border\b",
    r"\bline of (?:actual )?control\b", r"\bsurveil\w*", r"\bspy(?:ing)?\b", r"\bespionage\b", r"\breconnaissance\b",
    r"\bpolic(?:e|ing)\b", r"\bintelligence (?:gathering|agenc\w+|operations?)\b", r"\b(?:air|naval|military|army) bases?\b",
    r"\bdefen[cs]e (?:forces?|installations?|bases?|sites?|establishments?)\b", r"\binfiltrat\w*", r"\bsmuggl\w*",
    r"\bwhere (?:to|should|could|can|do) (?:i |we |they )?(?:mine|drill|dig|log|clear|deforest)\b",
    r"\b(?:mining|drilling|logging|quarrying) (?:sites?|locations?|potential|prospects?|targets?)\b",
    r"\bsites? (?:for|to) (?:mining|drilling|logging|quarrying)\b",
)
_COUNT = (r"\bhow many\b", r"\bnumber of\b", r"\bcount\w*\b", r"\bdetect\w*\b", r"\bidentify (?:each|every|all|the)\b", r"\blocate (?:each|every|all|the)\b", r"\bwhere (?:are|is) the\b")
_OBJECTS = {
    "building", "buildings", "house", "houses", "home", "homes", "hut", "huts", "tent", "tents", "roof", "roofs",
    "car", "cars", "vehicle", "vehicles", "truck", "trucks", "bus", "buses", "ship", "ships", "boat", "boats",
    "vessel", "vessels", "aircraft", "plane", "planes", "airplane", "airplanes", "helicopter", "helicopters",
    "tree", "trees", "people", "person", "persons", "animal", "animals", "cattle", "cow", "cows", "tower", "towers",
    "pool", "pools", "tank", "tanks", "panel", "panels", "well", "wells", "pole", "poles",
}
_OWNERSHIP = (
    r"\bwho owns\b", r"\bowner(?:s|ship)?\b", r"\bproperty\b", r"\bcadastr\w*", r"\bsurvey (?:number|no)\b",
    r"\bland records?\b", r"\blegal(?:ly)? (?:boundar\w+|titles?|status)\b", r"\bplot (?:numbers?|nos?|boundar\w+)\b",
    r"\btitle deeds?\b", r"\bpatta\b", r"\bkhasra\b", r"\bencroach\w*",
)
_DOWNLOAD = (r"\bdownload\w*", r"\bqgis\b", r"\barcgis\b", r"\bgis\b", r"\bshapefiles?\b", r"\bgeopackage\b", r"\bgpkg\b", r"\bgeotiffs?\b", r"\btiff?\b", r"\bcogs?\b", r"\bbundle\b", r"\bexport\w*", r"\bfiles?\b")
_SPECTRAL = (r"\bndvi\b", r"\bmndwi\b", r"\bndbi\b", r"\bspectral\b", r"\bind(?:ex|ices)\b", r"\bgreenness\b", r"\breflectance\b", r"\bbands?\b")
_SIMILARITY = (r"\bsimilar\w*", r"\blooks? like\b", r"\bresembl\w*", r"\bsame (?:as|kind)\b", r"\bfind (?:more|other)\b", r"\blook alike\b")
_CHANGE = (
    r"\bchang\w*", r"\btransition\w*", r"\bconver(?:t|ted|sion)\b", r"\bdeforest\w*", r"\burban (?:growth|expansion|sprawl)\b",
    r"\bexpan(?:d|ded|sion)\b", r"\bshrank\b|\bshrunk\b|\bshrink\w*", r"\b(?:lost|loss|gain|gained|grew|growth)\b",
    r"\bsince 20\d\d\b", r"\bbetween 20\d\d and 20\d\d\b", r"\bhappened\b", r"\bcompar\w*", r"\bdiffer\w*",
    r"\bagree\w* with worldcover\b", r"\bagreement matrix\b", r"\bvs\.? worldcover\b|\bversus worldcover\b",
)
_TRUST = (
    r"\baccura\w*", r"\breliab\w*", r"\btrust\w*", r"\bhow good\b", r"\bconfiden\w*", r"\bscores?\b", r"\bvalidat\w*",
    r"\berrors?\b", r"\bmistakes?\b", r"\bwrong\b", r"\bcorrect\w*", r"\bprecis\w*", r"\biou\b", r"\bf1\b", r"\bbenchmark\w*",
    r"\b(?:un)?certain\w*", r"\bquality\b", r"\bagreement\b",
)
_IMAGERY = (
    r"\bwhen\b", r"\bdates?\b", r"\bimage(?:ry|s)?\b", r"\bsatellite\b", r"\bsentinel\w*", r"\bclouds?\b|\bcloudy\b",
    r"\bseasons?\b|\bseasonal\b", r"\bresolution\b", r"\bpixels?\b", r"\bprocessing units?\b", r"\bacqui\w*",
)
_METHOD = (
    r"\bhow (?:was|is|does|did) (?:this|the|it)\b.*\b(?:made|built|trained|work\w*|comput\w*|classif\w*|generat\w*|produc\w*)\b",
    r"\bmodel\b", r"\bmethod\w*", r"\btrain\w*", r"\bolmoearth\b", r"\bworldcover\b", r"\bwhat (?:do|does) (?:the )?class\w* mean\b",
    r"\bwhich classes\b|\bwhat classes\b", r"\blicen[cs]\w*", r"\bcit(?:e|ation)\b", r"\battribut\w*",
)
_AREA = (
    r"\bhow much\b", r"\barea\b", r"\bpercent\w*", r"%", r"\bshares?\b", r"\bhectares?\b", r"\bha\b", r"\bkm2\b|km²|\bsq ?km\b|\bsquare (?:km|kilomet\w+)\b",
    r"\bproportion\b", r"\bfraction\b", r"\bdominant\b", r"\bmost\b", r"\blargest\b", r"\bbreakdown\b", r"\bcomposition\b",
    r"\bland ?cover\b", r"\blulc\b", r"\bcover(?:ed|s)?\b", r"\bis there\b|\bare there\b",
)
_SUMMARY = (
    r"\bdescribe\b", r"\bsummar\w*", r"\boverview\b", r"\bwhat(?:'s| is) (?:here|in this|this area|going on)\b", r"\btell me about\b",
    r"\bexplain (?:this|the) (?:map|run|area|result)\b", r"\bwhat does (?:this|the) map show\b", r"\bwhat am i looking at\b",
)
# Words that point a question at one class, in the record's class names.
_CLASS_WORDS = {
    "Built-up": ("built", "urban", "city", "town", "settlement", "concrete", "paved"),
    "Agriculture": ("agricultur", "crop", "farm", "cultivat", "paddy", "field", "cropland"),
    "Forest & natural vegetation": ("forest", "vegetat", "tree", "wood", "green", "shrub", "grass", "mangrove", "jungle"),
    "Barren / wasteland": ("barren", "bare", "waste", "sand", "rock", "desert", "fallow"),
    "Water bodies & wetlands": ("water", "wetland", "river", "lake", "pond", "sea", "reservoir", "tank", "creek", "estuar", "marsh"),
}


def _normalize(query: str) -> str:
    return " ".join(str(query or "").casefold().replace("’", "'").split())


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify_optical_query(query: str) -> OpticalIntent:
    """Route an optical question by what can honestly answer it. Refusals first."""
    text = _normalize(query)
    words = set(re.findall(r"[a-z0-9]+", text))
    if _matches(_SECURITY, text):
        return "refuse_security"
    if words & _OBJECTS and _matches(_COUNT, text):
        return "refuse_objects"
    if _matches(_OWNERSHIP, text):
        return "refuse_ownership"
    for intent, patterns in (
        ("download", _DOWNLOAD),
        ("spectral", _SPECTRAL),
        ("similarity", _SIMILARITY),
        ("change", _CHANGE),
        ("trust", _TRUST),
        ("summary", _SUMMARY),
        ("imagery", _IMAGERY),
        ("method", _METHOD),
        ("area", _AREA),
    ):
        if _matches(patterns, text):
            return intent  # type: ignore[return-value]
    if mentioned_classes(text):
        return "area"
    return "general"


def mentioned_classes(query: str) -> list[str]:
    text = _normalize(query)
    return [name for name, stems in _CLASS_WORDS.items() if any(stem in text for stem in stems)]


def message_mode(intent: OpticalIntent) -> str:
    if intent.startswith("refuse_"):
        return "optical_refusal"
    if intent == "general":
        return "optical_grounded"
    return f"optical_record_{intent}"


# --------------------------------------------------------------------------- record readers


def _lulc(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("lulc") if isinstance(record.get("lulc"), dict) else {}


def _imagery(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("imagery") if isinstance(record.get("imagery"), dict) else {}


def _classes(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [c for c in _lulc(record).get("classes") or [] if isinstance(c, dict)]
    return sorted(rows, key=lambda c: c.get("share_pct") or 0, reverse=True)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any, digits: int = 1) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:.{digits}f}%"


def _km2(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "n/a"
    return f"{number:,.2f} km²" if number >= 1 else f"{number:.3f} km²"


def _ratio_pct(value: Any) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{100 * number:.1f}%"


def _dates_used(record: dict[str, Any]) -> list[str]:
    imagery = _imagery(record)
    steps = [s for s in imagery.get("timesteps") or [] if isinstance(s, dict)]
    used = imagery.get("timesteps_used") or list(range(len(steps)))
    return [str(steps[i].get("sensing_start", ""))[:10] for i in used if isinstance(i, int) and 0 <= i < len(steps) and steps[i].get("sensing_start")]


def _date_phrase(record: dict[str, Any]) -> str:
    dates = _dates_used(record)
    if not dates:
        return "the image date"
    return dates[0] if len(dates) == 1 else ", ".join(dates[:-1]) + f" and {dates[-1]}"


def _evaluation(record: dict[str, Any]) -> dict[str, Any]:
    ev = _lulc(record).get("evaluation")
    return ev if isinstance(ev, dict) else {}


def _evaluation_phrase(record: dict[str, Any]) -> str:
    ev = _evaluation(record)
    applies = "three-season" if ev.get("applies_to") == "seasonal_stack" else "single-date"
    return (
        f"on held-out Indian tiles the model agrees with WorldCover-2021-derived labels on {_ratio_pct(ev.get('agreement_overall'))} "
        f"of pixels ({applies} benchmark; mean IoU {_num(ev.get('mean_iou')) or 0:.2f})"
    )


def _sentence(text: str) -> str:
    """Capitalise the first letter only; str.capitalize() would lowercase "Indian" and "WorldCover"."""
    return text[:1].upper() + text[1:]


def _artifact_name(record: dict[str, Any], suffix: str) -> str | None:
    artifacts = _lulc(record).get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    return next((name for name in artifacts if str(name).endswith(suffix)), None)


def _bundle_names(record: dict[str, Any]) -> dict[str, str]:
    """What this run's GIS bundle holds, and where its tables are.

    Runs made before the bundle was simplified have a flat folder of rasters,
    CSV tables and a GeoPackage. Newer runs ship only the rasters and their
    style files, and the tables are on the page. The record names the files it
    stored, so the answer says what is actually in the user's download.
    """
    if _artifact_name(record, "_landcover.tif"):
        return {
            "layout": "current",
            "map": _artifact_name(record, "_landcover.tif") or "the *_landcover.tif map",
            "statistics": "the class table on this page",
            "agreement": "the agreement table (ask me for it)",
            "spectral": "the spectral table on this page",
        }
    return {
        "layout": "legacy",
        "map": _artifact_name(record, "_lulc_10m.tif") or "the *_lulc_10m.tif map",
        "statistics": "lulc_stats.csv",
        "agreement": "agreement_matrix.csv",
        "spectral": "spectral_indices.csv",
    }


def _limitations(record: dict[str, Any], *needles: str) -> list[str]:
    items = [str(x) for x in record.get("limitations") or [] if isinstance(x, str)]
    if not needles:
        return items
    return [x for x in items if any(n in x.lower() for n in needles)]


def _class_line(c: dict[str, Any]) -> str:
    parts = [f"{c.get('class_name')}: {_pct(c.get('share_pct'))} ({_km2(c.get('area_sqkm'))}, {_num(c.get('area_hectares')) or 0:,.0f} ha"]
    if c.get("mean_score_pct") is not None:
        parts.append(f"; mean score {_pct(c.get('mean_score_pct'), 0)}")
    parts.append(")")
    wc = c.get("worldcover_2021_area_sqkm")
    same = c.get("same_class_in_worldcover_2021_pct")
    tail = ""
    if wc is not None:
        tail = f". WorldCover 2021 maps {_km2(wc)} as this class"
        if same is not None:
            tail += f", and {_pct(same, 0)} of the pixels mapped here share its class"
    return "- " + "".join(parts) + tail


# --------------------------------------------------------------------------- answers

REFUSALS = {
    "refuse_security": (
        "I can't help with military, border, surveillance, policing or extractive-planning questions. The model's licence "
        "(the OlmoEarth Artifact License) bars those uses, and this tool is for land-cover research and teaching. I can tell "
        "you what covers the ground here, how much of each class there is, how the map compares with WorldCover 2021, or how "
        "far to trust it."
    ),
    "refuse_objects": (
        "This map gives each 10 m pixel one of five land-cover classes. It does not detect, count or locate individual objects "
        "such as buildings, vehicles, trees or people, so I can't answer that from it. I can tell you how much of the area is "
        "Built-up, Agriculture, Forest & natural vegetation, Barren / wasteland or Water bodies & wetlands."
    ),
    "refuse_ownership": (
        "Land cover says nothing about who owns or uses land, or where legal boundaries run; for that, use the land records. "
        "I can tell you what covers the ground here and how much of each class there is."
    ),
}


def _area_answer(record: dict[str, Any], query: str) -> str:
    classes = _classes(record)
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    lulc = _lulc(record)
    asked = mentioned_classes(query)
    lead: list[str] = []
    if asked:
        for name in asked:
            row = next((c for c in classes if c.get("class_name") == name), None)
            if row is None:
                continue
            share = _num(row.get("share_pct")) or 0.0
            lead.append(
                f"{name} covers {_pct(share)} of the mapped area ({_km2(row.get('area_sqkm'))})."
                if share > 0
                else f"The map shows no {name} here."
            )
    mapped = _num(lulc.get("mapped_fraction"))
    header = (
        f"On {_date_phrase(record)}, this map covers {_km2(scene.get('area_sqkm'))}"
        + (f", {100 * mapped:.0f}% of it with a clear observation" if mapped is not None else "")
        + ":"
    )
    lines = [*lead, header, *[_class_line(c) for c in classes if (_num(c.get("share_pct")) or 0) > 0]]
    if lulc.get("dominant_class"):
        lines.append(f"The largest class is {lulc['dominant_class']}.")
    lines.append(
        "Scores are calibrated agreement with WorldCover-2021-derived labels, not field-verified accuracy; "
        f"the GIS bundle has the same figures in {_bundle_names(record)['statistics']}."
    )
    return "\n".join(lines)


def _change_answer(record: dict[str, Any]) -> str:
    reference = record.get("reference") if isinstance(record.get("reference"), dict) else {}
    matrix = reference.get("agreement_matrix_km2")
    names = reference.get("class_order") or [c.get("class_name") for c in _lulc(record).get("classes") or []]
    lines = [
        f"This compares the map ({_date_phrase(record)}) with ESA WorldCover 2021, remapped to the same five classes. "
        "It is an agreement matrix, not change detection."
    ]
    if isinstance(matrix, list) and matrix and all(isinstance(r, list) for r in matrix):
        total = sum(float(v) for r in matrix for v in r)
        same = sum(float(matrix[i][i]) for i in range(min(len(matrix), len(matrix[0]))))
        if total > 0:
            lines.append(f"The two maps agree on {100 * same / total:.1f}% of the {_km2(total)} they both cover.")
            cells = [
                (float(v), str(names[i]) if i < len(names) else f"class {i + 1}", str(names[j]) if j < len(names) else f"class {j + 1}")
                for i, r in enumerate(matrix) for j, v in enumerate(r) if i != j and float(v) > 0
            ]
            cells.sort(reverse=True)
            if cells:
                lines.append("Where they differ, the largest differences are:")
                lines += [f"- WorldCover 2021 says {a}, this map says {b}: {_km2(v)}" for v, a, b in cells[:3]]
    ev = _evaluation(record)
    agreement = _num(ev.get("agreement_overall"))
    if agreement is not None:
        lines.append(
            f"On held-out tiles where nothing changed by construction, the model already disagrees with WorldCover on about "
            f"{100 * (1 - agreement):.0f}% of pixels, so differences at or below that level are not evidence of change."
        )
    lines.append(
        "Every difference mixes real change with errors in both maps. Check it against the true-colour image and the score "
        "layer before reading it as change."
    )
    return "\n".join(lines)


def _trust_answer(record: dict[str, Any]) -> str:
    lulc = _lulc(record)
    ev = _evaluation(record)
    lines = [
        f"How far to trust this map: {_evaluation_phrase(record)}, macro F1 {_num(ev.get('macro_f1')) or 0:.2f}. "
        "That is agreement with a reference map, not field-verified accuracy."
    ]
    per_class = ev.get("per_class_iou") if isinstance(ev.get("per_class_iou"), dict) else {}
    if per_class:
        lines.append("Per-class agreement (IoU): " + ", ".join(f"{k} {(_num(v) or 0):.2f}" for k, v in per_class.items() if v is not None) + ".")
    score = lulc.get("score") if isinstance(lulc.get("score"), dict) else {}
    if score.get("meaning"):
        lines.append(f"Scores are {score['meaning']}" + (f" (temperature {float(score['temperature']):.2f})" if _num(score.get("temperature")) else "") + ".")
    scored = [c for c in _classes(record) if c.get("mean_score_pct") is not None and (_num(c.get("share_pct")) or 0) > 0]
    if scored:
        lines.append("Mean score in this run: " + ", ".join(f"{c['class_name']} {_pct(c['mean_score_pct'], 0)}" for c in scored) + ".")
    caveats = _limitations(record, "single date", "outside india", "no clear observation", "floods seasonally", "two dates")
    lines += [f"- {item}" for item in caveats]
    bundle = _bundle_names(record)
    if bundle["layout"] == "current":
        lines.append(
            f"To audit it in QGIS: open the GIS bundle, load {bundle['map']} (its .qml styles it) and the confidence map over a "
            "satellite basemap, look first at low-confidence areas, and compare with the WorldCover 2021 raster in extras/."
        )
    else:
        lines.append(
            f"To audit it in QGIS: open the GIS bundle, load {bundle['map']} (its .qml styles it), the WorldCover 2021 raster and the "
            f"score raster over the true-colour image, look first at low-score areas, and compare classes with {bundle['agreement']}."
        )
    return "\n".join(lines)


def _spectral_answer(record: dict[str, Any]) -> str:
    spectral = record.get("spectral") if isinstance(record.get("spectral"), dict) else {}
    rows = [r for r in spectral.get("rows") or [] if isinstance(r, dict)]
    if not rows:
        return "This run was made before spectral indices were added. Reprocess it to add them; the stored imagery is reused, so no processing units are spent."
    lines = ["Spectral index medians over clear pixels of each mapped class (harmonised Sentinel-2 L2A):"]
    for date in sorted({str(r.get("date")) for r in rows}):
        day = [r for r in rows if str(r.get("date")) == date and (r.get("clear_pixels") or 0) > 0]
        if not day:
            continue
        used = any(r.get("used_by_model") for r in day)
        lines.append(f"{date}{'' if used else ' (not used by the model: too cloudy)'}:")
        for r in day:
            values = ", ".join(
                f"{k.upper()} {float(r[f'{k}_median']):.2f}" for k in ("ndvi", "mndwi", "ndbi") if r.get(f"{k}_median") is not None
            )
            lines.append(f"- {r.get('class_name')}: {values} ({_km2(r.get('clear_area_sqkm'))} clear)")
    definitions = spectral.get("indices") if isinstance(spectral.get("indices"), dict) else {}
    if definitions:
        lines.append("; ".join(f"{k.upper()} = {v}" for k, v in definitions.items()) + ".")
    lines.append(
        f"They describe the image on that date, not field measurements; {_bundle_names(record)['spectral']} in the GIS bundle has every value."
    )
    return "\n".join(lines)


def _similarity_answer(record: dict[str, Any]) -> str:
    similarity = record.get("similarity") if isinstance(record.get("similarity"), dict) else {}
    if not similarity.get("available"):
        return "This run was made before similarity search was added. Reprocess it to add it; the stored imagery is reused, so no processing units are spent."
    token = _num(similarity.get("token_m")) or 40
    window = _num(similarity.get("window_m")) or 640
    lines = [
        f"Use “Find similar areas” on the map: click any spot and every {token:.0f} m cell is shaded by how alike it looks to the model, "
        f"or search your other optical runs for the most similar {window:.0f} m windows.",
        str(similarity.get("meaning") or ""),
    ]
    energy = _num(similarity.get("energy_captured"))
    if energy is not None:
        lines.append(f"The heatmap uses {similarity.get('basis_dims')} directions that keep {100 * energy:.0f}% of the features' energy.")
    return "\n".join(line for line in lines if line)


def _imagery_answer(record: dict[str, Any]) -> str:
    imagery = _imagery(record)
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    grid = scene.get("grid") if isinstance(scene.get("grid"), dict) else {}
    steps = [s for s in imagery.get("timesteps") or [] if isinstance(s, dict)]
    used = set(imagery.get("timesteps_used") or range(len(steps)))
    lines = [
        "Imagery: Copernicus Sentinel-2 L2A from the CDSE Process API, harmonised to one reflectance scale and placed on a native "
        f"UTM 10 m grid (EPSG:{grid.get('epsg', '?')}, {grid.get('width', '?')} x {grid.get('height', '?')} px, {_km2(scene.get('area_sqkm'))})."
    ]
    for i, step in enumerate(steps):
        clear = _num(step.get("valid_fraction"))
        note = "used" if i in used else "not used: too cloudy"
        season = f", {str(step.get('season')).replace('_', ' ')}" if step.get("season") else ""
        lines.append(f"- {str(step.get('sensing_start', ''))[:10]}{season}: {100 * clear:.0f}% clear, {note}" if clear is not None else f"- {str(step.get('sensing_start', ''))[:10]}: {note}")
    mode = "a seasonal stack" if imagery.get("mode") == "seasonal_stack" else "a single date"
    lines.append(f"The run is {mode}.")
    if imagery.get("processing_units") is not None:
        lines.append(f"Fetching it spent {float(imagery['processing_units']):.2f} Sentinel Hub processing units.")
    return "\n".join(lines)


def _method_answer(record: dict[str, Any]) -> str:
    lulc = _lulc(record)
    model = lulc.get("model") if isinstance(lulc.get("model"), dict) else {}
    names = [c.get("class_name") for c in lulc.get("classes") or [] if isinstance(c, dict)]
    trained = _limitations(record, "trained on")
    lines = [
        f"The map comes from {model.get('name') or 'the Raikou OlmoEarth India model'} {model.get('version') or ''}".rstrip()
        + f", a land-cover head fine-tuned on {model.get('base_model') or 'OlmoEarth v1.2 Base'} (Ai2).",
    ]
    if trained:
        lines.append(trained[0])
    if names:
        lines.append("Its five classes: " + ", ".join(str(n) for n in names) + ".")
    lines.append(
        "It reads up to three dates, predicts every 10 m pixel in overlapping 128 px windows, and a 4-pixel sieve removes specks; "
        f"the WorldCover 2021 reference is read onto the same grid. {_sentence(_evaluation_phrase(record))}, not field-verified accuracy."
    )
    if model.get("licence"):
        lines.append(f"Licence: {model['licence']}. It bars military, surveillance, policing and extractive uses.")
    return "\n".join(lines)


def _download_answer(record: dict[str, Any]) -> str:
    artifacts = _lulc(record).get("artifacts") if isinstance(_lulc(record).get("artifacts"), dict) else {}
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    grid = scene.get("grid") if isinstance(scene.get("grid"), dict) else {}
    files = sorted(name for name in artifacts if "." in str(name))
    if _bundle_names(record)["layout"] == "current":
        lines = [
            "Download the GIS bundle (one zip) from this run's results. It holds the land-cover map (classes 1-5, with a colour table, "
            "an ArcGIS .clr and a QGIS .qml style), the per-pixel confidence map, and in extras/ the WorldCover 2021 reference on the "
            "same grid and the per-class probabilities. Class areas, agreement with WorldCover 2021 and spectral indices are on this "
            "page and in this chat, not in the zip."
        ]
    else:
        lines = [
            "Download the GIS bundle (one zip) from this run's results. It holds the land-cover map (classes 1-5 with a colour table and "
            "QGIS .qml / .clr styles), the calibrated score and per-class probability rasters, the WorldCover 2021 reference on the same "
            "grid, polygons as a GeoPackage, lulc_stats.csv, agreement_matrix.csv, spectral_indices.csv, the model-view and similarity "
            "rasters, and VALIDATION_AND_CITATION.txt with how to cite it."
        ]
    if grid.get("epsg"):
        lines.append(f"Rasters are Cloud-Optimised GeoTIFFs on EPSG:{grid['epsg']}; drag them into QGIS or ArcGIS.")
    if files:
        lines.append("Files in this run: " + ", ".join(files[:20]) + ("…" if len(files) > 20 else "") + ".")
    return "\n".join(lines)


def _summary_answer(record: dict[str, Any]) -> str:
    from app.services.optical.record import summary_sentence

    lines = [summary_sentence(record)]
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    if scene.get("in_training_domain") is False:
        lines.append("This area lies outside India, the model's training domain, so treat the map as unvalidated here.")
    lines += _limitations(record, "single date", "no clear observation", "floods seasonally")[:2]
    lines.append("Ask how much of a class there is, how it compares with WorldCover 2021, or how far to trust it.")
    return "\n".join(lines)


_ANSWERS = {
    "area": lambda record, query: _area_answer(record, query),
    "change": lambda record, query: _change_answer(record),
    "trust": lambda record, query: _trust_answer(record),
    "spectral": lambda record, query: _spectral_answer(record),
    "similarity": lambda record, query: _similarity_answer(record),
    "imagery": lambda record, query: _imagery_answer(record),
    "method": lambda record, query: _method_answer(record),
    "download": lambda record, query: _download_answer(record),
    "summary": lambda record, query: _summary_answer(record),
}


def compose_optical_answer(intent: OpticalIntent, record: dict[str, Any], query: str = "") -> str | None:
    """The deterministic answer, or None when only the fallback can answer."""
    if intent in REFUSALS:
        return REFUSALS[intent]
    builder = _ANSWERS.get(intent)
    return builder(record, query) if builder else None


# --------------------------------------------------------------------------- fallback context

OPTICAL_SYSTEM_PROMPT = (
    "You answer questions about one Sentinel-2 land-cover map, using only the record below. Never invent numbers, places, "
    "objects, dates or events. Call the model's figures agreement with WorldCover 2021, never accuracy. Call differences from "
    "WorldCover 2021 differences, never change. If the record does not answer the question, say so in one sentence and say what "
    "it can answer. At most six sentences."
)
FALLBACK_FOOTER = (
    "\n\n(Written by the language model from this run's record. The class table and the GIS bundle hold the authoritative numbers.)"
)


def optical_context_text(record: dict[str, Any], limit: int = 4000) -> str:
    """The record, compact and bounded, for the fallback model."""
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    lines = [
        "OPTICAL RUN RECORD (the only source; do not add numbers):",
        f"Area: {scene.get('name') or 'unnamed'}, {_km2(scene.get('area_sqkm'))}, centre {scene.get('centroid_lonlat')}, "
        f"in the training domain (India): {scene.get('in_training_domain')}",
        f"Imagery: Sentinel-2 L2A; dates used: {', '.join(_dates_used(record)) or 'unknown'}",
        "Classes (share of mapped area, area, mean score, same class in WorldCover 2021):",
    ]
    for c in _classes(record):
        lines.append(
            f"- {c.get('class_name')}: {_pct(c.get('share_pct'))}, {_km2(c.get('area_sqkm'))}, "
            f"score {_pct(c.get('mean_score_pct'), 0)}, same in WorldCover 2021 {_pct(c.get('same_class_in_worldcover_2021_pct'), 0)}"
        )
    lines.append(f"Model: {_evaluation_phrase(record)}.")
    lines.append("Limitations:")
    lines += [f"- {item}" for item in _limitations(record)]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- citations


def optical_citations(record: dict[str, Any], *, scene_id: str, record_artifact_id: str | None, intent: OpticalIntent) -> list[dict[str, Any]]:
    """What was given to answer: the record, the map, the image, and the layer the intent leans on."""
    artifacts = _lulc(record).get("artifacts") if isinstance(_lulc(record).get("artifacts"), dict) else {}
    out: list[dict[str, Any]] = []
    if record_artifact_id:
        out.append(
            {
                "source_type": "metadata",
                "source_id": scene_id,
                "scene_id": scene_id,
                "artifact_id": record_artifact_id,
                "why_provided": "The optical scene record: class areas, agreement with WorldCover 2021 and the run's limitations.",
                "provenance": {"kind": "scene_record", "record_type": OPTICAL_RECORD_TYPE},
            }
        )
    if intent.startswith("refuse_"):
        return out

    def add(source_type: str, name: str, why: str, **provenance: Any) -> None:
        artifact_id = artifacts.get(name)
        if artifact_id:
            out.append(
                {
                    "source_type": source_type,
                    "source_id": str(artifact_id),
                    "scene_id": scene_id,
                    "artifact_id": str(artifact_id),
                    "why_provided": why,
                    "provenance": {"layer": name, "is_detector_evidence": False, **provenance},
                }
            )

    add("lulc_map", "lulc", "The land-cover map of this run (five classes at 10 m).")
    add("optical_overview", "true_color", "True-colour Sentinel-2 image of the clearest date the model used.")
    if intent == "change":
        add("lulc_map", "worldcover2021", "ESA WorldCover 2021, remapped to the same five classes, on the run's grid.", reference=True)
    if intent == "trust":
        add("lulc_map", "score", "Calibrated score per pixel: agreement with WorldCover-2021-derived labels, not accuracy.")
    if intent == "similarity":
        add("similarity_map", "similarity", "How alike each cell looks to the model compared with the run's centre cell.")
    return out
