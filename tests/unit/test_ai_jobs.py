"""Media AI analysis through the background queue (app/jobs.py + worker):
queued at once, run by the worker, failures recorded, inline without a queue."""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import jobs
from app.services import documents as documents_service
from tests.unit.test_ai_media import MediaAITestCase


class QueuedAnalysisTests(MediaAITestCase):
    def setUp(self):
        super().setUp()
        self.queued = []
        self.queue_works = True

        def enqueue(function_path, *args, job_id):
            if self.queue_works:
                self.queued.append((function_path, args, job_id))
            return self.queue_works

        patcher = patch.object(jobs, "enqueue", enqueue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.image = self.upload("figure1.png", b"\x89PNG fake image bytes")

    def run_worker(self):
        """What app/worker.py does with the queued job."""
        function_path, args, _ = self.queued.pop(0)
        self.assertEqual(function_path, "app.services.documents.run_ai_analysis")
        documents_service.run_ai_analysis(*args)

    def details(self):
        return self.backend.details[self.image["id"]]

    def test_click_returns_at_once_with_the_job_queued(self):
        response = self.analyse(self.image["id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ai_job_pending"])
        self.assertEqual(self.details()["ai_job"]["status"], "queued")
        self.assertEqual(self.gemini.generated, [])  # nothing sent to Gemini yet
        self.assertEqual(len(self.queued), 1)

    def test_worker_saves_the_text_and_clears_the_job(self):
        self.analyse(self.image["id"])
        self.run_worker()
        self.assertIn("Workload forecasting", self.details()["extracted_text"]["text"])
        self.assertNotIn("ai_job", self.details())
        self.assertFalse(self.client.get(f"/documents/{self.image['id']}").json()["ai_job_pending"])

    def test_second_click_while_pending_is_409(self):
        self.analyse(self.image["id"])
        self.assertEqual(self.analyse(self.image["id"]).status_code, 409)
        self.assertEqual(len(self.queued), 1)

    def test_failed_job_is_recorded_and_shown(self):
        self.analyse(self.image["id"])
        with patch.dict(os.environ, {"AI_MEDIA_ANALYSIS": "false"}):
            self.run_worker()
        job = self.details()["ai_job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("AI_MEDIA_ANALYSIS=false", job["error"])
        page = self.client.get(f"/ui/documents/{self.image['id']}").text
        self.assertIn("Lần phân tích trước thất bại", page)
        self.assertEqual(self.analyse(self.image["id"]).status_code, 200)  # retry allowed

    def test_page_shows_the_pending_notice_and_disables_the_button(self):
        self.analyse(self.image["id"])
        page = self.client.get(f"/ui/documents/{self.image['id']}").text
        self.assertIn("data-ai-job-pending", page)
        self.assertIn("Đang phân tích…", page)

    def test_without_a_queue_the_analysis_runs_inline(self):
        self.queue_works = False
        response = self.analyse(self.image["id"])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ai_job_pending"])
        self.assertIn("Workload forecasting", self.details()["extracted_text"]["text"])
        self.assertNotIn("ai_job", self.details())

    def test_stale_job_no_longer_blocks_a_retry(self):
        self.analyse(self.image["id"])
        old = datetime.now(timezone.utc) - timedelta(seconds=jobs.JOB_TIMEOUT_SECONDS + 600)
        self.details()["ai_job"]["queued_at"] = old.isoformat()  # worker died mid-job
        self.assertFalse(self.client.get(f"/documents/{self.image['id']}").json()["ai_job_pending"])
        self.assertEqual(self.analyse(self.image["id"]).status_code, 200)

    def test_superseded_job_does_nothing(self):
        self.analyse(self.image["id"])
        self.details()["ai_job"]["job_id"] = "a-newer-job"
        self.run_worker()
        self.assertEqual(self.gemini.generated, [])
        self.assertEqual(self.details()["ai_job"]["status"], "queued")

    def test_local_text_extraction_is_never_queued(self):
        text_doc = self.upload("notes.txt", b"plain text")
        response = self.analyse(text_doc["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.queued, [])
