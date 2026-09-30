"""Document management: upload, view/download, edit metadata, delete — across
PostgreSQL (row), MongoDB (metadata) and MinIO (file), including what happens
when one of those stores fails halfway. Storage replaced by support.py.
"""
import hashlib
import unittest
from uuid import UUID, uuid4

from support import FakeBackend, upload


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
            ("big.txt", b"x" * (10 * 1024 * 1024 + 1), {}, 413),         # over 10 MiB
            ("empty.txt", b"", {}, 422),                                 # empty file
            ("ok.txt", b"x", {"custom_metadata": "[1, 2]"}, 422),         # metadata not an object
            ("ok.txt", b"x", {"custom_metadata": "{broken"}, 422),        # metadata not JSON
            ("ok.txt", b"x", {"tags": ",".join(f"t{i}" for i in range(51))}, 422),  # > 50 tags
        ]
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
