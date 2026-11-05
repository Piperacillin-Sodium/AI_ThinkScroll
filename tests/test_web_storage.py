"""File safety and durable task tests; all inputs stay in temporary workspaces."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.jobs import JobManager
from backend.storage import Conflict, Store, now, revision


class WorkbenchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.root = self.workspace / "AI_NovelGenerator"
        self.root.mkdir(parents=True)
        self.store = Store(self.root, self.root / "runtime")
        self.managers = []
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for manager in self.managers:
            manager.close()
            manager.worker.join(timeout=5)
            self.assertFalse(manager.worker.is_alive(), "test worker did not stop")
        self.temp.cleanup()

    def project(self, name="测试小说"):
        return self.store.register(name, self.workspace / name)

    def manager(self, runner, config=None):
        manager = JobManager(self.store, lambda: config or {}, runner=runner)
        self.managers.append(manager)
        return manager

    def release_event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def completed(self, manager, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job["status"] in JobManager.TERMINAL:
                return job
            time.sleep(0.01)
        self.fail(f"task did not finish: {manager.get(job_id)}")


class WebStorageTest(WorkbenchFixture):
    def test_create_and_import_preserve_legacy_files_and_registration(self):
        created = self.store.register('名称/包含:符号', params={"num_chapters": "12"})
        self.assertTrue(Path(created["path"]).is_relative_to(self.workspace / "小说作品"))
        self.assertEqual(created["params"]["num_chapters"], 12)

        legacy = self.workspace / "小说"
        legacy.mkdir()
        (legacy / "Novel_architecture.txt").write_text("原有大纲", encoding="utf-8-sig")
        imported = self.store.register("旧书", legacy, existing=True)
        repeated = self.store.register("重复登记", legacy, existing=True)
        self.assertEqual(imported["id"], repeated["id"])
        self.assertEqual(self.store.doc(imported, "architecture")["content"], "原有大纲")
        reopened = Store(self.root, self.store.data_dir)
        self.assertEqual(reopened.project(imported["id"])["path"], str(legacy))
        self.assertEqual(len(reopened.projects()), 2)

    def test_import_rejects_outside_workspace_code_and_missing_folders(self):
        for dest in (Path(self.temp.name) / "outside", self.workspace, self.root,
                     self.root / "novel", self.workspace / "missing"):
            with self.subTest(path=dest), self.assertRaises(ValueError):
                self.store.register("不合法", dest, existing=True)
        self.assertEqual(self.store.projects(), [])

    def test_stale_save_cannot_overwrite_newer_text_or_add_history(self):
        project = self.project()
        initial = self.store.doc(project, "chapter", 1)
        saved = self.store.save_doc(project, "chapter", "第一稿", initial["revision"], chapter=1)
        with self.assertRaises(Conflict):
            self.store.save_doc(project, "chapter", "另一页的旧稿", initial["revision"], chapter=1)
        self.assertEqual(self.store.doc(project, "chapter", 1), saved)
        self.assertEqual([v["content"] for v in self.store.history(project, "chapter", 1)], ["第一稿"])
        self.assertEqual(list(Path(project["path"]).rglob("*.tmp")), [])

    def test_all_documents_are_validated_before_any_write(self):
        project = self.project()
        for kind in ("summary", "characters"):
            self.store.save_doc(project, kind, f"原始{kind}")
        before = {kind: self.store.doc(project, kind) for kind in ("summary", "characters")}
        with self.assertRaises(Conflict):
            self.store.apply_documents(project, [
                {"kind": "summary", "content": "新摘要", "base_revision": before["summary"]["revision"]},
                {"kind": "characters", "content": "新状态", "base_revision": "stale"},
            ])
        for kind in before:
            self.assertEqual(self.store.doc(project, kind), before[kind])
            self.assertEqual(len(self.store.history(project, kind)), 1)

        with self.assertRaises(ValueError):
            self.store.apply_documents(project, [
                {"kind": "summary", "content": "新摘要"},
                {"kind": "../../config", "content": "不可写"},
            ])
        self.assertEqual(self.store.doc(project, "summary"), before["summary"])

    def test_multi_file_failure_rolls_back_files_and_history(self):
        project = self.project()
        self.store.save_doc(project, "summary", "原摘要")
        write = self.store.atomic_write
        failed_once = False

        def fail_second(path, content):
            nonlocal failed_once
            if Path(path).name == "character_state.txt" and not failed_once:
                failed_once = True
                raise OSError("simulated disk failure")
            return write(path, content)

        with patch.object(self.store, "atomic_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.store.apply_documents(project, [
                    {"kind": "summary", "content": "新摘要"},
                    {"kind": "characters", "content": "新状态"},
                ])
        self.assertEqual(self.store.doc(project, "summary")["content"], "原摘要")
        self.assertFalse(self.store.document_path(project, "characters").exists())
        self.assertEqual([h["content"] for h in self.store.history(project, "summary")], ["原摘要"])
        self.assertEqual(self.store.history(project, "characters"), [])

    def test_first_overwrite_saves_imported_file_in_history(self):
        project = self.project()
        path = self.store.document_path(project, "architecture")
        path.write_text("外部原稿", encoding="utf-8")
        self.store.save_doc(project, "architecture", "人工修改", revision("外部原稿"))
        history = self.store.history(project, "architecture")
        self.assertEqual({v["content"] for v in history}, {"外部原稿", "人工修改"})
        self.assertTrue(any(v["content"] == "外部原稿" and v["source"] == "原有文件" for v in history))

    def test_modified_early_chapter_marks_later_finalized_memory_stale(self):
        project = self.project()
        self.store.save_doc(project, "directory", "第1章 起点\n本章简述：开始\n第2章 后续\n本章简述：继续")
        for chapter in (1, 2):
            document = self.store.save_doc(project, "chapter", f"第{chapter}章正文", chapter=chapter)
            self.store.record_finalization(project, chapter, document["revision"], {"index": "succeeded"})
        self.store.save_doc(project, "chapter", "第一章修改后的正文", chapter=1)
        result = self.store.chapters(project)
        self.assertEqual([c["status"] for c in result["chapters"]], ["modified", "memory_stale"])
        self.assertTrue(result["warnings"])


class WebJobsTest(WorkbenchFixture):
    def test_partial_success_errors_in_nested_results_are_redacted(self):
        secret = "private-model-key-123"
        config = {"embedding_configs": {"test": {"api_key": secret}}}

        def runner(ctx):
            return {"details": {"index": "failed", "index_details": {
                "error": f"embedding failed for {secret} at https://example.invalid/?key=other-secret"}}}

        manager = self.manager(runner, config)
        job = manager.submit(self.project(), {"type": "finalize"})
        result = self.completed(manager, job["id"])
        self.assertEqual(result["status"], "succeeded")
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("other-secret", serialized)
        self.assertNotIn(secret, json.dumps(self.store.jobs()))

    def test_candidates_do_not_write_until_adopted_and_adoption_is_idempotent(self):
        project = self.project()
        self.store.save_doc(project, "architecture", "原大纲")

        def runner(ctx):
            document = ctx.input_doc("architecture")
            return {"documents": [{"kind": "architecture", "content": "候选大纲",
                                    "base_revision": document["revision"]}]}

        manager = self.manager(runner)
        job = manager.submit(project, {"type": "architecture"})
        self.assertEqual(self.completed(manager, job["id"])["status"], "succeeded")
        self.assertEqual(self.store.doc(project, "architecture")["content"], "原大纲")
        manager.apply(job["id"])
        history = self.store.history(project, "architecture")
        manager.apply(job["id"])
        self.assertEqual(self.store.doc(project, "architecture")["content"], "候选大纲")
        self.assertEqual(self.store.history(project, "architecture"), history)

    def test_frozen_inputs_and_config_conflict_with_edits_during_generation(self):
        project = self.project()
        self.store.save_doc(project, "architecture", "输入大纲")
        config = {"llm_configs": {"test": {"api_key": "original-key"}}}
        started, release = threading.Event(), self.release_event()
        captured = {}

        def runner(ctx):
            started.set()
            release.wait(5)
            captured["config"] = ctx.config
            document = ctx.input_doc("architecture")
            captured["input"] = document["content"]
            return {"documents": [{"kind": "architecture", "content": "候选",
                                    "base_revision": document["revision"]}]}

        manager = self.manager(runner, config)
        job = manager.submit(project, {"type": "architecture"})
        self.assertTrue(started.wait(5))
        config["llm_configs"]["test"]["api_key"] = "changed-key"
        self.store.save_doc(project, "architecture", "生成期间的人工编辑")
        release.set()
        self.assertEqual(self.completed(manager, job["id"])["status"], "succeeded")
        self.assertEqual(captured["input"], "输入大纲")
        self.assertEqual(captured["config"]["llm_configs"]["test"]["api_key"], "original-key")
        with self.assertRaises(Conflict):
            manager.apply(job["id"])
        self.assertEqual(self.store.doc(project, "architecture")["content"], "生成期间的人工编辑")

    def test_new_architecture_cannot_reset_finalized_character_memory(self):
        project = self.project()
        self.store.save_doc(project, "characters", "第一章后的人物记忆")
        self.store.record_finalization(project, 1, revision("正文"), {"index": "succeeded"})

        def runner(ctx):
            return {"documents": [{"kind": kind, "content": content,
                                    "base_revision": ctx.input_doc(kind)["revision"]}
                                   for kind, content in (("architecture", "新大纲"), ("characters", "开篇人物状态"))]}

        manager = self.manager(runner)
        job = manager.submit(project, {"type": "architecture"})
        self.completed(manager, job["id"])
        manager.apply(job["id"])
        self.assertEqual(self.store.doc(project, "architecture")["content"], "新大纲")
        self.assertEqual(self.store.doc(project, "characters")["content"], "第一章后的人物记忆")

    def test_running_cancellation_is_requested_then_acknowledged(self):
        project = self.project()
        started, release = threading.Event(), self.release_event()

        def runner(ctx):
            started.set()
            release.wait(5)
            ctx.check_cancel()
            self.fail("cancelled task continued")

        manager = self.manager(runner)
        job = manager.submit(project, {"type": "draft"})
        self.assertTrue(started.wait(5))
        with self.assertRaises(Conflict):
            manager.submit(project, {"type": "draft"})
        self.assertEqual(manager.cancel(job["id"])["status"], "cancelling")
        release.set()
        self.assertEqual(self.completed(manager, job["id"])["status"], "cancelled")
        self.assertFalse(manager.active(project["id"]))

    def test_queued_cancelled_task_never_calls_runner(self):
        started, release = threading.Event(), self.release_event()
        called = []

        def runner(ctx):
            called.append(ctx.project["id"])
            started.set()
            release.wait(5)
            return {}

        manager = self.manager(runner)
        first = self.project("第一部")
        second = self.project("第二部")
        manager.submit(first, {"type": "review"})
        self.assertTrue(started.wait(5))
        queued = manager.submit(second, {"type": "review"})
        self.assertEqual(manager.cancel(queued["id"])["status"], "cancelled")
        release.set()
        deadline = time.monotonic() + 5
        while manager.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.queue.unfinished_tasks, 0)
        self.assertEqual(called, [first["id"]])

    def test_failed_job_redacts_secrets_in_events_errors_and_persisted_records(self):
        project = self.project()
        secrets = ("llm-private-key", "embedding-private-key", "url-private-token")
        config = {"llm_configs": {"model": {"api_key": secrets[0]}},
                  "embedding_configs": {"embedding": {"api_key": secrets[1]}}}

        def runner(ctx):
            ctx.emit(f"connecting with {secrets[0]}")
            raise RuntimeError(f"{secrets[0]}, {secrets[1]}, https://test.invalid/?token={secrets[2]}&mode=1")

        manager = self.manager(runner, config)
        job = manager.submit(project, {"type": "draft"})
        completed = self.completed(manager, job["id"])
        self.assertEqual(completed["status"], "failed")
        serialized = json.dumps([completed, self.store.jobs()], ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertIn("密钥已隐藏", completed["error"])

    def test_restart_marks_unfinished_jobs_interrupted_without_rerunning(self):
        project = self.project()
        for number, status in enumerate(("queued", "running", "cancelling", "succeeded")):
            self.store.save_job({"id": f"job-{number}", "project_id": project["id"],
                                 "status": status, "created_at": now(), "events": []})
        called = []
        manager = self.manager(lambda ctx: called.append(ctx.job_id))
        for number in range(3):
            self.assertEqual(manager.get(f"job-{number}")["status"], "interrupted")
        self.assertEqual(manager.get("job-3")["status"], "succeeded")
        self.assertEqual(called, [])
        persisted = {job["id"]: job for job in self.store.jobs()}
        self.assertEqual(persisted["job-1"]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
