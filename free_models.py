#!/usr/bin/env python3
"""
free_models.py — catalog of all OpenRouter free models + helper functions.

Source: https://openrouter.ai/api/v1/models (filtered pricing.prompt=0 AND pricing.completion=0)
Updated: 2026-09-08
"""
from typing import List, Dict, Optional

# All verified free models from OpenRouter (as of 2026-09-08)
FREE_MODELS: List[Dict] = [
    # ── 1M+ context ──────────────────────────────────────────────
    {"id": "thinkingmachines/inkling-small:free",  "name": "Inkling Small",  "ctx": 1048576, "max_out": 262144, "provider": "thinkingmachines", "notes": "12B active MoE, multimodal"},
    {"id": "thinkingmachines/inkling:free",        "name": "Inkling",        "ctx": 1048576, "max_out": 262144, "provider": "thinkingmachines", "notes": "41B active MoE, multimodal"},
    {"id": "nvidia/nemotron-3.5-lightning:free",   "name": "Nemotron 3.5 Lightning", "ctx": 1000000, "max_out": 65536, "provider": "nvidia", "notes": "3B active / 30B total MoE"},
    {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "name": "Nemotron 3 Ultra", "ctx": 1000000, "max_out": 65536, "provider": "nvidia", "notes": "55B active, frontier reasoning"},
    # ── 512K context ──────────────────────────────────────────────
    {"id": "dots-studio/dots-3-note-preview:free", "name": "Dots3-Note Preview", "ctx": 512000, "max_out": 460800, "provider": "dots-studio", "notes": "16B active / 280B total MoE"},
    # ── 262K context ──────────────────────────────────────────────
    {"id": "inclusionai/ling-3.0-flash-sante:free", "name": "Ling 3.0 Flash Sante", "ctx": 262144, "max_out": 32768, "provider": "inclusionai", "notes": "Health/medicine focused"},
    {"id": "inclusionai/ling-3.0-flash-fin:free",   "name": "Ling 3.0 Flash Fin",   "ctx": 262144, "max_out": 32768, "provider": "inclusionai", "notes": "Finance focused, 5.1B active"},
    {"id": "poolside/laguna-s-2.1:free",            "name": "Laguna S 2.1",        "ctx": 262144, "max_out": 32768, "provider": "poolside", "notes": "118B total, coding agent"},
    {"id": "poolside/laguna-xs-2.1:free",           "name": "Laguna XS 2.1",       "ctx": 262144, "max_out": 32768, "provider": "poolside", "notes": "33B-A3B coding agent"},
    {"id": "google/gemma-4-26b-a4b-it:free",        "name": "Gemma 4 26B A4B",     "ctx": 262144, "max_out": 32768, "provider": "google", "notes": "25.2B total MoE, instruction-tuned"},
    {"id": "google/gemma-4-31b-it:free",            "name": "Gemma 4 31B",         "ctx": 262144, "max_out": 32768, "provider": "google", "notes": "30.7B dense multimodal"},
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 3 Super",   "ctx": 262144, "max_out": 235929, "provider": "nvidia", "notes": "120B total, 12B active hybrid MoE"},
    # ── 256K context ──────────────────────────────────────────────
    {"id": "cohere/north-mini-code:free",           "name": "North Mini Code",     "ctx": 256000, "max_out": 64000, "provider": "cohere", "notes": "Agentic coding MoE"},
    {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "name": "Nemotron 3 Nano Omni", "ctx": 256000, "max_out": 65536, "provider": "nvidia", "notes": "30B-A3B multimodal reasoning"},
    # ── Router + specialized ─────────────────────────────────────
    {"id": "openrouter/free",                       "name": "Free Models Router",  "ctx": 200000, "max_out": 0,     "provider": "openrouter", "notes": "Random free model router"},
    {"id": "nvidia/nemotron-3.5-content-safety:free", "name": "Nemotron 3.5 Safety", "ctx": 128000, "max_out": 8192, "provider": "nvidia", "notes": "4B guardrail model"},
    {"id": "liquid/lfm-2.5-2.6b:free",              "name": "LFM2.5-2.6B",         "ctx": 65536,  "max_out": 8192,  "provider": "liquid", "notes": "Compact reasoning, agent workflows"},
]

# Models suitable for trading signal scoring (text-in, JSON-out, >= 128K context)
SCORING_MODELS: List[Dict] = [
    m for m in FREE_MODELS
    if m["id"] not in ("google/lyria-3-pro-preview", "google/lyria-3-clip-preview",
                       "nvidia/nemotron-3.5-content-safety:free",
                       "liquid/lfm-2.5-2.6b:free", "openrouter/free")
    and m["max_out"] >= 8192
]

# Default scoring model (best balance of quality + context + output)
DEFAULT_SCORING_MODEL = "nvidia/nemotron-3.5-lightning:free"

# Fallback chain for scoring (tried in order)
SCORING_FALLBACK_CHAIN: List[str] = [
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "thinkingmachines/inkling-small:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
]


def get_model(model_id: str) -> Optional[Dict]:
    """Get a free model by its full id (e.g. 'nvidia/nemotron-3.5-lightning:free')."""
    for m in FREE_MODELS:
        if m["id"] == model_id:
            return m
    return None


def get_models_by_provider(provider: str) -> List[Dict]:
    """Get all free models from a specific provider."""
    return [m for m in FREE_MODELS if m["provider"] == provider]


def get_models_by_min_context(min_ctx: int) -> List[Dict]:
    """Get all free models with at least min_ctx context length."""
    return [m for m in FREE_MODELS if m["ctx"] >= min_ctx]


def list_all_ids() -> List[str]:
    """Return list of all free model ids."""
    return [m["id"] for m in FREE_MODELS]


if __name__ == "__main__":
    print(f"Total free models: {len(FREE_MODELS)}")
    print(f"Scoring-suitable models: {len(SCORING_MODELS)}")
    print()
    for m in FREE_MODELS:
        print(f"  {m['id']:55s} ctx={m['ctx']:7d} max={m['max_out']:6d} {m['name']}")