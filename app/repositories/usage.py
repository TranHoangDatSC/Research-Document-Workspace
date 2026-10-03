"""Raw LLM call log: one row per (key, model) attempt made in llm.ask() or
media_ai.analyze(), success or failure — a failed attempt still counts,
since a page full of 429/503 is exactly the "quota exhausted" signal the
admin stats page (app/services/usage_stats.py) exists to surface.

Collection: llm_usage. Aggregation (by day/week/month/year, by model, totals)
is done in plain Python over the list this returns, not a Mongo pipeline —
small, self-hosted scale, and far easier to unit test without a real or
faked aggregation engine.
"""
import os
from datetime import datetime, timezone

from app.storage import mongo_client

COLLECTION = "llm_usage"


def record(source, provider, model, ok, latency_ms, usage=None, error=None):
    """`source`: "ask" | "graph" | "media_ai" — which feature made the call,
    so the stats page can break down usage by *why* quota is being spent,
    not just by model."""
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
