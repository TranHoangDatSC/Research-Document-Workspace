"""app/services/usage_stats.py: pure-Python aggregation over the raw call
log (app/repositories/usage.py) for the admin "Thống kê AI" page. Kept out
of the repository layer specifically so this is testable over plain lists,
no real or faked Mongo aggregation pipeline needed.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import usage_stats


def _row(when, ok=True, model="gemini-flash-latest", source="ask", input_tokens=100, output_tokens=50, latency_ms=500):
    return {
        "created_at": when, "ok": ok, "model": model, "source": source,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "thinking_tokens": 0,
        "latency_ms": latency_ms,
    }


class BucketKeyTests(unittest.TestCase):
    def test_hour_bucket_label(self):
        self.assertEqual(usage_stats.bucket_key("hour", datetime(2026, 3, 5, 14, 30, tzinfo=timezone.utc)), "05/03 14h")

    def test_day_bucket_label(self):
        self.assertEqual(usage_stats.bucket_key("day", datetime(2026, 3, 5, 14, 30, tzinfo=timezone.utc)), "05/03")

    def test_month_bucket_label(self):
        self.assertEqual(usage_stats.bucket_key("month", datetime(2026, 3, 5, tzinfo=timezone.utc)), "03/2026")

    def test_unknown_bucket_raises(self):
        with self.assertRaises(ValueError):
            usage_stats.bucket_key("century", datetime.now(timezone.utc))


class GroupByBucketTests(unittest.TestCase):
    def test_groups_and_sums_tokens_per_bucket_in_order(self):
        rows = [
            _row(datetime(2026, 3, 2, tzinfo=timezone.utc), input_tokens=20, output_tokens=2),
            _row(datetime(2026, 3, 1, tzinfo=timezone.utc), input_tokens=100, output_tokens=10),
            _row(datetime(2026, 3, 1, tzinfo=timezone.utc), input_tokens=50, output_tokens=5),
        ]
        buckets = usage_stats.group_by_bucket(rows, "day")
        self.assertEqual([label for label, _ in buckets], ["02/03", "01/03"])  # insertion order, not sorted
        day1 = dict(buckets)["01/03"]
        self.assertEqual(day1, {"calls": 2, "ok": 2, "input_tokens": 150, "output_tokens": 15, "thinking_tokens": 0})

    def test_failed_calls_are_counted_but_not_ok(self):
        buckets = usage_stats.group_by_bucket([_row(datetime(2026, 3, 1, tzinfo=timezone.utc), ok=False)], "day")
        self.assertEqual(buckets[0][1]["calls"], 1)
        self.assertEqual(buckets[0][1]["ok"], 0)

    def test_empty_rows_is_empty_list(self):
        self.assertEqual(usage_stats.group_by_bucket([], "day"), [])


class TotalsTests(unittest.TestCase):
    def test_grand_totals_and_failed_count(self):
        rows = [
            _row(datetime(2026, 3, 1, tzinfo=timezone.utc), ok=True, model="model-a", source="ask"),
            _row(datetime(2026, 3, 1, tzinfo=timezone.utc), ok=False, model="model-a", source="ask"),
            _row(datetime(2026, 3, 1, tzinfo=timezone.utc), ok=True, model="model-b", source="graph"),
        ]
        result = usage_stats.totals(rows)
        self.assertEqual((result["calls"], result["ok"], result["failed"]), (3, 2, 1))
        self.assertEqual(dict(result["by_model"])["model-a"], {"calls": 2, "ok": 1})
        self.assertEqual(dict(result["by_source"])["graph"], {"calls": 1, "ok": 1})

    def test_by_model_sorted_by_call_count_descending(self):
        rows = [_row(datetime.now(timezone.utc), model="rare")] + [_row(datetime.now(timezone.utc), model="common")] * 3
        result = usage_stats.totals(rows)
        self.assertEqual(result["by_model"][0][0], "common")

    def test_avg_latency_ignores_missing_values(self):
        rows = [_row(datetime(2026, 3, 1, tzinfo=timezone.utc), latency_ms=None)]
        self.assertIsNone(usage_stats.totals(rows)["avg_latency_ms"])

    def test_empty_rows_is_all_zero(self):
        result = usage_stats.totals([])
        self.assertEqual((result["calls"], result["ok"], result["failed"], result["avg_latency_ms"]), (0, 0, 0, None))


class ReportTests(unittest.TestCase):
    def test_unknown_range_falls_back_to_default(self):
        with patch.object(usage_stats.repository, "list_since", return_value=[]) as mock_list:
            result = usage_stats.report("not-a-real-range")
        self.assertEqual(result["range"], usage_stats.DEFAULT_RANGE)
        mock_list.assert_called_once()

    def test_storage_failure_reads_as_an_empty_report_not_an_error(self):
        with patch.object(usage_stats.repository, "list_since", side_effect=RuntimeError("down")):
            result = usage_stats.report("week")
        self.assertEqual(result["totals"]["calls"], 0)
        self.assertEqual(result["buckets"], [])

    def test_every_range_is_a_valid_bucket_config(self):
        for name in usage_stats.RANGES:
            with patch.object(usage_stats.repository, "list_since", return_value=[]):
                result = usage_stats.report(name)
            self.assertEqual(result["range"], name)
