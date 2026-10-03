"""Admin "Thống kê AI" page: turns the raw call log (app/repositories/usage.py)
into the totals, per-model/source breakdown and time-bucketed chart the page
shows. Pure functions over a plain list of dicts, deliberately kept out of
the repository layer so this is trivial to unit test without a real or faked
Mongo aggregation pipeline.
"""
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app.repositories import usage as repository

log = logging.getLogger("uvicorn.error")

# range name -> (days of history, chart bucket size, label). The bucket is
# coarser than the range so the chart stays readable: a year of per-day bars
# would be unreadable, a day of per-month bars would be a single bar.
RANGES = OrderedDict([
    ("day", {"days": 1, "bucket": "hour", "label": "24 giờ qua"}),
    ("week", {"days": 7, "bucket": "day", "label": "7 ngày qua"}),
    ("month", {"days": 30, "bucket": "day", "label": "30 ngày qua"}),
    ("year", {"days": 365, "bucket": "month", "label": "12 tháng qua"}),
])
DEFAULT_RANGE = "week"


def bucket_key(bucket, when):
    if bucket == "hour":
        return when.strftime("%d/%m %Hh")
    if bucket == "day":
        return when.strftime("%d/%m")
    if bucket == "month":
        return when.strftime("%m/%Y")
    raise ValueError(f"Unknown bucket: {bucket}")


def _empty_totals():
    return {"calls": 0, "ok": 0, "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}


def group_by_bucket(rows, bucket):
    """[(label, totals), ...] in chronological order — the chart's bars."""
    buckets = OrderedDict()
    for row in rows:
        entry = buckets.setdefault(bucket_key(bucket, row["created_at"]), _empty_totals())
        entry["calls"] += 1
        entry["ok"] += 1 if row.get("ok") else 0
        entry["input_tokens"] += row.get("input_tokens") or 0
        entry["output_tokens"] += row.get("output_tokens") or 0
        entry["thinking_tokens"] += row.get("thinking_tokens") or 0
    return list(buckets.items())


def _group_by(rows, field):
    groups = OrderedDict()
    for row in rows:
        key = row.get(field) or "?"
        entry = groups.setdefault(key, {"calls": 0, "ok": 0})
        entry["calls"] += 1
        entry["ok"] += 1 if row.get("ok") else 0
    return sorted(groups.items(), key=lambda kv: kv[1]["calls"], reverse=True)


def totals(rows):
    """Grand totals plus the per-model and per-source breakdowns — the stat
    cards and tables above the chart."""
    result = _empty_totals()
    latencies = []
    for row in rows:
        result["ok"] += 1 if row.get("ok") else 0
        result["calls"] += 1
        result["input_tokens"] += row.get("input_tokens") or 0
        result["output_tokens"] += row.get("output_tokens") or 0
        result["thinking_tokens"] += row.get("thinking_tokens") or 0
        if row.get("latency_ms") is not None:
            latencies.append(row["latency_ms"])
    result["failed"] = result["calls"] - result["ok"]
    result["avg_latency_ms"] = round(sum(latencies) / len(latencies)) if latencies else None
    result["by_model"] = _group_by(rows, "model")
    result["by_source"] = _group_by(rows, "source")
    return result


def report(range_name):
    """Everything the admin stats page needs for one range tab. Best-effort:
    an unreadable call log shows as an all-zero report instead of breaking
    the page — usage stats are a diagnostic, never load-bearing."""
    config = RANGES.get(range_name, RANGES[DEFAULT_RANGE])
    since = datetime.now(timezone.utc) - timedelta(days=config["days"])
    try:
        rows = repository.list_since(since)
    except Exception as exc:
        log.warning("usage_stats_read_failed error=%s", type(exc).__name__)
        rows = []
    return {
        "range": range_name if range_name in RANGES else DEFAULT_RANGE,
        "label": config["label"],
        "totals": totals(rows),
        "buckets": group_by_bucket(rows, config["bucket"]),
    }
