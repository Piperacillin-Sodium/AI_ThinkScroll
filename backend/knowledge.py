"""Reference-file jobs with the same queue, proxy snapshot, and status as generation."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .storage import Store


def _folder(project):
    base = Path(project["path"]).resolve()
    folder = (base / ".knowledge").resolve()
    if not folder.is_relative_to(base):
        raise ValueError("知识文件目录超出当前小说范围。")
    return folder


def _filename(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 180:
        raise ValueError("请选择名称有效的知识文件。")
    value = value.strip()
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((" ", ".")):
        raise ValueError("知识文件名称不能包含目录或非法字符。")
    if Path(value).suffix.lower() not in {".txt", ".md"}:
        raise ValueError("目前支持 TXT 和 Markdown 知识文件。")
    return value


def _manifest(project):
    path = _folder(project) / "manifest.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError):
        raise ValueError("知识库登记文件损坏，请先备份 .knowledge 目录再检查。") from None


def knowledge_status(project):
    data = _manifest(project)
    files = []
    for path in sorted(_folder(project).glob("*")):
        if path.suffix.lower() not in {".txt", ".md"} or not path.is_file():
            continue
        if not path.resolve().is_relative_to(_folder(project)):
            continue
        record = data.get(path.name, {})
        files.append({"name": path.name, "characters": record.get("characters", len(path.read_text(encoding="utf-8"))),
                      "indexed": bool(record.get("indexed"))})
    vector_path = (Path(project["path"]) / "vectorstore").resolve()
    if not vector_path.is_relative_to(Path(project["path"]).resolve()):
        raise ValueError("向量库位置超出当前小说范围。")
    return {"files": files, "has_vectorstore": vector_path.is_dir() and not data.get("_cleared", False)}


def run_knowledge(ctx):
    from .generation import _proxy_environment, _embedding_config
    from .vector_memory import clear_index, index_knowledge
    ctx.check_cancel()
    knowledge_status(ctx.project)  # Validate real paths before opening Chroma or writing files.
    with _proxy_environment(ctx):
        if ctx.payload["type"] == "knowledge_clear":
            ctx.emit("正在清理当前小说的全部向量检索记录。", "清理向量索引")
            count = clear_index(ctx.project["path"])
            # The clear has committed. Finish metadata even if stop was clicked meanwhile.
            with ctx.store.lock:
                data = _manifest(ctx.project)
                for value in data.values():
                    if isinstance(value, dict):
                        value["indexed"] = False
                data["_cleared"] = True
                Store.atomic_write(_folder(ctx.project) / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
                for receipt in ctx.store.finalizations(ctx.project):
                    ctx.store.record_finalization(ctx.project, receipt["chapter"], receipt["revision"], {
                        **receipt["details"], "index": "failed",
                        "index_details": {"error": "向量库已清理，请补建该章检索记忆。"},
                    })
            return {"documents": [], "message": f"已清理 {count} 条检索记录。正文、摘要、人物状态和知识源文件均已保留。"}
        if ctx.payload["type"] != "knowledge_import":
            raise ValueError("不支持的知识库操作。")
        filename = _filename(ctx.payload.get("filename"))
        path = (_folder(ctx.project) / filename).resolve()
        if not path.is_relative_to(_folder(ctx.project)):
            raise ValueError("知识文件位置超出当前小说目录。")
        content = ctx.payload.get("content")
        if content is None and path.is_file():
            content = path.read_text(encoding="utf-8")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("知识文件内容不能为空。")
        if len(content) > 5_000_000:
            raise ValueError("单个知识文件不能超过 500 万字符。")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        ctx.emit(f"正在切分并向量化知识文件：{filename}", "导入知识库")
        ctx.check_cancel()
        indexed = index_knowledge(ctx.project["path"], filename, content, _embedding_config(ctx))
        with ctx.store.lock:
            Store.atomic_write(path, content)
            data = _manifest(ctx.project)
            data[filename] = {"characters": len(content), "indexed": True, **indexed}
            data["_cleared"] = False
            Store.atomic_write(_folder(ctx.project) / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
        return {"documents": [], "message": f"知识文件已导入，共 {indexed['chunks']} 个文本分块。", "filename": filename, **indexed}
