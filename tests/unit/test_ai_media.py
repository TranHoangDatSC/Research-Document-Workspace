"""AI reading images, audio and video (app/media_ai.py): the file is streamed
to the Gemini Files API, read by the model, saved as text and the uploaded
copy deleted. FakeGemini speaks the same REST protocol — no network, no key.
"""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch
from uuid import UUID

from support import FakeBackend

from app import llm, media_ai


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"{}", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGemini:
    """Files API + generateContent. Records what it received."""

    def __init__(self, answer="## Chữ trong ảnh\nWorkload forecasting\n## Mô tả\nBiểu đồ cột", processing_polls=0):
        self.answer = answer
        self.processing_polls = processing_polls
        self.uploads = {}       # upload url -> {"key", "mime", "size", "data", "chunks"}
        self.files = {}         # name -> file resource
        self.generated = []     # (key, model, payload)
        self.deleted = []
        self.fail_generate = {}  # (key, model) -> HTTP status

    def __call__(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        key = url.split("key=")[1] if "key=" in url else None
        headers = {k.lower(): v for k, v in request.header_items()}
        if "/upload/v1beta/files" in url:
            upload_url = f"https://upload.example/{len(self.uploads)}"
            self.uploads[upload_url] = {
                "key": key, "mime": headers["x-goog-upload-header-content-type"],
                "size": int(headers["x-goog-upload-header-content-length"]), "data": b"", "chunks": [],
            }
            return FakeResponse(headers={"X-Goog-Upload-URL": upload_url})
        if url.startswith("https://upload.example/"):
            upload = self.uploads[url]
            assert int(headers["x-goog-upload-offset"]) == len(upload["data"])
            upload["data"] += request.data
            upload["chunks"].append(headers["x-goog-upload-command"])
            if "finalize" not in headers["x-goog-upload-command"]:
                return FakeResponse(b"")
            name = f"files/f{len(self.files)}"
            self.files[name] = {"name": name, "uri": f"https://generativelanguage.googleapis.com/v1beta/{name}",
                                "mimeType": upload["mime"], "state": "PROCESSING" if self.processing_polls else "ACTIVE",
                                "key": key}
            return FakeResponse(json.dumps({"file": self.files[name]}).encode())
        if ":generateContent" in url:
            model = url.split("/models/")[1].split(":")[0]
            if (key, model) in self.fail_generate:
                raise urllib.error.HTTPError(url, self.fail_generate[(key, model)], "err", {}, io.BytesIO(b"quota"))
            self.generated.append((key, model, json.loads(request.data)))
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [{"text": self.answer}]}}]}).encode())
        name = url.split("/v1beta/")[1].split("?")[0]
        if method == "DELETE":
            self.deleted.append(name)
            return FakeResponse()
        if self.processing_polls:
            self.processing_polls -= 1
        else:
            self.files[name]["state"] = "ACTIVE"
        return FakeResponse(json.dumps(self.files[name]).encode())


class MediaAITestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.client = self.backend.client(self)
        self.project_id = self.client.post("/projects", json={"name": "Media"}).json()["id"]
        env = patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "LLM_API_KEYS": "key-1", "LLM_MODELS": "model-a",
                                      "AI_MEDIA_ANALYSIS": "true", "AI_MEDIA_MAX_MB": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LLM_API_KEY", None)
        self.gemini = FakeGemini()
        for target, value in (("urllib.request.urlopen", self.gemini), ("app.media_ai.time.sleep", lambda s: None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, name, content):
        return self.client.post(f"/projects/{self.project_id}/documents", files={"file": (name, content)}).json()

    def analyse(self, document_id):
        return self.client.post(f"/documents/{document_id}/extract")


class AnalyseMediaTests(MediaAITestCase):
    def test_image_is_read_by_gemini_and_saved_as_text(self):
        image = self.upload("figure1.png", b"\x89PNG fake image bytes")
        response = self.analyse(image["id"])
        self.assertEqual(response.status_code, 200, response.text)
        extracted = self.backend.details[image["id"]]["extracted_text"]
        self.assertEqual((extracted["method"], extracted["model"]), ("gemini_image", "model-a"))
        self.assertIn("Workload forecasting", extracted["text"])
        # Gemini got the exact bytes, as a file reference + the image prompt.
        (upload,) = self.gemini.uploads.values()
        self.assertEqual((upload["data"], upload["mime"]), (b"\x89PNG fake image bytes", "image/png"))
        (_, model, payload) = self.gemini.generated[0]
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0]["file_data"]["mime_type"], "image/png")
        self.assertIn("Chép lại NGUYÊN VĂN", parts[1]["text"])
        self.assertEqual(self.gemini.deleted, ["files/f0"])  # the copy at Google is removed

    def test_video_streams_in_chunks_and_waits_for_processing(self):
        video_bytes = bytes(range(256)) * 40  # 10 KiB
        video = self.upload("talk.mp4", video_bytes)
        self.gemini.processing_polls = 2
        with patch.object(media_ai, "UPLOAD_CHUNK", 4096):
            self.assertEqual(self.analyse(video["id"]).status_code, 200)
        (upload,) = self.gemini.uploads.values()
        self.assertEqual(upload["data"], video_bytes)
        self.assertEqual(upload["chunks"], ["upload", "upload", "upload, finalize"])  # never the whole file at once
        self.assertIn("[mm:ss]", self.gemini.generated[0][2]["contents"][0]["parts"][1]["text"])
        self.assertEqual(self.backend.details[video["id"]]["extracted_text"]["method"], "gemini_video")

    def test_rate_limited_key_falls_back_to_the_next_key(self):
        os.environ["LLM_API_KEYS"] = "key-1,key-2"
        self.gemini.fail_generate[("key-1", "model-a")] = 429
        audio = self.upload("interview.mp3", b"ID3 audio")
        self.assertEqual(self.analyse(audio["id"]).status_code, 200)
        self.assertEqual([u["key"] for u in self.gemini.uploads.values()], ["key-1", "key-2"])  # re-uploaded under key-2
        self.assertEqual(sorted(self.gemini.deleted), ["files/f0", "files/f1"])  # both copies cleaned up
        self.assertEqual(self.gemini.generated[0][0], "key-2")

    def test_nothing_is_sent_when_turned_off_or_not_gemini_or_too_big(self):
        image = self.upload("photo.png", b"x" * (1024 * 1024 + 1))
        for env, message in (
            ({"AI_MEDIA_ANALYSIS": "false"}, "đang tắt"),
            ({"LLM_PROVIDER": "openai"}, "LLM_PROVIDER=gemini"),
            ({"AI_MEDIA_MAX_MB": "1"}, "quá 1 MiB"),
        ):
            with self.subTest(env=env), patch.dict(os.environ, env):
                response = self.analyse(image["id"])
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.json()["detail"])
        self.assertEqual(self.gemini.uploads, {})
        self.assertEqual(self.backend.downloads, [])  # not even read from MinIO

    def test_rejected_file_is_422_and_saves_nothing(self):
        self.gemini.fail_generate[("key-1", "model-a")] = 400
        image = self.upload("photo.png", b"x")
        response = self.analyse(image["id"])
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.backend.details[image["id"]]["extracted_text"])
        self.assertEqual(self.gemini.deleted, ["files/f0"])

    def test_analysed_image_is_used_in_ai_answers(self):
        image = self.upload("figure1.png", b"png")
        self.analyse(image["id"])
        with patch.object(llm, "ask", lambda prompt, **kw: ("Biểu đồ cho thấy [1]", "m")) as _:
            answer = self.client.post(f"/projects/{self.project_id}/ask", json={"question": "Hình 1 nói gì?"}).json()
        self.assertEqual(answer["sources"][0]["original_name"], "figure1.png")
        self.assertEqual(answer["unread"], [])


class UnreadDocumentsTests(MediaAITestCase):
    def test_answer_names_the_selected_files_the_ai_could_not_read(self):
        self.upload("notes.txt", b"ARIMA beats naive forecasts")
        video = self.upload("demo.mp4", b"video")
        with patch.object(llm, "ask", lambda prompt, **kw: ("ok [1]", "m")):
            answer = self.client.post(f"/projects/{self.project_id}/ask", json={"question": "q"}).json()
        self.assertEqual(answer["unread"], ["demo.mp4"])
        self.assertIsNone(self.backend.details[video["id"]]["extracted_text"])

    def test_only_unread_files_selected(self):
        video = self.upload("demo.mp4", b"video")
        response = self.client.post(f"/projects/{self.project_id}/ask", json={"question": "q", "document_ids": [video["id"]]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Phân tích bằng AI", response.json()["detail"])

    def test_overview_counts_what_the_ai_can_read(self):
        self.upload("notes.txt", b"text")
        image = self.upload("photo.png", b"png")
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn('<strong>1<span class="muted"> / 2</span></strong><small>tệp AI đã đọc được', html)
        self.analyse(image["id"])
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn('<strong>2<span class="muted"> / 2</span></strong>', html)
        self.assertTrue(UUID(image["id"]))


if __name__ == "__main__":
    unittest.main()
