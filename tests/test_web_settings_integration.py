"""Exercise route dispatch, job snapshots and settings effects without external services."""
import json
import logging
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app import create_app
from backend.language import creative_language
from backend.storage import Conflict
from config_manager import get_default_config


class SettingsIntegrationTests(unittest.TestCase):
    def setUp(self):
        parent = REPO_ROOT / "backend" / "runtime" / "test-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name) / "app"
        self.root.mkdir()
        conf = get_default_config()
        conf["other_params"].update(topic="全书主题", genre="悬疑", num_chapters=7, word_number=1200,
                                    chapter_num="2", user_guidance="全书指导", characters_involved="林砚",
                                    key_items="来信", scene_location="灯塔", time_constraint="天亮前")
        self.root.joinpath("config.json").write_text(json.dumps(conf), encoding="utf-8")
        self.app = create_app(self.root, self.root / "runtime")
        self.manager = self.app.state.jobs
        self.store = self.app.state.store

    def tearDown(self):
        self.manager.close()
        self.manager.worker.join(5)
        self.assertFalse(self.manager.worker.is_alive())
        self.temp.cleanup()

    def route(self, path, method="GET", **kwargs):
        match = next(r for r in self.app.routes if getattr(r, "path", None) == path and method in r.methods)
        return match.endpoint(**kwargs)

    def done(self, job):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = self.manager.get(job["id"])
            if value["status"] in self.manager.TERMINAL:
                return value
            time.sleep(.01)
        self.fail("settings job timed out")

    def project(self):
        return self.route("/api/projects", "POST", body={"name": "新小说"})

    def test_settings_routes_preserve_revision_and_refresh_bootstrap(self):
        data = self.route("/api/settings")
        saved = self.route("/api/settings", "PUT", body={"revision": data["revision"], "config": {"language": "en"}})
        self.assertEqual(saved["config"]["language"], "en")
        self.assertEqual(self.route("/api/bootstrap")["config"]["language"], "en")
        self.assertFalse(self.route("/api/bootstrap")["config"]["settings_deferred"])
        with self.assertRaises(Conflict):
            self.route("/api/settings", "PATCH", body={"revision": data["revision"], "config": {"language": "zh"}})
        self.assertEqual(self.route("/api/settings/reload", "POST")["revision"], saved["revision"])

    def test_new_projects_inherit_every_original_creative_default(self):
        project = self.project()
        for key, value in {"topic": "全书主题", "genre": "悬疑", "num_chapters": 7,
                           "word_number": 1200, "chapter_num": 2, "user_guidance": "全书指导",
                           "characters_involved": "林砚", "key_items": "来信", "scene_location": "灯塔",
                           "time_constraint": "天亮前"}.items():
            self.assertEqual(project["params"][key], value, key)

    def test_explicit_external_novel_directory_is_supported(self):
        folder = Path(self.temp.name) / "outside-original-workspace"
        folder.mkdir()
        project = self.store.register("外部目录", folder, existing=True)
        self.assertEqual(Path(project["path"]), folder.resolve())
        with self.assertRaises(ValueError):
            self.store.register("不能写代码目录", self.root, existing=True)

    def test_unsaved_connection_credentials_never_enter_job_records(self):
        secret = "unsaved-private-key-928"
        class Model:
            def invoke(self, prompt):
                logging.warning("provider error: %s", secret)
                raise RuntimeError("connection failed for " + secret)
        with patch("llm_adapters.create_llm_adapter", return_value=Model()):
            job = self.route("/api/settings/operations", "POST", body={"type": "test_llm", "name": "临时预设",
                "config": {"model_name": "offline", "base_url": "https://not-called.invalid/v1",
                           "interface_format": "OpenAI", "api_key": secret}})
            value = self.done(job)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["project_id"], "__settings__")
        self.assertEqual(self.store.projects(), [])
        self.assertNotIn(secret, json.dumps(self.store.jobs()))
        self.assertEqual(self.route("/api/settings/jobs")["jobs"][0]["id"], job["id"])

    def test_saved_language_is_used_by_real_generation_dispatch_and_restored(self):
        import config_manager
        import prompt_definitions
        import prompt_definitions_en
        old = prompt_definitions.core_seed_prompt
        data = self.route("/api/settings")
        self.route("/api/settings", "PUT", body={"revision": data["revision"], "config": {"language": "en"}})
        def architecture(ctx, options, stage, docs):
            self.assertTrue(config_manager.IS_ENGLISH)
            self.assertEqual(prompt_definitions.core_seed_prompt, prompt_definitions_en.core_seed_prompt)
            return {"documents": [], "language": "en"}
        project = self.project()
        with patch("backend.generation._architecture", side_effect=architecture):
            value = self.done(self.manager.submit(project, {"type": "architecture"}))
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(value["result"]["language"], "en")
        self.assertFalse(config_manager.IS_ENGLISH)
        self.assertEqual(prompt_definitions.core_seed_prompt, old)
        with self.assertRaises(RuntimeError), creative_language({"language": "en"}):
            raise RuntimeError("test restore")
        self.assertFalse(config_manager.IS_ENGLISH)

    def test_knowledge_import_clear_and_reimport_preserve_source_and_memory(self):
        project = self.project()
        doc = self.store.save_doc(project, "chapter", "已定稿正文")
        self.store.record_finalization(project, 1, doc["revision"], {"index": "succeeded", "memory": "completed"})
        with patch("backend.vector_memory.index_knowledge", return_value={"chunks": 2, "content_hash": "hash"}) as index:
            value = self.done(self.route("/api/projects/{project_id}/knowledge", "POST", project_id=project["id"],
                body={"filename": "参考.txt", "content": "参考资料内容"}))
            self.assertEqual(value["status"], "succeeded", value)
            self.assertEqual(index.call_count, 1)
        listing = self.route("/api/projects/{project_id}/knowledge", project_id=project["id"])
        self.assertEqual(listing["files"][0]["name"], "参考.txt")
        with self.assertRaises(ValueError):
            self.route("/api/projects/{project_id}/knowledge/clear", "POST", project_id=project["id"], body={})
        with patch("backend.vector_memory.clear_index", return_value=9):
            cleared = self.done(self.route("/api/projects/{project_id}/knowledge/clear", "POST",
                project_id=project["id"], body={"confirm": True}))
        self.assertEqual(cleared["status"], "succeeded")
        receipt = self.store.get_finalization(project, 1)
        self.assertEqual(receipt["details"]["index"], "failed")
        self.assertEqual(receipt["details"]["memory"], "completed")
        self.assertEqual(self.store.doc(project, "chapter")["content"], "已定稿正文")
        source = Path(project["path"]) / ".knowledge" / "参考.txt"
        self.assertEqual(source.read_text(encoding="utf-8"), "参考资料内容")
        with patch("backend.vector_memory.index_knowledge", return_value={"chunks": 2, "content_hash": "hash"}):
            reimported = self.done(self.route("/api/projects/{project_id}/knowledge", "POST", project_id=project["id"],
                body={"filename": "参考.txt"}))
        self.assertEqual(reimported["status"], "succeeded", reimported)

    def test_failed_or_invalid_knowledge_import_does_not_change_sources(self):
        project = self.project()
        for filename, content in (("../config.json", "bad"), ("empty.txt", "")):
            value = self.done(self.manager.submit(project, {"type": "knowledge_import", "filename": filename, "content": content}))
            self.assertEqual(value["status"], "failed")
        with patch("backend.vector_memory.index_knowledge", side_effect=RuntimeError("offline")):
            value = self.done(self.manager.submit(project, {"type": "knowledge_import", "filename": "new.txt", "content": "文本"}))
        self.assertEqual(value["status"], "failed")
        self.assertFalse((Path(project["path"]) / ".knowledge" / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
