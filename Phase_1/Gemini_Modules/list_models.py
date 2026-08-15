"""
Tools/Gemini_Modules/gemini_router_module/list_models.py — Dynamic Gemini Model Discovery & Categorization Engine
===================================================================================================================
Google Gemini AI System Architecture Specification (2026 Production Ready).

Features:
1. Queries live Google GenAI API (`client.models.list()`) using modern `google.genai` SDK (with fallback to `google.generativeai`).
2. Filters out non-generative endpoints (embeddings, imagen, aqa, bison).
3. Evaluates and scores discovered models across 10 task categories:
   - creative, reasoning, cheap, master, watermark, vision, caption, narrative, price, analysis.
4. Generates a clean, flat list of active production models sorted by capability tier.
5. Performs atomic file persistence to `storage/gemini_models_cache.json`.
6. Enforces a 5-minute cooldown timer between dynamic refresh attempts to prevent API quota storms on 404 error loops.
"""

import os
import re
import json
import time
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("list_models")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CACHE_FILE = os.path.join(_REPO_ROOT, "storage", "gemini_models_cache.json")
REFRESH_COOLDOWN_SECONDS = 300  # 5-minute throttling between API scans

# Early dotenv loading for GEMINI_API_KEY
try:
    from dotenv import load_dotenv
    for _epath in [os.path.join(_REPO_ROOT, "Credentials", ".env"), os.path.join(_REPO_ROOT, ".env")]:
        if os.path.exists(_epath):
            load_dotenv(_epath)
except ImportError:
    pass

_LAST_REFRESH_TIME = 0.0


# ── Hardcoded Baseline Default Task Matrix (Fallback) ─────────────────────────

DEFAULT_TASK_MODEL_RATINGS = {
    "creative": {
        "gemini-2.5-flash-lite": 3.0,
        "gemini-flash-lite-latest": 2.9,
        "gemini-2.0-flash-lite": 2.8,
        "gemini-2.5-flash": 2.7,
        "gemini-2.0-flash": 2.6,
        "gemini-flash-latest": 2.5,
        "gemini-2.5-pro": 1.5,
        "gemini-pro-latest": 1.1,
    },
    "reasoning": {
        "gemini-2.5-flash-lite": 3.0,
        "gemini-2.0-flash-lite": 2.9,
        "gemini-2.5-flash": 2.8,
        "gemini-2.0-flash": 2.7,
        "gemini-flash-latest": 2.6,
        "gemini-2.5-pro": 1.7,
        "gemini-pro-latest": 1.1,
    },
    "cheap": {
        "gemini-2.5-flash-lite": 3.9,
        "gemini-flash-lite-latest": 3.8,
        "gemini-2.0-flash-lite": 3.7,
        "gemini-2.0-flash-lite-001": 3.6,
        "gemini-2.5-flash": 2.0,
        "gemini-2.0-flash": 1.8,
        "gemini-flash-latest": 1.6,
    },
    "master": {
        "gemini-2.5-flash": 3.2,
        "gemini-2.0-flash": 3.0,
        "gemini-flash-latest": 2.9,
        "gemini-2.5-flash-lite": 2.7,
        "gemini-2.0-flash-lite": 2.6,
        "gemini-2.5-pro": 1.7,
        "gemini-pro-latest": 1.1,
    },
    "watermark": {
        "gemini-2.5-flash": 4.0,
        "gemini-2.0-flash": 3.8,
        "gemini-flash-latest": 3.6,
        "gemini-2.5-flash-lite": 2.5,
        "gemini-2.0-flash-lite": 2.4,
        "gemini-2.0-flash-lite-001": 2.3,
        "gemini-flash-lite-latest": 2.2,
        "gemini-2.5-pro": 1.5,
        "gemini-pro-latest": 1.1,
    },
    "vision": {
        "gemini-2.5-flash-lite": 3.8,
        "gemini-2.0-flash-lite": 3.7,
        "gemini-2.5-flash": 3.5,
        "gemini-2.0-flash": 3.3,
        "gemini-flash-latest": 3.2,
        "gemini-2.5-pro": 1.5,
        "gemini-pro-latest": 1.1,
    },
    "caption": {
        "gemini-2.5-flash": 3.5,
        "gemini-2.0-flash": 3.3,
        "gemini-flash-latest": 3.2,
        "gemini-2.5-flash-lite": 3.0,
        "gemini-2.0-flash-lite": 2.9,
        "gemini-flash-lite-latest": 2.8,
        "gemini-2.5-pro": 1.4,
        "gemini-pro-latest": 1.0,
    },
    "narrative": {
        "gemini-2.5-flash": 3.3,
        "gemini-2.0-flash": 3.1,
        "gemini-flash-latest": 3.0,
        "gemini-2.5-flash-lite": 2.8,
        "gemini-2.0-flash-lite": 2.7,
        "gemini-2.5-pro": 1.6,
        "gemini-pro-latest": 1.1,
    },
    "price": {
        "gemini-2.5-flash-lite": 3.8,
        "gemini-2.0-flash-lite": 3.7,
        "gemini-flash-lite-latest": 3.6,
        "gemini-2.5-flash": 3.3,
        "gemini-2.0-flash": 3.1,
        "gemini-2.5-pro": 1.4,
        "gemini-pro-latest": 1.1,
    },
    "analysis": {
        "gemini-2.5-flash-lite": 3.7,
        "gemini-2.0-flash-lite": 3.6,
        "gemini-2.5-flash": 3.4,
        "gemini-2.0-flash": 3.2,
        "gemini-flash-latest": 3.0,
        "gemini-2.5-pro": 1.5,
        "gemini-pro-latest": 1.0,
    },
}

DEFAULT_MODELS_LIST = [
    "gemini-2.5-pro",
    "gemini-pro-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-001",
    "gemini-flash-lite-latest",
]


# ── Dynamic Model Discovery Engine ───────────────────────────────────────────

def discover_api_models(api_key: str = "") -> List[str]:
    """
    Queries Google GenAI API for currently active models.
    Filters out non-generative models (embedding, imagen, bison, aqa).
    """
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    if not key:
        logger.warning("⚠️ No API key found for model discovery — returning default list.")
        return DEFAULT_MODELS_LIST

    discovered = []

    # 1. Try modern google.genai SDK
    try:
        from google import genai
        client = genai.Client(api_key=key)
        all_models = list(client.models.list())
        for m in all_models:
            name = getattr(m, "name", "") or getattr(m, "display_name", "")
            name = name.replace("models/", "").strip()
            if _is_valid_generative_model(name):
                discovered.append(name)
        if discovered:
            logger.info(f"✅ Discovered {len(discovered)} active Gemini models via google.genai SDK.")
            return _sort_models_by_tier(discovered)
    except Exception as e:
        logger.debug(f"google.genai models.list failed: {e}")

    # 2. Fallback to google.generativeai legacy SDK
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=key)
        all_models = list(genai_legacy.list_models())
        for m in all_models:
            name = getattr(m, "name", "").replace("models/", "").strip()
            supported_methods = getattr(m, "supported_generation_methods", [])
            if "generateContent" in supported_methods and _is_valid_generative_model(name):
                discovered.append(name)
        if discovered:
            logger.info(f"✅ Discovered {len(discovered)} active Gemini models via google.generativeai SDK.")
            return _sort_models_by_tier(discovered)
    except Exception as e:
        logger.debug(f"google.generativeai list_models failed: {e}")

    return DEFAULT_MODELS_LIST


def _is_valid_generative_model(model_name: str) -> bool:
    """Filters out embeddings, audio-only, imagen, and legacy non-gemini models."""
    name = model_name.lower()
    if not name.startswith("gemini"):
        return False
    # Exclude non-generative model types
    excluded_keywords = ["embedding", "embed", "imagen", "bison", "aqa", "gecko", "text-001"]
    for kw in excluded_keywords:
        if kw in name:
            return False
    return True


def _sort_models_by_tier(models: List[str]) -> List[str]:
    """Sorts discovered models into Pro -> Flash -> Lite tiers."""
    pro_models = [m for m in models if "pro" in m.lower()]
    lite_models = [m for m in models if "lite" in m.lower()]
    flash_models = [m for m in models if "flash" in m.lower() and "lite" not in m.lower()]
    other_models = [m for m in models if m not in pro_models and m not in flash_models and m not in lite_models]

    # Combine known production list with newly discovered models
    combined = []
    for m in DEFAULT_MODELS_LIST:
        if m in models:
            combined.append(m)
    for m in pro_models + flash_models + lite_models + other_models:
        if m not in combined:
            combined.append(m)
    return combined


def calculate_task_matrix(models: List[str]) -> Dict[str, Dict[str, float]]:
    """
    Computes heuristic category rating matrices for all 10 task categories
    based on model version, speed tier (lite vs flash vs pro), and accuracy characteristics.
    """
    matrix = {task: {} for task in DEFAULT_TASK_MODEL_RATINGS.keys()}

    for m in models:
        m_lower = m.lower()
        
        # Base version score multiplier (e.g. 2.5 > 2.0 > 1.5)
        if "3." in m_lower or "3-" in m_lower:
            version_score = 3.0
        elif "2.5" in m_lower:
            version_score = 2.5
        elif "2.0" in m_lower or "2-" in m_lower:
            version_score = 2.0
        else:
            version_score = 1.5

        is_lite = "lite" in m_lower
        is_pro = "pro" in m_lower
        is_flash = "flash" in m_lower and not is_lite

        # 1. creative (Lite highest RPM > Flash > Pro)
        if is_lite:
            matrix["creative"][m] = round(version_score + 0.5, 1)
        elif is_flash:
            matrix["creative"][m] = round(version_score + 0.2, 1)
        else:
            matrix["creative"][m] = 1.5 if is_pro else 1.0

        # 2. reasoning (High-quota Flash/Lite first, Pro fallback)
        if is_lite:
            matrix["reasoning"][m] = round(version_score + 0.5, 1)
        elif is_flash:
            matrix["reasoning"][m] = round(version_score + 0.3, 1)
        else:
            matrix["reasoning"][m] = round(version_score - 0.8, 1)

        # 3. cheap (Lite dominates)
        if is_lite:
            matrix["cheap"][m] = round(version_score + 1.4, 1)
        elif is_flash:
            matrix["cheap"][m] = round(version_score - 0.5, 1)
        else:
            matrix["cheap"][m] = 1.0

        # 4. master (Flash high-quota first, Pro fallback)
        if is_flash:
            matrix["master"][m] = round(version_score + 0.7, 1)
        elif is_lite:
            matrix["master"][m] = round(version_score + 0.2, 1)
        else:
            matrix["master"][m] = 1.7 if is_pro else 1.1

        # 5. watermark (Standard Flash highest 4.0 for forensic vision accuracy)
        if is_flash:
            matrix["watermark"][m] = round(version_score + 1.5, 1)
        elif is_lite:
            matrix["watermark"][m] = round(version_score, 1)
        else:
            matrix["watermark"][m] = 1.5 if is_pro else 1.1

        # 6. vision (High quota Lite & Flash)
        if is_lite:
            matrix["vision"][m] = round(version_score + 1.3, 1)
        elif is_flash:
            matrix["vision"][m] = round(version_score + 1.0, 1)
        else:
            matrix["vision"][m] = 1.5 if is_pro else 1.1

        # 7. caption (Flash & Lite)
        if is_flash:
            matrix["caption"][m] = round(version_score + 1.0, 1)
        elif is_lite:
            matrix["caption"][m] = round(version_score + 0.5, 1)
        else:
            matrix["caption"][m] = 1.4 if is_pro else 1.0

        # 8. narrative (Flash & Lite)
        if is_flash:
            matrix["narrative"][m] = round(version_score + 0.8, 1)
        elif is_lite:
            matrix["narrative"][m] = round(version_score + 0.3, 1)
        else:
            matrix["narrative"][m] = 1.6 if is_pro else 1.1

        # 9. price (Lite first)
        if is_lite:
            matrix["price"][m] = round(version_score + 1.3, 1)
        elif is_flash:
            matrix["price"][m] = round(version_score + 0.8, 1)
        else:
            matrix["price"][m] = 1.4 if is_pro else 1.1

        # 10. analysis (Lite & Flash)
        if is_lite:
            matrix["analysis"][m] = round(version_score + 1.2, 1)
        elif is_flash:
            matrix["analysis"][m] = round(version_score + 0.9, 1)
        else:
            matrix["analysis"][m] = 1.5 if is_pro else 1.0

    return matrix


# ── Cache & Refresh Operations ───────────────────────────────────────────────

def refresh_gemini_models_cache(api_key: str = "", force: bool = False) -> dict:
    """
    Refreshes storage/gemini_models_cache.json.
    Enforces a 5-minute cooldown timer unless force=True.
    """
    global _LAST_REFRESH_TIME
    now = time.time()
    
    if not force and (now - _LAST_REFRESH_TIME) < REFRESH_COOLDOWN_SECONDS:
        cached_data = load_cached_models()
        if cached_data:
            return cached_data

    logger.info("🔄 [MODEL REFRESH] Querying Google GenAI API for active Gemini models...")
    discovered_models = discover_api_models(api_key)
    task_ratings = calculate_task_matrix(discovered_models)

    cache_payload = {
        "schema_version": "1.0",
        "updated_at": now,
        "models": discovered_models,
        "task_ratings": task_ratings
    }

    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp_file = CACHE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cache_payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, CACHE_FILE)
        _LAST_REFRESH_TIME = now
        logger.info(f"💾 Saved refreshed Gemini model matrix ({len(discovered_models)} models) to {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save models cache: {e}")

    return cache_payload


def load_cached_models() -> Optional[dict]:
    """Loads model cache from storage/gemini_models_cache.json if valid."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "models" in data and "task_ratings" in data:
                return data
    except Exception as e:
        logger.warning(f"Failed to read models cache {CACHE_FILE}: {e}")
    return None


def get_active_models_and_ratings() -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    """
    Returns active models list and task ratings matrix.
    Loads from cache if available, or initiates API discovery.
    """
    cached = load_cached_models()
    if cached:
        return cached.get("models", DEFAULT_MODELS_LIST), cached.get("task_ratings", DEFAULT_TASK_MODEL_RATINGS)
    
    refreshed = refresh_gemini_models_cache()
    return refreshed.get("models", DEFAULT_MODELS_LIST), refreshed.get("task_ratings", DEFAULT_TASK_MODEL_RATINGS)
