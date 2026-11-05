"""Versioned chapter indexing without changing the desktop application's vector API.

The collection is shared with the existing retrieval implementation. Stable IDs
make an interrupted/repeated finalization safe; unrelated imported knowledge is
never deleted.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def split_chapter(text: str, maximum: int = 500) -> list[str]:
    text = text.strip()
    return [text[i:i + maximum] for i in range(0, len(text), maximum) if text[i:i + maximum].strip()]


def _open_store(project_path: str | Path, config: dict):
    from embedding_adapters import create_embedding_adapter
    from langchain_chroma import Chroma
    from langchain_core.embeddings import Embeddings
    from chromadb.config import Settings

    interface = config.get("interface_format", "OpenAI")
    if not config.get("model_name"):
        raise ValueError("尚未配置向量模型，请先在 config.json 中配置 embedding_configs")
    if interface.lower() not in {"ollama", "ml studio"} and not config.get("api_key"):
        raise ValueError("向量 API Key 为空；正文和记忆已经保存，可配置后重试索引")
    adapter = create_embedding_adapter(
        interface, config.get("api_key", ""), config.get("base_url", ""), config["model_name"]
    )

    class EmbeddingWrapper(Embeddings):
        def embed_documents(self, texts):
            vectors = adapter.embed_documents(texts)
            if not vectors or len(vectors) != len(texts) or any(not vector for vector in vectors):
                raise RuntimeError("向量服务返回了空或不完整的 embeddings")
            return vectors

        def embed_query(self, text):
            vector = adapter.embed_query(text)
            if not vector:
                raise RuntimeError("向量服务返回了空 embedding")
            return vector

    return Chroma(
        persist_directory=str(Path(project_path) / "vectorstore"),
        embedding_function=EmbeddingWrapper(),
        client_settings=Settings(anonymized_telemetry=False),
        collection_name="novel_collection",
    )


def index_chapter(project_path: str | Path, chapter: int, content: str, config: dict) -> dict:
    """Upsert a chapter and remove only its obsolete chunks. Failures propagate."""
    if not content.strip():
        raise ValueError("空章节无法建立向量索引")
    store = _open_store(project_path, config)
    chapter = int(chapter)
    chunks = split_chapter(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    ids = [f"web:chapter:{chapter}:chunk:{i}" for i in range(len(chunks))]
    existing = store.get(where={"web_chapter": chapter}, include=["metadatas"])
    # add_texts uses Chroma upsert, so a retry replaces these exact IDs.
    store.add_texts(
        texts=[f"第{chapter}章\n{chunk}" for chunk in chunks],
        ids=ids,
        metadatas=[{"web_chapter": chapter, "source": "web_chapter", "content_hash": digest}] * len(chunks),
    )
    obsolete = set(existing.get("ids", [])) - set(ids)
    if obsolete:
        store.delete(ids=sorted(obsolete))
    manifest = Path(project_path) / ".knowledge" / "manifest.json"
    if manifest.is_file() and manifest.resolve().is_relative_to(Path(project_path).resolve()):
        from .storage import Store
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["_cleared"] = False
        Store.atomic_write(manifest, json.dumps(data, ensure_ascii=False, indent=2))
    return {"status": "completed", "chunks": len(chunks), "content_hash": digest}


def index_knowledge(project_path: str | Path, filename: str, content: str, config: dict) -> dict:
    """Stable source IDs avoid multiplying reference material on repeated imports."""
    chunks = split_chapter(content)
    if not chunks:
        raise ValueError("知识文件没有可导入的文本。")
    store = _open_store(project_path, config)
    source_id = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:24]
    ids = [f"web:knowledge:{source_id}:chunk:{i}" for i in range(len(chunks))]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    old = store.get(where={"web_knowledge": filename}, include=["metadatas"])
    store.add_texts(chunks, ids=ids, metadatas=[{
        "source": "knowledge", "web_knowledge": filename, "content_hash": digest,
    }] * len(chunks))
    obsolete = set(old.get("ids", [])) - set(ids)
    if obsolete:
        store.delete(ids=sorted(obsolete))
    return {"chunks": len(chunks), "content_hash": digest}


def clear_index(project_path: str | Path):
    """Clear vector collections without deleting open SQLite/HNSW files on Windows."""
    import chromadb
    from chromadb.config import Settings
    directory = Path(project_path) / "vectorstore"
    if not directory.is_dir():
        return 0
    client = chromadb.PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False))
    count = 0
    for item in client.list_collections():
        name = item if isinstance(item, str) else item.name
        collection = client.get_collection(name)
        count += collection.count()
        # Deleting the collection also releases its stored embedding dimension;
        # deleting rows alone prevents switching to a different dimension later.
        client.delete_collection(name)
    return count
