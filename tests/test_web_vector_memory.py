"""Real persistent Chroma integration with deterministic, offline embeddings.

Chroma's installed client has no public close method. Each phase uses a normal
child process so Windows releases its database handles before temp cleanup; the
second process also verifies the collection genuinely survives a restart.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class FixedEmbeddingAdapter:
    def embed_documents(self, texts):
        return [[float(len(text) % 7 + 1), 1.0, 2.0, 3.0] for text in texts]

    def embed_query(self, query):
        return [1.0, 1.0, 2.0, 3.0]


def exercise_persistent_store(project_path, phase):
    from backend.vector_memory import index_chapter
    from novel_generator.vectorstore_utils import load_vector_store

    check = unittest.TestCase()
    project_path.mkdir(parents=True, exist_ok=True)
    adapter = FixedEmbeddingAdapter()
    config = {"interface_format": "OpenAI", "api_key": "offline-test-only",
              "base_url": "https://never-called.invalid", "model_name": "fixed-4d"}
    with patch("embedding_adapters.create_embedding_adapter", return_value=adapter):
        if phase == "write":
            original = "甲" * 1201  # Three stable chapter chunks.
            first = index_chapter(project_path, 1, original, config)
            check.assertEqual(first["chunks"], 3)
            legacy = load_vector_store(adapter, str(project_path))
            check.assertIsNotNone(legacy, "desktop retrieval must read the web collection")
            legacy.add_texts(["资料库：灯塔建于海岸。"], ids=["knowledge:source:0"],
                             metadatas=[{"source": "knowledge"}])
            second = index_chapter(project_path, 2, "乙" * 701, config)
            check.assertEqual(second["chunks"], 2)
            check.assertEqual(len(legacy.get()["ids"]), 6)
            unaffected = legacy.get(ids=["knowledge:source:0", "web:chapter:2:chunk:0", "web:chapter:2:chunk:1"],
                                    include=["documents", "metadatas"])

            # Retry the exact same chapter: upsert IDs, never append duplicates.
            repeated = index_chapter(project_path, 1, original, config)
            check.assertEqual(repeated, first)
            check.assertEqual(len(legacy.get()["ids"]), 6)
            check.assertEqual(len(legacy.get(where={"web_chapter": 1})["ids"]), 3)

            # Shortening removes only obsolete chunks belonging to this chapter.
            shortened = index_chapter(project_path, 1, "精简后的第一章。", config)
            check.assertEqual(shortened["chunks"], 1)
            check.assertNotEqual(shortened["content_hash"], first["content_hash"])
            check.assertEqual(legacy.get(where={"web_chapter": 1})["ids"], ["web:chapter:1:chunk:0"])
            check.assertEqual(len(legacy.get()["ids"]), 4)
            after = legacy.get(ids=unaffected["ids"], include=["documents", "metadatas"])
            check.assertEqual(after, unaffected, "other chapters and imported knowledge must be unchanged")
        else:
            # A separate process cannot reuse the writer's in-memory collection.
            legacy = load_vector_store(adapter, str(project_path))
            check.assertIsNotNone(legacy)
            snapshot = legacy.get(include=["documents", "metadatas"])
            check.assertEqual(set(snapshot["ids"]), {"knowledge:source:0", "web:chapter:1:chunk:0",
                                                    "web:chapter:2:chunk:0", "web:chapter:2:chunk:1"})
            content = dict(zip(snapshot["ids"], snapshot["documents"]))
            check.assertEqual(content["web:chapter:1:chunk:0"], "第1章\n精简后的第一章。")
            check.assertEqual(content["knowledge:source:0"], "资料库：灯塔建于海岸。")
            retrieved = legacy.similarity_search("灯塔", k=4)
            check.assertEqual(len(retrieved), 4)
            check.assertTrue(any(document.metadata.get("source") == "knowledge" for document in retrieved))


def exercise_knowledge(project_path):
    from backend.vector_memory import index_chapter, index_knowledge, clear_index
    from novel_generator.vectorstore_utils import load_vector_store
    check = unittest.TestCase()
    config = {"interface_format": "OpenAI", "api_key": "offline-test-only",
              "base_url": "https://never-called.invalid", "model_name": "fixed-4d"}
    adapter = FixedEmbeddingAdapter()
    project_path.mkdir(parents=True, exist_ok=True)
    with patch("embedding_adapters.create_embedding_adapter", return_value=adapter):
        index_chapter(project_path, 1, "章节正文", config)
        index_knowledge(project_path, "资料.txt", "参考" * 800, config)
        store = load_vector_store(adapter, str(project_path))
        first = len(store.get()["ids"])
        index_knowledge(project_path, "资料.txt", "参考" * 800, config)
        check.assertEqual(len(store.get()["ids"]), first)
        index_knowledge(project_path, "资料.txt", "缩短的资料", config)
        check.assertEqual(len(store.get()["ids"]), 2)
        check.assertEqual(len(store.get(where={"web_chapter": 1})["ids"]), 1)
        check.assertEqual(clear_index(project_path), 2)
    class SmallerEmbedding:
        def embed_documents(self, texts):
            return [[1.0, 2.0] for text in texts]
        def embed_query(self, query):
            return [1.0, 2.0]
    smaller = SmallerEmbedding()
    with patch("embedding_adapters.create_embedding_adapter", return_value=smaller):
        index_knowledge(project_path, "新维度.txt", "重建后的资料", config)
        rebuilt = load_vector_store(smaller, str(project_path))
        check.assertEqual(len(rebuilt.get()["ids"]), 1)
        check.assertEqual(len(rebuilt.similarity_search("资料", k=1)), 1)


class WebVectorMemoryTest(unittest.TestCase):
    def test_knowledge_reimport_and_clear_allow_switching_embedding_dimension(self):
        with TemporaryDirectory() as temporary:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--knowledge", str(Path(temporary) / "知识测试")],
                cwd=REPO_ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True,
                text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_chapter_upsert_cleanup_and_desktop_retrieval_survive_restart(self):
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "小说"
            environment = dict(os.environ, PYTHONIOENCODING="utf-8")
            for phase in ("write", "read"):
                with self.subTest(phase=phase):
                    process = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--exercise", str(project), phase],
                        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
                    )
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    self.assertTrue((project / "vectorstore" / "chroma.sqlite3").is_file())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--exercise":
        exercise_persistent_store(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "--knowledge":
        exercise_knowledge(Path(sys.argv[2]))
    else:
        unittest.main()
