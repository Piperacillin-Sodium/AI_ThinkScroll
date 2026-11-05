"""Service safety tests. All model and embedding calls are replaced locally."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.generation import run_generation
from backend.generation import _proxy_environment
from backend.generation import _options, _snapshot
from backend.jobs import Cancelled
from backend.storage import Store


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeContext:
    def __init__(self, store, project, stage, payload, config):
        self.store, self.project = store, copy.deepcopy(project)
        self.stage_dir, self.payload, self.config = Path(stage), payload, copy.deepcopy(config)
        self.stage_dir.mkdir(parents=True)
        self.cancelled, self.events = False, []
        self.input_documents = [store.doc(project, kind) for kind in Store.STATIC]
        for kind in Store.PER_CHAPTER:
            for chapter in range(1, 4):
                self.input_documents.append(store.doc(project, kind, chapter))

    def input_doc(self, kind, chapter=1):
        for document in self.input_documents:
            if document["kind"] == kind and (kind in Store.STATIC or document["chapter"] == chapter):
                return dict(document)
        return {"kind": kind, "chapter": chapter, "content": "", "revision": digest(""), "filename": ""}

    def emit(self, message, stage=None):
        self.events.append((message, stage))

    def check_cancel(self):
        if self.cancelled:
            raise Cancelled()


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = Store(root / "app", root / "runtime")
        self.project = self.store.register("测试小说", path=root / "novel", params={
            "topic": "寻找失踪的灯塔", "genre": "悬疑", "num_chapters": 3, "word_number": 100,
        })
        self.stage_number = 0
        self.config = {
            "llm_configs": {"test": {"interface_format": "OpenAI", "api_key": "fake-test-key",
                "base_url": "http://model.invalid/v1", "model_name": "fake", "temperature": 0.7,
                "max_tokens": 8000, "timeout": 30}},
            "choose_configs": {key: "test" for key in (
                "architecture_llm", "chapter_outline_llm", "prompt_draft_llm", "final_chapter_llm", "consistency_review_llm")},
            "last_embedding_interface_format": "test",
            "embedding_configs": {"test": {"interface_format": "OpenAI", "api_key": "fake-key", "model_name": "fake-embedding"}},
        }
        self.write("architecture", "真实大纲")
        self.write("directory", "第1章 开始\n本章简述：到达灯塔\n第2章 发现\n本章简述：发现信件\n第3章 结局\n本章简述：查明真相")
        self.write("characters", "旧人物状态")

    def write(self, kind, content, chapter=1):
        return self.store.save_doc(self.project, kind, content, chapter=chapter)

    def context(self, action, **payload):
        self.stage_number += 1
        return FakeContext(self.store, self.project,
            Path(self.temporary.name) / f"task-{self.stage_number}", {"type": action, **payload}, self.config)

    def test_draft_isolated_and_uses_frozen_input(self):
        self.write("chapter", "原有正文")
        self.write("prompt", "确认后的提示词")
        ctx = self.context("draft")
        self.write("architecture", "任务提交后用户修改的大纲")

        def generate(**kwargs):
            stage = Path(kwargs["filepath"])
            self.assertEqual((stage / "Novel_architecture.txt").read_text(encoding="utf-8"), "真实大纲")
            self.assertEqual(kwargs["custom_prompt_text"], "确认后的提示词")
            (stage / "chapters" / "chapter_1.txt").write_text("候选新正文", encoding="utf-8")
            return "候选新正文"

        with patch("novel_generator.chapter.generate_chapter_draft", side_effect=generate):
            result = run_generation(ctx)
        self.assertEqual(result["documents"][0]["content"], "候选新正文")
        self.assertEqual(result["documents"][0]["base_revision"], digest("原有正文"))
        self.assertEqual(self.store.doc(self.project, "chapter")["content"], "原有正文")

    def test_prepare_prompt_and_review_use_full_context(self):
        self.write("summary", "已经发生的剧情")
        self.write("plot_arcs", "尚未解决的信件")
        ctx = self.context("prompt")
        with patch("novel_generator.chapter.build_chapter_prompt", return_value="真实准备的提示词") as build:
            result = run_generation(ctx)
        self.assertEqual(result["documents"][0]["kind"], "prompt")
        self.assertEqual(build.call_args.kwargs["novel_number"], 1)
        self.assertEqual(self.store.doc(self.project, "prompt")["content"], "")

        ctx = self.context("review", content="尚未保存的当前编辑正文")
        with patch("consistency_checker.check_consistency", return_value="无明显冲突") as check:
            result = run_generation(ctx)
        arguments = check.call_args.kwargs
        self.assertIn("真实大纲", arguments["novel_setting"])
        self.assertIn("到达灯塔", arguments["novel_setting"])
        self.assertEqual(arguments["chapter_text"], "尚未保存的当前编辑正文")
        self.assertEqual(arguments["global_summary"], "已经发生的剧情")
        self.assertEqual(arguments["plot_arcs"], "尚未解决的信件")
        self.assertEqual(result["documents"][0]["kind"], "review")

    def test_architecture_checkpoint_resume_and_fingerprint(self):
        first = self.context("architecture")

        def fail(**kwargs):
            stage = Path(kwargs["filepath"])
            (stage / "partial_architecture.json").write_text(json.dumps({"core_seed_result": "已生成种子"}), encoding="utf-8")
            raise RuntimeError("模拟网络中断")

        with patch("novel_generator.architecture.Novel_architecture_generate", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "模拟网络中断"):
                run_generation(first)
        self.assertEqual(self.store.doc(self.project, "architecture")["content"], "真实大纲")
        metadata = (Path(self.project["path"]) / ".web-checkpoints" / "architecture" / "checkpoint.json").read_text(encoding="utf-8")
        self.assertNotIn("fake-test-key", metadata)

        def resume(**kwargs):
            stage = Path(kwargs["filepath"])
            self.assertEqual(json.loads((stage / "partial_architecture.json").read_text(encoding="utf-8"))["core_seed_result"], "已生成种子")
            (stage / "Novel_architecture.txt").write_text("完成的大纲", encoding="utf-8")
            (stage / "character_state.txt").write_text("新的角色状态", encoding="utf-8")
            (stage / "partial_architecture.json").unlink()

        with patch("novel_generator.architecture.Novel_architecture_generate", side_effect=resume) as model:
            result = run_generation(self.context("architecture", mode="resume"))
            self.assertEqual(model.call_count, 1)
            completed = run_generation(self.context("architecture", mode="resume"))
            self.assertEqual(model.call_count, 1, "已完成检查点应直接恢复，避免重复付费调用")
        self.assertEqual(result["documents"][0]["content"], "完成的大纲")
        self.assertEqual(completed["documents"][0]["content"], "完成的大纲")
        with self.assertRaisesRegex(ValueError, "已变更"):
            run_generation(self.context("architecture", mode="resume", topic="新的主题"))

    def test_directory_new_starts_empty_and_rejects_missing_chapter(self):
        def bad_directory(**kwargs):
            path = Path(kwargs["filepath"]) / "Novel_directory.txt"
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            path.write_text("第1章 开始\n本章简述：到达\n第3章 跳章\n", encoding="utf-8")

        with patch("novel_generator.blueprint.Chapter_blueprint_generate", side_effect=bad_directory):
            with self.assertRaisesRegex(RuntimeError, "缺章"):
                run_generation(self.context("directory"))
        cp = Path(self.project["path"]) / ".web-checkpoints" / "directory" / "Novel_directory.txt"
        self.assertNotIn("第3章", cp.read_text(encoding="utf-8"))
        self.assertIn("第2章", self.store.doc(self.project, "directory")["content"])

        def resume(**kwargs):
            path = Path(kwargs["filepath"]) / "Novel_directory.txt"
            self.assertIn("第1章", path.read_text(encoding="utf-8"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n第2章 继续\n第3章 完结\n")

        with patch("novel_generator.blueprint.Chapter_blueprint_generate", side_effect=resume):
            result = run_generation(self.context("directory", mode="resume"))
        self.assertEqual(result["chapters"], 3)

    def test_finalize_partial_index_and_idempotent_retry(self):
        self.write("chapter", "正式章节正文")
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.generation._invoke", side_effect=["新剧情摘要", "新角色状态"]) as invoke, \
             patch("backend.vector_memory.index_chapter", side_effect=RuntimeError("模拟向量服务故障")):
            result = run_generation(self.context("finalize"))
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(result["details"]["index"], "failed")
        self.assertEqual(self.store.doc(self.project, "summary")["content"], "新剧情摘要")
        receipt = self.store.get_finalization(self.project, 1)
        self.assertEqual(receipt["revision"], digest("正式章节正文"))

        with patch("backend.generation._invoke") as invoke, \
             patch("backend.vector_memory.index_chapter", return_value={"status": "completed", "chunks": 1}) as index:
            result = run_generation(self.context("finalize"))
            self.assertEqual(result["details"]["index"], "succeeded")
            self.assertEqual(index.call_count, 1)
            run_generation(self.context("finalize"))
            self.assertEqual(index.call_count, 1)
            invoke.assert_not_called()

    def test_finalize_second_model_failure_changes_nothing(self):
        self.write("chapter", "正式正文")
        before_summary = self.store.doc(self.project, "summary")["content"]
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.generation._invoke", side_effect=["不应保存的摘要", RuntimeError("角色模型失败")]):
            with self.assertRaisesRegex(RuntimeError, "角色模型失败"):
                run_generation(self.context("finalize"))
        self.assertEqual(self.store.doc(self.project, "summary")["content"], before_summary)
        self.assertEqual(self.store.doc(self.project, "characters")["content"], "旧人物状态")
        self.assertIsNone(self.store.get_finalization(self.project, 1))

    def test_finalize_cancelled_preparation_never_commits(self):
        self.write("chapter", "原有正文")
        ctx = self.context("finalize", content="编辑后的正文")

        def cancel_after_summary(*args):
            ctx.cancelled = True
            return "未提交摘要"

        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.generation._invoke", side_effect=cancel_after_summary):
            with self.assertRaises(Cancelled):
                run_generation(ctx)
        self.assertEqual(self.store.doc(self.project, "chapter")["content"], "原有正文")
        self.assertIsNone(self.store.get_finalization(self.project, 1))

    def test_cancelled_architecture_does_not_retry_model(self):
        ctx = self.context("architecture")

        class Adapter:
            calls = 0

            def invoke(self, prompt):
                self.calls += 1
                ctx.cancelled = True
                return "模型已经完成一次响应"

        adapter = Adapter()
        with patch("novel_generator.architecture.create_llm_adapter", return_value=adapter):
            with self.assertRaises(Cancelled):
                run_generation(ctx)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue((Path(self.project["path"]) / ".web-checkpoints" / "architecture" / "checkpoint.json").exists())
        self.assertEqual(self.store.doc(self.project, "architecture")["content"], "真实大纲")

    def test_revised_early_chapter_rebuilds_baseline_and_marks_later_stale(self):
        self.write("chapter", "第一章原稿", 1)
        self.write("chapter", "第二章原稿", 2)
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.vector_memory.index_chapter", return_value={"status": "completed"}), \
             patch("backend.generation._invoke", side_effect=["第一章摘要", "第一章角色", "第二章摘要", "第二章角色"]):
            run_generation(self.context("finalize", chapter=1))
            run_generation(self.context("finalize", chapter=2))
        self.write("chapter", "第一章修改稿", 1)
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.vector_memory.index_chapter", return_value={"status": "completed"}), \
             patch("backend.generation._invoke", side_effect=["第一章修改摘要", "第一章修改角色"]) as invoke:
            run_generation(self.context("finalize", chapter=1))
        first_prompt = invoke.call_args_list[0].args[2]
        self.assertNotIn("第二章摘要", first_prompt)
        self.assertEqual(self.store.get_finalization(self.project, 2)["details"]["memory"], "stale")
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.vector_memory.index_chapter", return_value={"status": "completed"}), \
             patch("backend.generation._invoke", side_effect=["第二章重建摘要", "第二章重建角色"]) as invoke:
            run_generation(self.context("finalize", chapter=2))
        self.assertIn("第一章修改摘要", invoke.call_args_list[0].args[2])
        self.assertEqual(self.store.get_finalization(self.project, 2)["details"]["memory"], "completed")

    def test_batch_stops_at_existing_unfinalized_chapter(self):
        self.write("chapter", "用户已写的草稿", 1)
        result = run_generation(self.context("batch", start=1, end=3))
        self.assertTrue(result["stopped"])
        self.assertIn("未定稿", result["reason"])
        self.assertEqual(self.store.doc(self.project, "chapter", 1)["content"], "用户已写的草稿")
        self.assertEqual(self.store.doc(self.project, "chapter", 2)["content"], "")

    def test_batch_next_chapter_sees_committed_memory(self):
        seen_summaries = []

        def prepare(**kwargs):
            seen_summaries.append((Path(kwargs["filepath"]) / "global_summary.txt").read_text(encoding="utf-8"))
            return f"提示词 {kwargs['novel_number']}"

        def draft(**kwargs):
            return f"第{kwargs['novel_number']}章正文。" * 30

        with patch("novel_generator.chapter.build_chapter_prompt", side_effect=prepare), \
             patch("novel_generator.chapter.generate_chapter_draft", side_effect=draft), \
             patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.vector_memory.index_chapter", return_value={"status": "completed"}), \
             patch("backend.generation._invoke", side_effect=["第一章新摘要", "第一章新角色", "第二章新摘要", "第二章新角色"]):
            result = run_generation(self.context("batch", start=1, end=2, min_words=50))
        self.assertEqual(seen_summaries, ["", "第一章新摘要"])
        self.assertEqual(len(result["chapters"]), 2)
        self.assertTrue(self.store.doc(self.project, "chapter", 2)["content"])
        self.assertEqual(self.store.doc(self.project, "prompt", 2)["content"], "提示词 2")

    def test_review_and_enrich_obey_specific_user_guidance(self):
        ctx = self.context("review", content="用户当前正文", user_guidance="检查信件出现的日期是否一致")
        with patch("consistency_checker.check_consistency", return_value="日期一致") as check:
            run_generation(ctx)
        self.assertIn("检查信件出现的日期是否一致", check.call_args.kwargs["novel_setting"])
        self.assertEqual(check.call_args.kwargs["chapter_text"], "用户当前正文")
        ctx = self.context("enrich", content="用户当前正文", user_guidance="补充灯塔的海雾环境描写")
        with patch("llm_adapters.create_llm_adapter", return_value=object()), \
             patch("backend.generation._invoke", return_value="包含海雾环境的新正文") as invoke:
            result = run_generation(ctx)
        self.assertIn("补充灯塔的海雾环境描写", invoke.call_args.args[2])
        self.assertIn("用户当前正文", invoke.call_args.args[2])
        self.assertEqual(result["documents"][0]["content"], "包含海雾环境的新正文")
        self.assertEqual(self.store.doc(self.project, "chapter")["content"], "")

    def test_proxy_setting_applied_and_environment_restored(self):
        ctx = self.context("draft")
        ctx.config["proxy_setting"] = {"enabled": True, "proxy_url": "127.0.0.1", "proxy_port": "7890"}
        with patch.dict(os.environ, {"HTTP_PROXY": "http://old.invalid:1", "ALL_PROXY": "socks5://old.invalid:2", "NO_PROXY": "private.test"}):
            with _proxy_environment(ctx):
                self.assertEqual(os.environ["HTTP_PROXY"], "http://127.0.0.1:7890")
                self.assertEqual(os.environ["HTTPS_PROXY"], "http://127.0.0.1:7890")
                self.assertNotIn("ALL_PROXY", os.environ)
                self.assertIn("localhost", os.environ["NO_PROXY"])
                self.assertIn("private.test", os.environ["NO_PROXY"])
            self.assertEqual(os.environ["HTTP_PROXY"], "http://old.invalid:1")
            self.assertEqual(os.environ["ALL_PROXY"], "socks5://old.invalid:2")
            ctx.config["proxy_setting"]["enabled"] = False
            with self.assertRaisesRegex(RuntimeError, "请求失败"):
                with _proxy_environment(ctx):
                    self.assertNotIn("HTTP_PROXY", os.environ)
                    self.assertNotIn("ALL_PROXY", os.environ)
                    self.assertEqual(os.environ["NO_PROXY"], "*")
                    raise RuntimeError("请求失败")
            self.assertEqual(os.environ["HTTP_PROXY"], "http://old.invalid:1")

    def test_english_directory_resume_fails_explicitly(self):
        self.write("directory", "Chapter 1 - Beginning\nChapter summary: The lighthouse.")
        with self.assertRaisesRegex(ValueError, "英文目录续生成"):
            run_generation(self.context("directory", mode="resume"))

    def test_only_retrieval_snapshots_copy_vector_index(self):
        source = Path(self.project["path"]) / "vectorstore"
        source.mkdir()
        (source / "index.dat").write_text("完整索引", encoding="utf-8")
        for action in ("architecture", "directory", "draft", "review", "enrich", "finalize", "reindex", "batch"):
            with self.subTest(action=action):
                ctx = self.context(action, chapter=2)
                stage, _ = _snapshot(ctx, _options(ctx))
                self.assertFalse((stage / "vectorstore").exists())
        first = self.context("prompt", chapter=1)
        first_stage, _ = _snapshot(first, _options(first))
        self.assertFalse((first_stage / "vectorstore").exists())
        for action, live in (("prompt", False), ("batch", True)):
            with self.subTest(action=action):
                ctx = self.context(action, chapter=2)
                stage, _ = _snapshot(ctx, _options(ctx), live=live)
                self.assertEqual((stage / "vectorstore" / "index.dat").read_text(encoding="utf-8"), "完整索引")

    def test_incomplete_vector_copy_is_never_used_for_retrieval(self):
        source = Path(self.project["path"]) / "vectorstore"
        source.mkdir()
        (source / "index.dat").write_text("完整索引", encoding="utf-8")
        ctx = self.context("prompt", chapter=2)

        def fail_copy(source, destination):
            Path(destination).mkdir()
            (Path(destination) / "index.dat").write_text("不完整索引", encoding="utf-8")
            raise OSError("模拟复制中断")

        with patch("backend.generation.shutil.copytree", side_effect=fail_copy):
            stage, _ = _snapshot(ctx, _options(ctx))
        self.assertFalse((stage / "vectorstore").exists())
        self.assertFalse(list(stage.glob("_vectorstore-copy-*")))
        self.assertTrue(any("本次不读取该索引" in message for message, _ in ctx.events))


if __name__ == "__main__":
    unittest.main()
