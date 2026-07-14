import logging

logger = logging.getLogger(__name__)

# Config: Keyword lists
MACRO_PHRASES = ["overview", "describe", "what is this", "summarize", "whole scene", "general context"]
MICRO_PHRASES = ["find", "where", "how many", "locate", "near", "coordinates"]
CACHED_MACRO_PHRASES = ["what is this", "describe this image", "give me an overview", "summarize this scene"]

# Config: Thresholds
ABSOLUTE_SCORE_FLOOR = 0.20
STEEP_DROPOFF_THRESHOLD = 0.05

def stage_1_keyword_pass(query_text: str) -> str:
    query_lower = query_text.lower()
    
    has_macro = any(phrase in query_lower for phrase in MACRO_PHRASES)
    has_micro = any(phrase in query_lower for phrase in MICRO_PHRASES)
    
    if has_macro and has_micro:
        return "hybrid"
    elif has_macro:
        return "macro"
    elif has_micro:
        return "micro"
    else:
        return "none"

def stage_2_score_spread(scores: list[float]) -> str:
    """
    Expects a sorted list of scores descending.
    """
    if not scores:
        return "fail_floor"
        
    top_score = scores[0]
    if top_score < ABSOLUTE_SCORE_FLOOR:
        return "fail_floor"
        
    bottom_index = min(len(scores) - 1, 9)
    bottom_score = scores[bottom_index]
    
    dropoff = top_score - bottom_score
    if dropoff > STEEP_DROPOFF_THRESHOLD:
        return "steep"
    else:
        return "flat"

def resolve_routing(stage_1: str, stage_2: str) -> str:
    """
    Resolves the final routing based on Stage 1 and Stage 2 outputs.
    Note: 'macro' from stage_1 bypasses stage_2 (mostly).
    This function is called when retrieval happens.
    """
    if stage_1 == "hybrid":
        if stage_2 == "fail_floor":
            return "macro"
        return "hybrid"
        
    if stage_2 == "fail_floor":
        return "macro"
    elif stage_2 == "flat":
        return "hybrid"
    elif stage_2 == "steep":
        return "micro"
        
    return "micro"

def check_cached_macro(query_text: str) -> bool:
    """
    Returns True if the query strongly matches purely descriptive intent.
    """
    query_lower = query_text.lower()
    return any(phrase in query_lower for phrase in CACHED_MACRO_PHRASES)
