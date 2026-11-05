"""HTTP integration tests with injected local runners, never paid model APIs."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import create_app
from backend.jobs import JobManager
from config_manager import get_default_config


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.root = self.workspace / "AI_NovelGenerator"
        self.root.mkdir(parents=True)
        self.novel = self.workspace / "小说"
        self.novel.mkdir()
        (self.novel / "Novel_architecture.txt").write_text("已有大纲", encoding="utf-8")
        config = get_default_config()
        self.secret = "never-return-this-test-api-key"
        for preset in config["llm_configs"].values():
            preset["api_key"] = self.secret
        config["other_params"]["filepath"] = str(self.novel)
        self.config = config
        self.write_config()
        self.releases = []
        self.runner = self.default_runner
        self.app = create_app(self.root, self.root / "runtime", runner=lambda ctx: self.runner(ctx))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.client.__exit__(None, None, None)
        self.app.state.jobs.worker.join(timeout=5)
        self.assertFalse(self.app.state.jobs.worker.is_alive())
        self.temp.cleanup()

    def write_config(self):
        (self.root / "config.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def default_runner(self, ctx):
        ctx.emit("开始测试生成", "生成中")
        return {"documents": [{"kind": "architecture", "chapter": 1, "content": "候选大纲",
                                "base_revision": ctx.input_doc("architecture")["revision"]}]}

    def imported_project(self):
        response = self.client.post("/api/projects/import", json={"path": str(self.novel)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def doc_url(self, project, kind="architecture", chapter=1):
        return f"/api/projects/{project['id']}/documents/{kind}?chapter={chapter}"

    def start_job(self, project, **payload):
        response = self.client.post(f"/api/projects/{project['id']}/jobs", json={"type": "architecture", **payload})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait_job(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job['id']}")
            self.assertEqual(response.status_code, 200, response.text)
            latest = response.json()
            if latest["status"] in JobManager.TERMINAL:
                return latest
            time.sleep(0.01)
        self.fail(f"job timed out: {latest}")

    def test_bootstrap_imports_configured_legacy_novel_without_exposing_secrets(self):
        response = self.client.get("/api/bootstrap")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["projects"]), 1)
        project = body["projects"][0]
        self.assertEqual(body["active_project_id"], project["id"])
        self.assertEqual(project["path"], str(self.novel))
        self.assertEqual(self.client.get(self.doc_url(project)).json()["content"], "已有大纲")
        self.assertFalse(body["config"]["settings_deferred"])
        self.assertTrue(any(model["configured"] for model in body["config"]["models"]))
        self.assertNotIn(self.secret, response.text)
        self.assertNotIn(self.secret, self.client.get("/api/settings").text)
        self.assertEqual(len(self.client.get("/api/bootstrap").json()["projects"]), 1)

    def test_project_creation_and_parameter_update_validate_numbers(self):
        response = self.client.post("/api/projects", json={"name": "新书", "params": {"num_chapters": 20, "word_number": 2000}})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertTrue(Path(project["path"]).is_dir())
        updated = self.client.patch(f"/api/projects/{project['id']}", json={"params": {"topic": "冒险", "num_chapters": 30}})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["params"]["num_chapters"], 30)
        self.assertEqual(updated.json()["params"]["word_number"], 2000)
        rejected = self.client.patch(f"/api/projects/{project['id']}", json={"params": {"num_chapters": 0}})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}").json()["params"]["num_chapters"], 30)

    def test_document_edit_history_and_stale_revision_return_conflict(self):
        project = self.imported_project()
        url = self.doc_url(project)
        initial = self.client.get(url).json()
        saved = self.client.put(url, json={"content": "人工修改", "revision": initial["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        rejected = self.client.put(url, json={"content": "过时编辑", "revision": initial["revision"]})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(self.client.get(url).json()["content"], "人工修改")
        history = self.client.get(f"/api/projects/{project['id']}/documents/architecture/history").json()["versions"]
        self.assertEqual({version["content"] for version in history}, {"已有大纲", "人工修改"})
        self.assertEqual(self.client.put(url, json={"content": "没有版本号"}).status_code, 400)

    def test_generation_returns_candidate_then_accepts_and_preserves_history(self):
        project = self.imported_project()
        job = self.start_job(project)
        done = self.wait_job(job)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["documents"][0]["content"], "候选大纲")
        self.assertEqual(self.client.get(self.doc_url(project)).json()["content"], "已有大纲")
        applied = self.client.post(f"/api/jobs/{job['id']}/apply")
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["documents"][0]["content"], "候选大纲")
        self.assertTrue(self.client.get(f"/api/jobs/{job['id']}").json()["applied"])
        history_url = f"/api/projects/{project['id']}/documents/architecture/history"
        history = self.client.get(history_url).json()
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/apply").status_code, 200)
        self.assertEqual(self.client.get(history_url).json(), history)
        self.assertEqual(self.client.get(f"/api/jobs?project_id={project['id']}").json()["jobs"][0]["id"], job["id"])

    def test_candidate_cannot_replace_edits_saved_after_generation(self):
        project = self.imported_project()
        job = self.start_job(project)
        self.assertEqual(self.wait_job(job)["status"], "succeeded")
        url = self.doc_url(project)
        document = self.client.get(url).json()
        saved = self.client.put(url, json={"content": "保留这次人工编辑", "revision": document["revision"]})
        self.assertEqual(saved.status_code, 200)
        response = self.client.post(f"/api/jobs/{job['id']}/apply")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get(url).json()["content"], "保留这次人工编辑")

    def test_active_task_blocks_duplicate_calls_and_file_writes_until_cancelled(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def blocking_runner(ctx):
            started.set()
            release.wait(5)
            ctx.check_cancel()
            return {}

        self.runner = blocking_runner
        project = self.imported_project()
        document = self.client.get(self.doc_url(project)).json()
        job = self.start_job(project, type="draft", chapter=1)
        self.assertTrue(started.wait(5))
        duplicate = self.client.post(f"/api/projects/{project['id']}/jobs", json={"type": "draft", "chapter": 1})
        self.assertEqual(duplicate.status_code, 409)
        saving = self.client.put(self.doc_url(project), json={"content": "不能中途保存", "revision": document["revision"]})
        self.assertEqual(saving.status_code, 409)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/apply").status_code, 400)
        cancelling = self.client.post(f"/api/jobs/{job['id']}/cancel")
        self.assertEqual(cancelling.json()["status"], "cancelling")
        release.set()
        self.assertEqual(self.wait_job(job)["status"], "cancelled")
        saving = self.client.put(self.doc_url(project), json={"content": "停止后保存", "revision": document["revision"]})
        self.assertEqual(saving.status_code, 200)

    def test_sse_replays_only_events_after_cursor_and_closes_on_completion(self):
        project = self.imported_project()
        job = self.start_job(project)
        done = self.wait_job(job)
        cursor = done["events"][0]["seq"]
        response = self.client.get(f"/api/jobs/{job['id']}/events?after={cursor}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = []
        for block in response.text.split("\n\n"):
            lines = block.splitlines()
            if "event: log" in lines:
                events.append(json.loads(next(line[6:] for line in lines if line.startswith("data: "))))
        self.assertEqual([event["seq"] for event in events], [event["seq"] for event in done["events"] if event["seq"] > cursor])
        self.assertIn('event: done\ndata: {"status": "succeeded"}', response.text)

    def test_runner_failure_is_visible_and_does_not_return_credentials(self):
        def failing_runner(ctx):
            raise RuntimeError(f"model request failed with {self.secret}")

        self.runner = failing_runner
        project = self.imported_project()
        done = self.wait_job(self.start_job(project))
        self.assertEqual(done["status"], "failed")
        self.assertIn("model request failed", done["error"])
        for response in (self.client.get(f"/api/jobs/{done['id']}"), self.client.get("/api/jobs"),
                         self.client.get(f"/api/jobs/{done['id']}/events")):
            self.assertNotIn(self.secret, response.text)

    def test_invalid_paths_kinds_numbers_and_missing_resources_are_rejected(self):
        project = self.imported_project()
        invalid_imports = ({}, {"path": str(self.root)}, {"path": str(Path(self.temp.name))})
        for body in invalid_imports:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/projects/import", json=body).status_code, 400)
        self.assertEqual(self.client.get(self.doc_url(project, "not-a-document")).status_code, 400)
        self.assertEqual(self.client.get(self.doc_url(project, "chapter", 0)).status_code, 422)
        self.assertEqual(self.client.get("/api/projects/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)
        self.assertEqual(self.client.post(f"/api/projects/{project['id']}/jobs", json={"type": "unknown"}).status_code, 400)

    def test_local_api_rejects_cross_site_mutations_and_untrusted_hosts(self):
        response = self.client.post("/api/projects", json={"name": "跨站"}, headers={"Origin": "https://other.invalid"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/projects", json={"name": "跨站"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "other.invalid"}).status_code, 400)
        health = self.client.get("/api/health")
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(health.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
