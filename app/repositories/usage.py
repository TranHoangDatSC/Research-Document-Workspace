"""LLM call log in MongoDB (`llm_usage`): one row per (key, model) attempt,
failed ones included, since repeated 429/503 is the "quota exhausted" signal.
Totals are computed in Python by services/usage_stats.py.
"""
import os
from datetime import datetime, timezone

from app.storage import mongo_client

COLLECTION = "llm_usage"


def record(source, provider, model, ok, latency_ms, usage=None, error=None):
    """`source`: "ask" | "graph" | "media_ai", the feature that made the call."""
    doc = {
        "created_at": datetime.now(timezone.utc),
        "source": source,
        "provider": provider,
        "model": model,
        "ok": bool(ok),
        "latency_ms": latency_ms,
        "input_tokens": (usage or {}).get("input"),
        "output_tokens": (usage or {}).get("output"),
        "thinking_tokens": (usage or {}).get("thinking"),
        "cached_tokens": (usage or {}).get("cached"),
        "error": (error or None) and str(error)[:300],
    }
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]][COLLECTION].insert_one(doc)


def list_since(since):
    with mongo_client() as client:
        cursor = client[os.environ["MONGO_DB"]][COLLECTION].find(
            {"created_at": {"$gte": since}}, {"_id": 0}
        ).sort("created_at", 1)
        return list(cursor)
