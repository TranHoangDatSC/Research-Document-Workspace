"""Project management: create, view, update, delete — JSON API and browser UI.
Real services/routes/templates; storage replaced by tests/unit/support.py.
"""
import unittest
from uuid import UUID, uuid4

from support import FakeBackend, upload


def project_grid(html):
    """The home page's project list only — the sidebar also names recent projects."""
    return html.split('class="project-grid"', 1)[1].split("</section>", 1)[0]


class ProjectTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.client = self.backend.client(self)

    def create(self, name="Cloud project", description="About cloud"):
        response = self.client.post("/projects", json={"name": name, "description": description})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class CreateProjectTests(ProjectTestCase):
    def test_api_create_returns_new_project(self):
        project = self.create("  Cloud project  ", "About cloud")
        self.assertEqual(project["name"], "Cloud project")  # surrounding spaces trimmed
        self.assertEqual(project["description"], "About cloud")
        self.assertIn(UUID(project["id"]), self.backend.projects)

    def test_api_rejects_invalid_input(self):
        for payload in (
            {"name": "   "},                           # blank name
            {"name": "x" * 201},                       # name too long
            {"name": "ok", "description": "x" * 5001},  # description too long
            {"name": "ok", "owner": "someone"},        # unknown field
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/projects", json=payload).status_code, 422)
        self.assertEqual(self.backend.projects, {})

    def test_form_create_redirects_to_new_project_page(self):
        response = self.client.post("/ui/projects", data={"name": "UI project"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("UI project", self.client.get(response.headers["location"]).text)

    def test_form_blank_name_shows_html_error(self):
        response = self.client.post("/ui/projects", data={"name": "   "})
        self.assertEqual(response.status_code, 422)
        self.assertIn("text/html", response.headers["content-type"])

    def test_project_name_is_html_escaped(self):
        response = self.client.post("/ui/projects", data={"name": "<script>alert(1)</script>"}, follow_redirects=False)
        html = self.client.get(response.headers["location"]).text
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_form_from_another_site_is_rejected(self):
        rejected = self.client.post("/ui/projects", data={"name": "no"}, headers={"Origin": "https://other.invalid"})
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post("/ui/projects", data={"name": "ok"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303)


class ViewProjectTests(ProjectTestCase):
    def test_api_get_one(self):
        project = self.create()
        response = self.client.get(f"/projects/{project['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Cloud project")

    def test_api_get_unknown_or_malformed_id(self):
        self.assertEqual(self.client.get(f"/projects/{uuid4()}").status_code, 404)
        response = self.client.get("/projects/not-a-uuid")
        self.assertEqual(response.status_code, 422)
        self.assertIn("application/json", response.headers["content-type"])

    def test_api_list_newest_first_with_paging(self):
        for name in ("first", "second", "third"):
            self.create(name)
        names = [p["name"] for p in self.client.get("/projects").json()]
        self.assertEqual(names, ["third", "second", "first"])
        page = self.client.get("/projects", params={"limit": 1, "offset": 1}).json()
        self.assertEqual([p["name"] for p in page], ["second"])

    def test_home_page_lists_and_searches_projects(self):
        self.create("Machine learning survey")
        self.create("Cloud deployment")
        html = self.client.get("/").text
        self.assertIn("Machine learning survey", html)
        self.assertIn("Cloud deployment", html)
        searched = project_grid(self.client.get("/", params={"q": "cloud"}).text)
        self.assertIn("Cloud deployment", searched)
        self.assertNotIn("Machine learning survey", searched)

    def test_home_page_paginates(self):
        for i in range(8):
            self.create(f"Project {i}")
        first_page = project_grid(self.client.get("/").text)
        self.assertIn("Project 7", first_page)
        self.assertNotIn("Project 0", first_page)
        self.assertIn("Project 0", project_grid(self.client.get("/", params={"page": 2}).text))

    def test_project_page_shows_its_documents(self):
        project = self.create()
        upload(self.client, project["id"], name="notes.txt")
        html = self.client.get(f"/ui/projects/{project['id']}").text
        self.assertIn("Cloud project", html)
        self.assertIn("notes.txt", html)

    def test_unknown_project_page_is_html_404(self):
        response = self.client.get(f"/ui/projects/{uuid4()}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("text/html", response.headers["content-type"])


class UpdateProjectTests(ProjectTestCase):
    def test_api_update(self):
        project = self.create()
        response = self.client.put(f"/projects/{project['id']}", json={"name": "Renamed", "description": "New"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.backend.projects[UUID(project["id"])]["name"], "Renamed")

    def test_api_update_validation_and_unknown_project(self):
        project = self.create()
        self.assertEqual(self.client.put(f"/projects/{project['id']}", json={"name": " "}).status_code, 422)
        self.assertEqual(self.backend.projects[UUID(project["id"])]["name"], "Cloud project")
        self.assertEqual(self.client.put(f"/projects/{uuid4()}", json={"name": "x"}).status_code, 404)

    def test_form_edit_prefills_then_saves(self):
        project = self.create("Old name", "Old description")
        edit_page = self.client.get(f"/ui/projects/{project['id']}/edit").text
        self.assertIn("Old name", edit_page)
        self.assertIn("Old description", edit_page)
        response = self.client.post(
            f"/ui/projects/{project['id']}/edit", data={"name": "New name", "description": "New description"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("New name", self.client.get(response.headers["location"]).text)

    def test_form_edit_rejects_blank_name(self):
        project = self.create()
        self.assertEqual(self.client.post(f"/ui/projects/{project['id']}/edit", data={"name": ""}).status_code, 422)
        self.assertEqual(self.backend.projects[UUID(project["id"])]["name"], "Cloud project")


class DeleteProjectTests(ProjectTestCase):
    def test_form_requires_confirmation(self):
        project = self.create()
        url = f"/ui/projects/{project['id']}/delete"
        self.assertIn("Cloud project", self.client.get(url).text)
        self.assertEqual(self.client.post(url, data={}).status_code, 422)
        self.assertIn(UUID(project["id"]), self.backend.projects)
        response = self.client.post(url, data={"confirm": "delete"}, follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        self.assertNotIn(UUID(project["id"]), self.backend.projects)

    def test_delete_removes_documents_files_metadata_and_chat(self):
        project = self.create()
        document = upload(self.client, project["id"])
        self.backend.add_messages([{"project_id": project["id"], "user_id": "u", "role": "user", "content": "hi"}])
        self.assertEqual(self.client.delete(f"/projects/{project['id']}").status_code, 204)
        self.assertEqual(self.backend.documents, {})
        self.assertEqual(self.backend.details, {})
        self.assertNotIn(document["object_name"], self.backend.objects)
        self.assertEqual(self.backend.chats, [])

    def test_pending_upload_blocks_delete(self):
        project = self.create()
        document = upload(self.client, project["id"])
        self.backend.documents[UUID(document["id"])]["status"] = "pending"
        self.assertEqual(self.client.delete(f"/projects/{project['id']}").status_code, 409)
        self.assertIn(UUID(project["id"]), self.backend.projects)
        self.assertIn(UUID(document["id"]), self.backend.documents)

    def test_delete_unknown_project(self):
        self.assertEqual(self.client.delete(f"/projects/{uuid4()}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
