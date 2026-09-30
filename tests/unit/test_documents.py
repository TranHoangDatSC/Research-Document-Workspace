"""Document management: upload, view/download, edit metadata, delete — across
PostgreSQL (row), MongoDB (metadata) and MinIO (file), including what happens
when one of those stores fails halfway. Storage replaced by support.py.
"""
import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from support import FakeBackend, upload

MIB = 1024 * 1024


class DocumentTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.client = self.backend.client(self)
        response = self.client.post("/projects", json={"name": "Cloud project"})
        self.project_id = response.json()["id"]

    def upload(self, **kwargs):
        return upload(self.client, self.project_id, **kwargs)

    def post_file(self, name, content, **data):
        return self.client.post(f"/ui/projects/{self.project_id}/documents", files={"file": (name, content)}, data=data)


class UploadDocumentTests(DocumentTestCase):
    def test_upload_stores_row_metadata_and_file(self):
        document = self.upload(tags="cloud, docker, cloud", authors="Dat")
        document_id = UUID(document["id"])
        self.assertEqual(self.backend.documents[document_id]["status"], "ready")
        self.assertEqual(self.backend.objects[document["object_name"]], b"original bytes")
        self.assertEqual(document["sha256"], hashlib.sha256(b"original bytes").hexdigest())
        self.assertEqual(document["tags"], ["cloud", "docker"])  # trimmed and de-duplicated
        self.assertEqual(document["authors"], ["Dat"])
        self.assertEqual(document["custom_metadata"], {"year": 2026})
        self.assertIsNone(document["extracted_text"])

    def test_api_upload_returns_201(self):
        response = self.client.post(
            f"/projects/{self.project_id}/documents", files={"file": ("paper.pdf", b"%PDF-1.4 fake")},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["content_type"], "application/pdf")

    def test_rejects_bad_files_and_metadata(self):
        cases = [
            ("bad.exe", b"x", {}, 415),                                  # extension not allowed
            ("noextension", b"x", {}, 415),
            ("big.txt", b"x" * (MIB + 1), {}, 413),                      # over the (lowered) document limit
            ("empty.txt", b"", {}, 422),                                 # empty file
            ("ok.txt", b"x", {"custom_metadata": "[1, 2]"}, 422),         # metadata not an object
            ("ok.txt", b"x", {"custom_metadata": "{broken"}, 422),        # metadata not JSON
            ("ok.txt", b"x", {"tags": ",".join(f"t{i}" for i in range(51))}, 422),  # > 50 tags
        ]
        with patch.dict(os.environ, {"MAX_UPLOAD_MB_DOCUMENT": "1"}):
            for name, content, data, status in cases:
                with self.subTest(name=name, data=data):
                    self.assertEqual(self.post_file(name, content, **data).status_code, status)
        self.assertEqual(self.client.post(f"/ui/projects/{self.project_id}/documents").status_code, 422)  # no file
        self.assertEqual(self.backend.documents, {})
        self.assertEqual(self.backend.objects, {})

    def test_upload_to_unknown_project(self):
        response = self.client.post(f"/projects/{uuid4()}/documents", files={"file": ("a.txt", b"x")})
        self.assertEqual(response.status_code, 404)

    def test_metadata_failure_rolls_back_the_file(self):
        self.backend.fail.add("mongo")
        self.assertEqual(self.post_file("paper.txt", b"content").status_code, 503)
        (row,) = self.backend.documents.values()
        self.assertEqual(row["status"], "failed")  # never shown as a usable document
        self.assertEqual(self.backend.objects, {})  # uploaded file removed again


class UploadFileKindsTests(DocumentTestCase):
    """Beyond text documents: slides, data, images, audio, video, archives."""

    def test_every_kind_is_stored_with_its_type(self):
        for name, content_type in (
            ("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ("notes.md", "text/markdown"),
            ("table.csv", "text/csv"),
            ("photo.JPG", "image/jpeg"),
            ("talk.mp3", "audio/mpeg"),
            ("demo.mp4", "video/mp4"),
            ("dataset.zip", "application/zip"),
        ):
            with self.subTest(name=name):
                response = self.client.post(f"/projects/{self.project_id}/documents", files={"file": (name, b"bytes of " + name.encode())})
                self.assertEqual(response.status_code, 201, response.text)
                document = response.json()
                self.assertEqual(document["content_type"], content_type)
                self.assertEqual(self.backend.content_types[document["object_name"]], content_type)
                self.assertEqual(document["sha256"], hashlib.sha256(b"bytes of " + name.encode()).hexdigest())

    def test_each_kind_has_its_own_size_limit(self):
        with patch.dict(os.environ, {"MAX_UPLOAD_MB_IMAGE": "1"}):
            too_big_image = self.post_file("photo.png", b"x" * (MIB + 1))
            same_size_video = self.post_file("clip.mp4", b"x" * (MIB + 1))
        self.assertEqual(too_big_image.status_code, 413)
        self.assertIn("Hình ảnh tối đa 1 MiB", too_big_image.text)
        self.assertEqual(same_size_video.status_code, 200)  # video limit is far higher

    def test_upload_form_offers_all_kinds_with_limits(self):
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        for fragment in (".pptx", ".mp4", ".png", ".zip", "Video", "500 MiB"):
            self.assertIn(fragment, html)


class ViewDocumentTests(DocumentTestCase):
    def test_api_get_merges_row_and_metadata(self):
        document = self.upload()
        response = self.client.get(f"/documents/{document['id']}").json()
        self.assertEqual(response["original_name"], "paper.txt")  # PostgreSQL
        self.assertEqual(response["tags"], ["cloud", "docker"])   # MongoDB

    def test_list_documents_of_project(self):
        self.upload(name="first.txt")
        self.upload(name="second.txt")
        names = [d["original_name"] for d in self.client.get(f"/projects/{self.project_id}/documents").json()]
        self.assertEqual(names, ["second.txt", "first.txt"])

    def test_download_returns_original_bytes(self):
        document = self.upload(name="báo cáo.txt")
        response = self.client.get(f"/documents/{document['id']}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), document["sha256"])
        self.assertIn("b%C3%A1o%20c%C3%A1o.txt", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_detail_page(self):
        document = self.upload()
        html = self.client.get(f"/ui/documents/{document['id']}").text
        self.assertIn("paper.txt", html)
        self.assertIn("docker", html)

    def test_errors_are_html_for_pages_and_json_for_api(self):
        for path, status in (("/ui/documents/not-a-uuid", 422), (f"/ui/documents/{uuid4()}", 404)):
            response = self.client.get(path)
            self.assertEqual(response.status_code, status)
            self.assertIn("text/html", response.headers["content-type"])
        response = self.client.get("/documents/not-a-uuid")
        self.assertEqual(response.status_code, 422)
        self.assertIn("application/json", response.headers["content-type"])

    def test_metadata_store_down_gives_clean_503_page(self):
        document = self.upload()
        self.backend.fail.add("mongo")
        response = self.client.get(f"/ui/documents/{document['id']}")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Traceback", response.text)

    def test_pending_document_is_not_readable(self):
        document = self.upload()
        self.backend.documents[UUID(document["id"])]["status"] = "pending"
        self.assertEqual(self.client.get(f"/documents/{document['id']}").status_code, 409)
        self.assertEqual(self.client.get(f"/documents/{document['id']}/download").status_code, 409)


class SearchAndFilterTests(DocumentTestCase):
    """Sidebar search by name and filter by kind — server-side, across all pages."""

    def setUp(self):
        super().setUp()
        for name in ("Forecasting survey.pdf", "cloud_notes.txt", "Forecast chart.png", "demo.mp4"):
            self.post_file(name, b"x")

    def names(self, **params):
        return [d["original_name"] for d in self.client.get(f"/projects/{self.project_id}/documents", params=params).json()]

    def test_api_search_is_case_insensitive_substring(self):
        self.assertEqual(sorted(self.names(q="forecast")), ["Forecast chart.png", "Forecasting survey.pdf"])

    def test_api_filter_by_kind_and_combined(self):
        self.assertEqual(self.names(kind="video"), ["demo.mp4"])
        self.assertEqual(self.names(q="forecast", kind="image"), ["Forecast chart.png"])
        self.assertEqual(self.client.get(f"/projects/{self.project_id}/documents", params={"kind": "spaceship"}).status_code, 422)

    def test_like_wildcards_in_the_query_are_literal(self):
        from app.repositories.documents import _escape_like
        self.assertEqual(_escape_like("100%_done\\"), "100\\%\\_done\\\\")

    def test_sidebar_shows_filtered_list_and_keeps_it_on_links(self):
        html = self.client.get(f"/ui/projects/{self.project_id}", params={"q": "forecast", "kind": "image"}).text
        sidebar = html.split('id="source-results"', 1)[1].split("</nav>", 1)[0]
        self.assertIn("Forecast chart.png", sidebar)
        self.assertNotIn("Forecasting survey.pdf", sidebar)
        self.assertIn("1 kết quả cho “forecast”", html)
        self.assertIn('value="forecast"', html)                 # search box keeps the text
        self.assertIn('<option value="image" selected>', html)  # filter keeps the kind
        self.assertIn("?q=forecast&amp;kind=image\" title", sidebar)  # opening a document keeps the filter

    def test_document_page_keeps_the_filtered_sidebar(self):
        png = next(d for d in self.backend.documents.values() if d["original_name"] == "Forecast chart.png")
        html = self.client.get(f"/ui/documents/{png['id']}", params={"kind": "image"}).text
        sidebar = html.split('id="source-results"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("demo.mp4", sidebar)
        self.assertIn(f'source-item ready active" data-document-id="{png["id"]}"', sidebar)

    def test_no_match_offers_to_clear(self):
        html = self.client.get(f"/ui/projects/{self.project_id}", params={"q": "nothing-like-this"}).text
        self.assertIn("Không có tài liệu nào khớp", html)
        self.assertIn("data-source-clear", html)

    def test_unknown_kind_in_the_page_url_is_ignored(self):
        response = self.client.get(f"/ui/projects/{self.project_id}", params={"kind": "spaceship"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("demo.mp4", response.text)

    def test_pager_keeps_search(self):
        for i in range(20):
            self.post_file(f"forecast-{i}.txt", b"x")
        html = self.client.get(f"/ui/projects/{self.project_id}", params={"q": "forecast"}).text
        self.assertIn("?q=forecast&amp;offset=20", html)


class ProjectOverviewTests(DocumentTestCase):
    """The main panel on the project page: whole-project totals by kind."""

    def test_totals_cover_every_page_and_group_by_kind(self):
        for i in range(22):
            self.post_file(f"paper-{i}.txt", b"x" * 10)
        self.post_file("clip.mp4", b"x" * 100)
        self.post_file("photo.png", b"x" * 5)
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        main = html.split('id="main"', 1)[1]
        self.assertIn("<strong>24</strong><small>tệp trong dự án", main)  # not just the 20 in the sidebar
        self.assertIn("<strong>22</strong><small>tệp có văn bản cho AI", main)
        self.assertIn('data-kind-filter="video"', main)
        self.assertIn("22 tệp · 220 B", main)

    def test_empty_project_says_how_to_start(self):
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn("Dự án chưa có tệp nào", html)
        self.assertNotIn("data-kind-filter", html)

    def test_documents_open_in_the_main_panel(self):
        document = upload(self.client, self.project_id)
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn(f'href="/ui/documents/{document["id"]}" title="paper.txt" data-open-in-main', html)
        detail = self.client.get(f"/ui/documents/{document['id']}").text
        # The breadcrumb back to the project overview also stays in place.
        self.assertIn(f'href="/ui/projects/{self.project_id}" data-open-in-main', detail)


class PreviewAndStreamingTests(DocumentTestCase):
    """Images/audio/video play on the page; large files are streamed in ranges."""

    VIDEO = bytes(range(100))

    def upload_bytes(self, name, content):
        return self.client.post(f"/projects/{self.project_id}/documents", files={"file": (name, content)}).json()

    def test_media_preview_is_inline_and_sandboxed(self):
        document = self.upload_bytes("photo.png", b"\x89PNG fake image")
        response = self.client.get(f"/documents/{document['id']}/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x89PNG fake image")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.headers["content-disposition"].startswith("inline"))
        self.assertEqual(response.headers["content-security-policy"], "sandbox")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_non_media_cannot_be_previewed(self):
        for name in ("paper.txt", "slides.pptx", "data.zip"):
            with self.subTest(name=name):
                document = self.upload_bytes(name, b"x")
                self.assertEqual(self.client.get(f"/documents/{document['id']}/content").status_code, 415)

    def test_range_requests_let_video_seek(self):
        document = self.upload_bytes("clip.mp4", self.VIDEO)
        url = f"/documents/{document['id']}/content"
        for header, body, content_range in (
            ("bytes=10-19", self.VIDEO[10:20], "bytes 10-19/100"),
            ("bytes=95-", self.VIDEO[95:], "bytes 95-99/100"),
            ("bytes=-5", self.VIDEO[95:], "bytes 95-99/100"),
            ("bytes=90-500", self.VIDEO[90:], "bytes 90-99/100"),  # end clamped to the file
        ):
            with self.subTest(range=header):
                response = self.client.get(url, headers={"Range": header})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, body)
                self.assertEqual(response.headers["content-range"], content_range)
                self.assertEqual(response.headers["content-length"], str(len(body)))

    def test_whole_file_when_range_absent_or_malformed(self):
        document = self.upload_bytes("clip.mp4", self.VIDEO)
        for headers in ({}, {"Range": "bytes=abc"}, {"Range": "bytes=0-1,5-9"}):
            with self.subTest(headers=headers):
                response = self.client.get(f"/documents/{document['id']}/content", headers=headers)
                self.assertEqual((response.status_code, response.content), (200, self.VIDEO))
                self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_range_past_the_end_is_416(self):
        document = self.upload_bytes("clip.mp4", self.VIDEO)
        response = self.client.get(f"/documents/{document['id']}/content", headers={"Range": "bytes=100-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */100")

    def test_download_supports_ranges_too(self):
        document = self.upload_bytes("dataset.zip", self.VIDEO)
        response = self.client.get(f"/documents/{document['id']}/download", headers={"Range": "bytes=0-9"})
        self.assertEqual((response.status_code, response.content), (206, self.VIDEO[:10]))
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))

    def test_document_page_shows_the_right_player(self):
        for name, tag in (("photo.png", "<img"), ("clip.mp4", "<video"), ("talk.mp3", "<audio")):
            with self.subTest(name=name):
                document = self.upload_bytes(name, b"x")
                html = self.client.get(f"/ui/documents/{document['id']}").text
                self.assertIn(f'{tag} src="/documents/{document["id"]}/content"', html)
        document = self.upload_bytes("paper.txt", b"x")
        html = self.client.get(f"/ui/documents/{document['id']}").text
        self.assertNotIn("/content", html)

    def test_media_is_not_offered_to_the_ai(self):
        text = self.upload_bytes("paper.txt", b"x")
        video = self.upload_bytes("clip.mp4", b"x")
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn(f'name="document_ids" value="{text["id"]}"', html)
        self.assertNotIn(f'name="document_ids" value="{video["id"]}"', html)


class UpdateDocumentTests(DocumentTestCase):
    def test_api_patch_replaces_metadata(self):
        document = self.upload()
        response = self.client.patch(
            f"/documents/{document['id']}",
            data={"tags": "ml, survey", "authors": "An, Binh", "custom_metadata": '{"venue": "ICML"}'},
        )
        self.assertEqual(response.status_code, 200)
        stored = self.backend.details[document["id"]]
        self.assertEqual(stored["tags"], ["ml", "survey"])
        self.assertEqual(stored["authors"], ["An", "Binh"])
        self.assertEqual(stored["custom_metadata"], {"venue": "ICML"})
        # The file and its row are untouched by a metadata edit.
        self.assertEqual(self.backend.objects[document["object_name"]], b"original bytes")
        self.assertEqual(stored["sha256"], document["sha256"])

    def test_form_edit_prefills_then_saves(self):
        document = self.upload()
        edit_page = self.client.get(f"/ui/documents/{document['id']}/edit").text
        self.assertIn("docker", edit_page)
        response = self.client.post(
            f"/ui/documents/{document['id']}/edit",
            data={"tags": "updated", "authors": "Dat", "custom_metadata": "{}"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.backend.details[document["id"]]["tags"], ["updated"])

    def test_invalid_metadata_changes_nothing(self):
        document = self.upload()
        response = self.client.patch(f"/documents/{document['id']}", data={"custom_metadata": "not json"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.backend.details[document["id"]]["custom_metadata"], {"year": 2026})

    def test_update_unknown_document(self):
        self.assertEqual(self.client.patch(f"/documents/{uuid4()}", data={}).status_code, 404)


class DeleteDocumentTests(DocumentTestCase):
    def test_form_requires_confirmation_then_deletes_everywhere(self):
        document = self.upload()
        url = f"/ui/documents/{document['id']}/delete"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, data={}).status_code, 422)
        self.assertIn(UUID(document["id"]), self.backend.documents)

        response = self.client.post(url, data={"confirm": "delete"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], f"/ui/projects/{self.project_id}")
        # Intent recorded first, then file, metadata, row — so a failure leaves a retryable row.
        self.assertEqual(self.backend.events, ["intent", "object-delete", "mongo-delete", "sql-delete"])
        self.assertEqual(self.backend.documents, {})
        self.assertNotIn(document["id"], self.backend.details)
        self.assertNotIn(document["object_name"], self.backend.objects)

    def test_delete_is_idempotent(self):
        document = self.upload()
        self.assertEqual(self.client.delete(f"/documents/{document['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/documents/{document['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/documents/{document['id']}").status_code, 200)

    def test_pending_upload_cannot_be_deleted(self):
        document = self.upload()
        self.backend.documents[UUID(document["id"])]["status"] = "pending"
        self.assertEqual(self.client.delete(f"/documents/{document['id']}").status_code, 409)
        self.assertIn(document["id"], self.backend.details)
        self.assertIn(document["object_name"], self.backend.objects)


class DeleteRecoveryTests(DocumentTestCase):
    """A store failing mid-delete leaves the document 'deleting'; retrying finishes it."""

    def delete_fails_on(self, store):
        document = self.upload()
        self.backend.fail.add(store)
        self.assertEqual(self.client.delete(f"/documents/{document['id']}").status_code, 503)
        self.assertEqual(self.backend.documents[UUID(document["id"])]["status"], "deleting")
        self.backend.fail.discard(store)
        return document

    def assert_retry_finishes(self, document):
        self.assertEqual(self.client.delete(f"/documents/{document['id']}").status_code, 200)
        self.assertEqual(self.backend.documents, {})
        self.assertEqual(self.backend.details, {})
        self.assertEqual(self.backend.objects, {})

    def test_file_store_failure(self):
        document = self.delete_fails_on("minio")
        # Stopped at the first step: file and metadata both still there.
        self.assertIn(document["object_name"], self.backend.objects)
        self.assertIn(document["id"], self.backend.details)
        self.assert_retry_finishes(document)

    def test_metadata_store_failure(self):
        document = self.delete_fails_on("mongo")
        self.assertNotIn(document["object_name"], self.backend.objects)  # file step already done
        self.assertEqual(self.client.get(f"/documents/{document['id']}/download").status_code, 409)
        self.assertIn("Thử xóa lại", self.client.get(f"/ui/projects/{self.project_id}").text)
        self.assert_retry_finishes(document)

    def test_final_row_delete_failure(self):
        self.assert_retry_finishes(self.delete_fails_on("sql-finish"))


if __name__ == "__main__":
    unittest.main()
