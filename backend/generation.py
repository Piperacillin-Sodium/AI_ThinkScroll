"""Real model-backed creation jobs for the browser workbench.

All generation runs in a task workspace. Only finalization/batch intentionally
commit files; every other operation returns reviewable candidate documents.
Imports of the model stack are lazy so project/file APIs remain usable even
when a model dependency is unavailable.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import urlsplit, urlunsplit


DOCUMENT_FILES = {
    "architecture": "Novel_architecture.txt",
    "directory": "Novel_directory.txt",
    "summary": "global_summary.txt",
    "characters": "character_state.txt",
    "plot_arcs": "plot_arcs.txt",
}
MODEL_KEYS = {
    "architecture": "architecture_llm",
    "directory": "chapter_outline_llm",
    "prompt": "prompt_draft_llm",
    "draft": "prompt_draft_llm",
    "enrich": "final_chapter_llm",
    "finalize": "final_chapter_llm",
    "review": "consistency_review_llm",
}


def _lock(ctx):
    if hasattr(ctx.store, "locked"):
        return ctx.store.locked(ctx.project)
    lock = getattr(ctx.store, "lock", None)
    return lock if lock is not None and hasattr(lock, "__enter__") else nullcontext()


def _positive(value, label, maximum=100000):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正整数")
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label}必须是正整数") from None
    if number < 1 or number > maximum or (isinstance(value, float) and value != number):
        raise ValueError(f"{label}必须是 1 到 {maximum} 之间的整数")
    return number


def _options(ctx):
    options = dict(ctx.project.get("params") or {})
    options.update({key: value for key, value in ctx.payload.items() if value is not None})
    options["chapter"] = _positive(options.get("chapter", options.get("chapter_num", 1)), "章节号")
    options["word_number"] = _positive(options.get("word_number") or 3000, "目标字数", 200000)
    options["num_chapters"] = _positive(options.get("num_chapters", options.get("number_of_chapters")) or 10, "章节数量")
    for key in ("topic", "genre", "user_guidance", "characters_involved", "key_items", "scene_location", "time_constraint"):
        options[key] = str(options.get(key) or "").strip()
    selected = {str(item) for item in (options.get("role_ids") or [])}
    if selected:
        roles = ctx.input_roles if hasattr(ctx, "input_roles") else ctx.store.roles(ctx.project)
        found = [role for role in roles if str(role.get("id")) in selected]
        if len(found) != len(selected):
            raise ValueError("所选角色已变更或不存在，请刷新角色列表")
        descriptions = "\n\n".join(f"【{role.get('name', '角色')}】\n{role.get('content', '')}" for role in found)
        options["user_guidance"] += "\n\n【必须遵守的角色资料】\n" + descriptions
    return options


def _llm_config(ctx, action):
    key = MODEL_KEYS.get(action, "prompt_draft_llm")
    name = (ctx.config.get("choose_configs") or {}).get(key) or ctx.config.get("last_llm_config_name")
    config = dict((ctx.config.get("llm_configs") or {}).get(name) or {})
    if not config or not config.get("model_name"):
        raise ValueError(f"{key} 尚未配置可用模型，请检查 config.json")
    interface = config.get("interface_format", "OpenAI")
    if interface.lower() not in {"ollama", "ml studio"} and not config.get("api_key"):
        raise ValueError(f"模型配置「{name}」的 API Key 为空，请在 config.json 中填写后重试")
    return {
        "interface_format": interface,
        "api_key": config.get("api_key", ""),
        "base_url": config.get("base_url", ""),
        "model_name": config["model_name"],
        "temperature": float(config.get("temperature", 0.7)),
        "max_tokens": int(config.get("max_tokens", 8192)),
        "timeout": int(config.get("timeout", 600)),
    }


def _embedding_config(ctx):
    name = ctx.config.get("last_embedding_interface_format", "OpenAI")
    return dict((ctx.config.get("embedding_configs") or {}).get(name) or {})


def _fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _public_model(config):
    return {key: value for key, value in config.items() if key != "api_key"}


def _read(path):
    return Path(path).read_text(encoding="utf-8-sig") if Path(path).is_file() else ""


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _doc(ctx, kind, chapter=1):
    return ctx.store.doc(ctx.project, kind, chapter=chapter)


def _candidate(document, content):
    return {
        "kind": document["kind"], "chapter": document.get("chapter", 1),
        "content": content, "base_revision": document["revision"],
    }


def _snapshot(ctx, options, live=False):
    stage = Path(ctx.stage_dir) / (f"workspace-{options['chapter']}" if live else "workspace")
    stage.mkdir(parents=True, exist_ok=True)
    documents = {}
    read_doc = (lambda kind, chapter=1: _doc(ctx, kind, chapter)) if live or not hasattr(ctx, "input_doc") else ctx.input_doc
    with _lock(ctx):
        for kind, filename in DOCUMENT_FILES.items():
            document = read_doc(kind)
            documents[kind] = document
            (stage / filename).write_text(document["content"], encoding="utf-8")
        chapter = options["chapter"]
        for kind in ("chapter", "prompt", "review"):
            documents[kind] = read_doc(kind, chapter)
        chapters_dir = stage / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        for number in range(max(1, chapter - 3), chapter + 1):
            document = documents["chapter"] if number == chapter else read_doc("chapter", number)
            (chapters_dir / f"chapter_{number}.txt").write_text(document["content"], encoding="utf-8")
        source_vectors = Path(ctx.project["path"]) / "vectorstore"
        destination_vectors = stage / "vectorstore"
        # Drafts already have a confirmed prompt. The first chapter's prompt
        # also bypasses retrieval in the existing generator. Batch snapshots
        # need retrieval only inside their per-chapter live snapshot.
        needs_retrieval = options["chapter"] > 1 and (
            options.get("type") == "prompt" or (options.get("type") == "batch" and live)
        )
        if needs_retrieval and source_vectors.is_dir() and not destination_vectors.exists():
            temporary_vectors = stage / f"_vectorstore-copy-{time.time_ns()}"
            try:
                # Publish only a complete copy. A failed copy must never be
                # visible under the vectorstore name used by core retrieval.
                shutil.copytree(source_vectors, temporary_vectors)
                temporary_vectors.rename(destination_vectors)
            except OSError:
                ctx.emit("向量库复制未完成，本次不读取该索引；提示词将使用大纲、目录和近章文本", "retrieval_warning")
                if (temporary_vectors.exists()
                        and temporary_vectors.resolve().is_relative_to(stage.resolve())):
                    try:
                        shutil.rmtree(temporary_vectors)
                    except OSError:
                        ctx.emit("未完成的索引复制已隔离在临时目录，生成流程不会读取它", "retrieval_warning")
    _atomic_json(Path(ctx.stage_dir) / "input-revisions.json", {
        kind: document["revision"] for kind, document in documents.items()
    })
    return stage, documents


def _require_creation_inputs(documents, chapter=None):
    if not documents["architecture"]["content"].strip():
        raise ValueError("请先生成并保存小说大纲")
    if chapter is not None:
        from chapter_directory_parser import parse_chapter_blueprint
        entries = parse_chapter_blueprint(documents["directory"]["content"])
        if not any(entry["chapter_number"] == chapter for entry in entries):
            raise ValueError(f"目录中没有第{chapter}章，请先生成并保存该章节目录")


def _checkpoint(ctx, action, fingerprint, stage, resume=False, files=()):
    """Restore/checkpoint only known local filenames; never trust stored paths."""
    root = Path(ctx.project["path"]) / ".web-checkpoints" / action
    metadata_path = root / "checkpoint.json"
    warnings = []
    with _lock(ctx):
        if resume:
            if metadata_path.is_file():
                metadata = json.loads(_read(metadata_path))
                if metadata.get("fingerprint") != fingerprint:
                    raise ValueError("续生成的参数、模型或输入内容已变更，请恢复原输入，或选择重新生成")
                for filename in files:
                    source = root / filename
                    if source.is_file():
                        shutil.copyfile(source, stage / filename)
            else:
                # Desktop checkpoints predate input fingerprints. Resume is
                # explicit, and the warning is retained in the task result.
                legacy = Path(ctx.project["path"]) / files[0] if files else None
                if legacy and legacy.is_file() and _read(legacy).strip():
                    shutil.copyfile(legacy, stage / files[0])
                    warning = "已导入桌面版的旧检查点；旧文件没有输入指纹，请核对主题、章节数和内容指导与原任务一致。"
                    warnings.append(warning)
                    ctx.emit(warning, "checkpoint")
                else:
                    raise ValueError("没有可继续的检查点，请先运行一次生成任务")
        root.mkdir(parents=True, exist_ok=True)
        # Mark this generation before work starts so failed jobs remain resumable.
        _atomic_json(metadata_path, {"fingerprint": fingerprint, "updated_at": time.time(), "completed": False})
        if not resume:
            for filename in files:
                if (root / filename).is_file():
                    (root / filename).unlink()
    return root, warnings


def _save_checkpoint(ctx, root, fingerprint, stage, files, completed):
    with _lock(ctx):
        for filename in files:
            if (stage / filename).is_file():
                shutil.copyfile(stage / filename, root / filename)
            elif (root / filename).is_file():
                (root / filename).unlink()
        _atomic_json(root / "checkpoint.json", {
            "fingerprint": fingerprint, "updated_at": time.time(), "completed": completed,
        })


def _architecture(ctx, options, stage, documents):
    from novel_generator.architecture import Novel_architecture_generate
    if not options["topic"]:
        raise ValueError("请填写小说主题或创意")
    config = _llm_config(ctx, "architecture")
    fingerprint = _fingerprint({
        "model": _public_model(config),
        **{key: options[key] for key in ("topic", "genre", "num_chapters", "word_number", "user_guidance")},
    })
    files = ("partial_architecture.json", "Novel_architecture.txt", "character_state.txt")
    root, warnings = _checkpoint(ctx, "architecture", fingerprint, stage, options.get("mode") == "resume", files)
    partial_path = stage / "partial_architecture.json"
    if partial_path.is_file():
        partial = json.loads(_read(partial_path))
        if partial.get("character_state_result"):
            (stage / "character_state.txt").write_text(partial["character_state_result"], encoding="utf-8")
    # Current adopted architecture must never masquerade as generated output.
    output = stage / "Novel_architecture.txt"
    if options.get("mode") == "resume" and _read(output).strip() and not (stage / "partial_architecture.json").exists():
        result_documents = [_candidate(documents["architecture"], _read(output))]
        if _read(stage / "character_state.txt").strip():
            result_documents.append(_candidate(documents["characters"], _read(stage / "character_state.txt")))
        _save_checkpoint(ctx, root, fingerprint, stage, files, True)
        ctx.emit("检查点中的大纲已完成，已恢复为待采用候选", "completed")
        return {"documents": result_documents, "warnings": warnings}
    if output.exists():
        output.unlink()
    completed = False
    ctx.emit("正在生成核心种子、角色动力学、角色初始状态、世界观和情节架构", "architecture")
    try:
        ctx.check_cancel()
        Novel_architecture_generate(
            **{key: value for key, value in config.items() if key != "model_name"},
            llm_model=config["model_name"], topic=options["topic"], genre=options["genre"],
            number_of_chapters=options["num_chapters"], word_number=options["word_number"],
            user_guidance=options["user_guidance"], filepath=str(stage),
        )
        ctx.check_cancel()
        content = _read(output).strip()
        if not content:
            raise RuntimeError("大纲未完成，已保留检查点；可选择继续生成")
        completed = True
        result_documents = [_candidate(documents["architecture"], content)]
        character_state = _read(stage / "character_state.txt").strip()
        if character_state:
            result_documents.append(_candidate(documents["characters"], character_state))
        ctx.emit("大纲与初始角色状态已生成，等待采用", "completed")
        return {"documents": result_documents, "warnings": warnings}
    finally:
        _save_checkpoint(ctx, root, fingerprint, stage, files, completed)


def _directory_entries(content):
    from chapter_directory_parser import parse_chapter_blueprint
    return parse_chapter_blueprint(content)


def _directory_prefix(content, maximum):
    """Keep the first contiguous sequence only, so an invalid chunk can retry."""
    from chapter_directory_parser import _parse_chapter_header
    lines = content.splitlines(keepends=True)
    next_number = 1
    saw_header = False
    for index, line in enumerate(lines):
        header = _parse_chapter_header(line)
        if header:
            number = header[0]
            if number != next_number or number > maximum:
                return "".join(lines[:index]).strip() if saw_header else ""
            next_number += 1
            saw_header = True
    return content.strip() if saw_header else ""


def _directory(ctx, options, stage, documents):
    from novel_generator.blueprint import Chapter_blueprint_generate
    _require_creation_inputs(documents)
    config = _llm_config(ctx, "directory")
    fingerprint = _fingerprint({
        "model": _public_model(config), "architecture": documents["architecture"]["content"],
        "number": options["num_chapters"], "guidance": options["user_guidance"],
    })
    output = stage / "Novel_directory.txt"
    output.write_text("", encoding="utf-8")
    files = ("Novel_directory.txt",)
    root, warnings = _checkpoint(ctx, "directory", fingerprint, stage, options.get("mode") == "resume", files)
    if options.get("mode") == "resume":
        content = _read(output)
        if re.search(r"(?im)^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+)*chapter\s+\d+", content):
            raise ValueError("当前目录使用英文章节标题，现有生成引擎暂不支持英文目录续生成；请选择重新生成，或改为「第N章」格式后再继续")
        numbers = [entry["chapter_number"] for entry in _directory_entries(content)]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("检查点目录存在重复章节或缺章，无法安全续生成；请选择重新生成")
    completed = False
    ctx.emit("正在分块生成章节目录；完成的分块会保留为检查点", "directory")
    try:
        ctx.check_cancel()
        Chapter_blueprint_generate(
            **{key: value for key, value in config.items() if key != "model_name"},
            llm_model=config["model_name"], filepath=str(stage), number_of_chapters=options["num_chapters"],
            user_guidance=options["user_guidance"],
        )
        ctx.check_cancel()
        content = _read(output).strip()
        numbers = [entry["chapter_number"] for entry in _directory_entries(content)]
        if numbers != list(range(1, options["num_chapters"] + 1)):
            raise RuntimeError("模型返回的目录缺章、重复或未完成；已保存有效检查点，可继续生成")
        completed = True
        ctx.emit(f"{len(numbers)} 章目录已生成，等待采用", "completed")
        return {"documents": [_candidate(documents["directory"], content)], "warnings": warnings, "chapters": len(numbers)}
    finally:
        if not completed:
            output.write_text(_directory_prefix(_read(output), options["num_chapters"]), encoding="utf-8")
        _save_checkpoint(ctx, root, fingerprint, stage, files, completed)


def _chapter_arguments(ctx, options, stage, action):
    config = _llm_config(ctx, action)
    embedding = _embedding_config(ctx)
    return {
        **config, "filepath": str(stage), "novel_number": options["chapter"],
        "word_number": options["word_number"], "user_guidance": options["user_guidance"],
        "characters_involved": options["characters_involved"], "key_items": options["key_items"],
        "scene_location": options["scene_location"], "time_constraint": options["time_constraint"],
        "embedding_api_key": embedding.get("api_key", ""), "embedding_url": embedding.get("base_url", ""),
        "embedding_interface_format": embedding.get("interface_format", "OpenAI"),
        "embedding_model_name": embedding.get("model_name", ""),
        "embedding_retrieval_k": int(embedding.get("retrieval_k", 4)),
    }


def _prompt(ctx, options, stage, documents):
    from novel_generator.chapter import build_chapter_prompt
    _require_creation_inputs(documents, options["chapter"])
    ctx.emit("正在汇总近章、检索知识并生成本章场景计划", "prompt")
    ctx.check_cancel()
    content = build_chapter_prompt(**_chapter_arguments(ctx, options, stage, "prompt"))
    ctx.check_cancel()
    if not content.strip():
        raise RuntimeError("模型未能生成请求提示词")
    ctx.emit("提示词已准备，可以编辑后再生成正文", "completed")
    return {"documents": [_candidate(documents["prompt"], content)]}


def _draft(ctx, options, stage, documents):
    from novel_generator.chapter import generate_chapter_draft
    _require_creation_inputs(documents, options["chapter"])
    custom = options.get("custom_prompt_text")
    if custom is None:
        custom = documents["prompt"]["content"]
    if not str(custom or "").strip():
        raise ValueError("请先准备并确认本章提示词，再生成草稿")
    ctx.emit(f"正在生成第 {options['chapter']} 章草稿", "draft")
    ctx.check_cancel()
    content = generate_chapter_draft(
        **_chapter_arguments(ctx, options, stage, "draft"), custom_prompt_text=str(custom)
    )
    ctx.check_cancel()
    if not content.strip():
        raise RuntimeError("模型返回了空草稿，请重试")
    return {"documents": [_candidate(documents["chapter"], content)], "word_count": len(content)}


def _current_content(options, documents):
    content = options["content"] if "content" in options else documents["chapter"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("当前章节正文为空，请先生成或载入正文")
    return content


def _enrich(ctx, options, stage, documents):
    from novel_generator.finalization import enrich_chapter_text
    content = _current_content(options, documents)
    ctx.emit(f"正在扩写第 {options['chapter']} 章，目标 {options['word_number']} 字", "enrich")
    ctx.check_cancel()
    config = _llm_config(ctx, "enrich")
    if options["user_guidance"]:
        import prompt_definitions
        from llm_adapters import create_llm_adapter
        prompt = prompt_definitions.enrich_prompt.format(chapter_text=content, word_number=options["word_number"])
        prompt += "\n\n【本次扩写要求】\n" + options["user_guidance"]
        expanded = _invoke(ctx, create_llm_adapter(**config), prompt)
    else:
        expanded = enrich_chapter_text(chapter_text=content, word_number=options["word_number"], **config)
    ctx.check_cancel()
    if not expanded.strip():
        raise RuntimeError("模型返回了空扩写结果")
    return {"documents": [_candidate(documents["chapter"], expanded)], "word_count": len(expanded)}


def _review(ctx, options, stage, documents):
    from consistency_checker import check_consistency
    _require_creation_inputs(documents, options["chapter"])
    content = _current_content(options, documents)
    ctx.emit("正在依据真实大纲、章节目录、剧情摘要、人物状态和未解决冲突审校当前正文", "review")
    ctx.check_cancel()
    setting = documents["architecture"]["content"] + "\n\n【章节目录】\n" + documents["directory"]["content"]
    if options["user_guidance"]:
        setting += "\n\n【本次审校要求】\n" + options["user_guidance"]
    review = check_consistency(
        novel_setting=setting, character_state=documents["characters"]["content"],
        global_summary=documents["summary"]["content"], chapter_text=content,
        plot_arcs=documents["plot_arcs"]["content"], **_llm_config(ctx, "review"),
    )
    ctx.check_cancel()
    if not review or not review.strip() or review.strip() == "审校Agent无回复":
        raise RuntimeError("审校模型没有回复，请重试")
    return {"documents": [_candidate(documents["review"], review)]}


def _invoke(ctx, adapter, prompt):
    from novel_generator.common import invoke_with_cleaning
    ctx.check_cancel()
    result = invoke_with_cleaning(adapter, prompt)
    ctx.check_cancel()
    if not result.strip():
        raise RuntimeError("模型返回空内容，尚未提交任何记忆变更")
    return result


def _index(ctx, chapter, content, revision, details):
    from .vector_memory import index_chapter
    ctx.emit(f"正在为第 {chapter} 章建立向量索引", "index")
    # A receipt already exists at this point. Finish recording the outcome even
    # if the user requests cancellation during this short post-commit stage.
    ctx._committing_index = True
    try:
        index_result = index_chapter(ctx.project["path"], chapter, content, _embedding_config(ctx))
        details = {**details, "index": "succeeded", "index_details": index_result}
    except Exception as exc:
        details = {**details, "index": "failed", "index_details": {"status": "failed", "error": str(exc)}}
        ctx.emit("正文、摘要和角色状态已保存；向量索引失败，可配置后重试索引", "index_failed")
    finally:
        ctx._committing_index = False
    with _lock(ctx):
        # A direct external edit during indexing must never get a receipt for
        # content other than the exact version embedded above.
        if _doc(ctx, "chapter", chapter)["revision"] != revision:
            details = {**details, "index": "failed", "index_details": {"status": "failed", "error": "索引期间正文已变化，请重新定稿"}}
        ctx.store.record_finalization(ctx.project, chapter, revision, details)
    return details


def _finalize(ctx, options, stage, documents):
    chapter = options["chapter"]
    content = _current_content(options, documents)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    warnings = []
    with _lock(ctx):
        current = _doc(ctx, "chapter", chapter)
        if current["revision"] != documents["chapter"]["revision"]:
            raise ValueError("任务开始后当前章节已被修改，请重新载入正文再定稿")
        receipts = ctx.store.finalizations(ctx.project)
        for receipt in receipts:
            if receipt["chapter"] < chapter:
                prior = _doc(ctx, "chapter", receipt["chapter"])
                if receipt["details"].get("memory") == "stale" or receipt["revision"] != prior["revision"]:
                    raise ValueError(f"请先重新定稿第 {receipt['chapter']} 章，再按顺序处理后续章节")
        previous = ctx.store.get_finalization(ctx.project, chapter)
        if (previous and previous.get("details", {}).get("content_hash") == content_hash
                and previous.get("details", {}).get("memory") != "stale"
                and previous.get("revision") == current["revision"] and current["content"] == content):
            details = previous.get("details", {})
            if details.get("index") == "succeeded":
                ctx.emit("此版本已经定稿，摘要和人物状态无需重复更新", "completed")
                return {"documents": [], "committed": True, "already_finalized": True, "chapter": chapter, "details": details}
            revision = documents["chapter"]["revision"]
            retry_index = True
        else:
            retry_index = False
    if retry_index:
        details = _index(ctx, chapter, content, revision, details)
        return {"documents": [], "committed": True, "already_finalized": True, "chapter": chapter, "details": details}

    _require_creation_inputs(documents, chapter)
    basis = {"summary": documents["summary"]["content"], "characters": documents["characters"]["content"]}
    if previous and previous.get("details", {}).get("memory") != "stale":
        before = previous.get("details", {}).get("before_memory")
        if before is not None:
            basis = before
            ctx.emit("正在从该章首次定稿前的记忆基线重新生成，后续章节将标记为需要重新定稿", "memory_rebuild")
        elif any(receipt["chapter"] > chapter for receipt in receipts):
            raise ValueError("旧定稿记录没有保存记忆基线，请先恢复该章定稿前的摘要和人物状态后重试")
        else:
            warnings.append("旧定稿记录缺少定稿前记忆基线，请核对累计摘要与人物状态。")
    elif not previous and (basis["summary"].strip() or (receipts == [] and chapter > 1)):
        warnings.append("这是该章节首次通过网页版定稿；旧工程没有定稿收据，无法判断旧摘要是否已包含本章，请核对定稿后的记忆。")
    for warning in warnings:
        ctx.emit(warning, "memory_warning")
    import prompt_definitions
    from llm_adapters import create_llm_adapter
    config = _llm_config(ctx, "finalize")
    adapter = create_llm_adapter(**config)
    ctx.emit(f"正在更新第 {chapter} 章的全局剧情摘要", "summary")
    summary = _invoke(ctx, adapter, prompt_definitions.summary_prompt.format(
        chapter_text=content, global_summary=basis["summary"]
    ))
    ctx.emit("正在更新角色状态，全部生成完成后统一保存", "characters")
    characters = _invoke(ctx, adapter, prompt_definitions.update_character_state_prompt.format(
        chapter_text=content, old_state=basis["characters"]
    ))
    changes = [_candidate(documents["chapter"], content), _candidate(documents["summary"], summary), _candidate(documents["characters"], characters)]
    details = {"content_hash": content_hash, "memory": "completed", "index": "pending", "finalized_at": time.time(), "before_memory": basis}
    ctx.check_cancel()
    with _lock(ctx):
        committed = ctx.store.apply_documents(ctx.project, changes, source="finalize")
        chapter_doc = next(document for document in committed if document["kind"] == "chapter")
        revision = chapter_doc["revision"]
        ctx.store.record_finalization(ctx.project, chapter, revision, details)
        for receipt in receipts:
            if receipt["chapter"] > chapter:
                ctx.store.record_finalization(ctx.project, receipt["chapter"], receipt["revision"], {
                    **receipt["details"], "memory": "stale", "invalidated_by": chapter,
                })
    ctx.emit("正文、剧情摘要和角色状态已统一保存", "memory_committed")
    details = _index(ctx, chapter, content, revision, details)
    return {"documents": [], "committed": True, "chapter": chapter, "saved_documents": committed, "details": details, "warnings": warnings}


def _batch(ctx, options, stage, documents):
    start = _positive(options.get("start", options["chapter"]), "起始章节")
    end = _positive(options.get("end", start), "结束章节")
    if end < start or end - start > 500:
        raise ValueError("批量范围无效，单次最多生成 501 章")
    minimum = _positive(options.get("min_words") or int(options["word_number"] * 0.8), "最少字数", 200000)
    results = []
    for chapter in range(start, end + 1):
        ctx.check_cancel()
        item_options = {**options, "chapter": chapter}
        # Re-snapshot after every committed chapter: subsequent chapters must
        # use the newly updated cumulative memory.
        item_stage, item_documents = _snapshot(ctx, item_options, live=True)
        if item_documents["chapter"]["content"].strip():
            receipt = ctx.store.get_finalization(ctx.project, chapter)
            if (receipt and receipt["revision"] == item_documents["chapter"]["revision"]
                    and receipt["details"].get("memory") != "stale"):
                if receipt["details"].get("index") != "succeeded":
                    retried = _finalize(ctx, item_options, item_stage, item_documents)
                    if retried["details"].get("index") != "succeeded":
                        return {"documents": [], "committed": True, "chapters": results, "stopped": True, "reason": f"第 {chapter} 章向量索引仍失败"}
                ctx.emit(f"第 {chapter} 章已经定稿，跳过该章", "batch_skip")
                results.append({"chapter": chapter, "status": "skipped", "reason": "已经定稿"})
                continue
            ctx.emit(f"第 {chapter} 章已有未定稿或修改后的正文，请先在定稿页处理，批量任务已停止", "batch_stopped")
            return {"documents": [], "committed": True, "chapters": results, "stopped": True, "reason": f"第 {chapter} 章存在未定稿正文"}
        ctx.emit(f"批量创作第 {chapter} 章（{chapter - start + 1}/{end - start + 1}）", "batch")
        prompt = _prompt(ctx, item_options, item_stage, item_documents)["documents"][0]["content"]
        item_options["custom_prompt_text"] = prompt
        draft = _draft(ctx, item_options, item_stage, item_documents)["documents"][0]["content"]
        if options.get("auto_enrich") and len(draft) < minimum:
            draft = _enrich(ctx, {**item_options, "content": draft}, item_stage, item_documents)["documents"][0]["content"]
        if len(draft) < minimum:
            # Keep the generated candidate on disk for recovery without silently
            # accepting a chapter below the user's minimum.
            _atomic_json(Path(ctx.stage_dir) / f"chapter-{chapter}-candidate.json", {"chapter": chapter, "content": draft, "prompt": prompt})
            ctx.emit(f"第 {chapter} 章不足 {minimum} 字，已停止并保留草稿候选供检查", "batch_stopped")
            return {"documents": [_candidate(item_documents["chapter"], draft), _candidate(item_documents["prompt"], prompt)],
                    "committed": True, "chapters": results, "stopped": True, "reason": f"第 {chapter} 章不足 {minimum} 字"}
        finalized = _finalize(ctx, {**item_options, "content": draft}, item_stage, item_documents)
        ctx.store.save_doc(ctx.project, "prompt", prompt, revision=item_documents["prompt"]["revision"], chapter=chapter, source="batch")
        results.append({"chapter": chapter, "status": "completed", "details": finalized["details"]})
        _atomic_json(Path(ctx.stage_dir) / "batch-progress.json", {"results": results})
        if finalized["details"].get("index") == "failed":
            ctx.emit("向量索引失败，批量任务停止；已完成的章节和记忆均已保存", "batch_stopped")
            return {"documents": [], "committed": True, "chapters": results, "stopped": True, "reason": "index_failed"}
    return {"documents": [], "committed": True, "chapters": results}


def _reindex(ctx, options, stage, documents):
    chapter = options["chapter"]
    document = documents["chapter"]
    if not document["content"].strip():
        raise ValueError("当前章节没有保存的正文，无法重建索引")
    existing = ctx.store.get_finalization(ctx.project, chapter)
    details = existing.get("details", {}) if existing else {}
    content_hash = hashlib.sha256(document["content"].encode("utf-8")).hexdigest()
    if existing and details.get("content_hash") != content_hash:
        raise ValueError("已保存正文与定稿版本不同，请先定稿当前正文再重建索引")
    if not existing:
        raise ValueError("请先定稿当前章节，再重建该章索引")
    if details.get("memory") == "stale":
        raise ValueError("该章创作记忆已经过期，请先按顺序重新定稿")
    details = _index(ctx, chapter, document["content"], document["revision"], details)
    return {"documents": [], "committed": True, "chapter": chapter, "details": details}


@contextmanager
def _core_progress(ctx):
    """Cancellation at model log boundaries without global adapter monkeypatches.

    The desktop generators log between model calls. A thread-filtered handler
    provides stage feedback and lets Cancelled (a BaseException) escape retries.
    """
    thread_id = threading.get_ident()
    stages = {
        "Step1: Generating": ("正在生成核心种子", "core_seed"),
        "Step2: Generating": ("正在生成角色动力学", "character_dynamics"),
        "Generating initial character state": ("正在生成初始角色状态", "initial_characters"),
        "Step3: Generating": ("正在生成世界观", "world_building"),
        "Step4: Generating": ("正在生成情节架构", "plot_architecture"),
        "Attempting to generate summary": ("正在汇总最近三章", "recent_summary"),
    }

    class ProgressHandler(logging.Handler):
        def emit(self, record):
            if record.thread != thread_id:
                return
            if not getattr(ctx, "_committing_index", False):
                ctx.check_cancel()
            message = record.getMessage()
            for prefix, (label, stage) in stages.items():
                if message.startswith(prefix):
                    ctx.emit(label, stage)
                    break
            if message.startswith("Generating chapters ["):
                ctx.emit("正在生成目录分块 " + message.split("[", 1)[1].split("]", 1)[0], "directory_chunk")

    handler = ProgressHandler()
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


@contextmanager
def _proxy_environment(ctx):
    """Apply the desktop proxy setting for this single-worker model task only."""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    previous = {key: os.environ.get(key) for key in keys}
    settings = ctx.config.get("proxy_setting") or {}
    try:
        # Lowercase and ALL_PROXY must also be cleared: requests/httpx can
        # otherwise pick up a stale shell/system proxy despite the UI switch.
        for key in keys:
            if key.lower() != "no_proxy":
                os.environ.pop(key, None)
        if settings.get("enabled", False):
            address = str(settings.get("proxy_url") or "127.0.0.1").strip()
            if "://" not in address:
                address = "http://" + address
            parsed = urlsplit(address)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                raise ValueError("代理地址无效，请检查 config.json 中的 proxy_setting")
            port = str(settings.get("proxy_port") or "").strip()
            if port:
                parsed_port = _positive(port, "代理端口", 65535)
                host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
                if parsed.username:
                    host = parsed.username + (":" + parsed.password if parsed.password else "") + "@" + host
                address = urlunsplit((parsed.scheme, f"{host}:{parsed_port}", parsed.path, parsed.query, parsed.fragment))
            os.environ["HTTP_PROXY"] = address
            os.environ["HTTPS_PROXY"] = address
            os.environ["http_proxy"] = address
            os.environ["https_proxy"] = address
        exclusions = []
        for current in (previous.get("NO_PROXY"), previous.get("no_proxy")):
            exclusions.extend(part.strip() for part in (current or "").split(",") if part.strip() and part.strip() != "*")
        exclusions.extend(["localhost", "127.0.0.1", "::1"])
        # urllib on Windows can otherwise fall back to the registry proxy even
        # after proxy variables were cleared. '*' makes the disabled switch
        # unambiguously direct for requests and SDK HTTP clients.
        bypass = ",".join(dict.fromkeys(exclusions)) if settings.get("enabled", False) else "*"
        os.environ["NO_PROXY"] = bypass
        os.environ["no_proxy"] = bypass
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def run_generation(ctx) -> dict:
    """Execute one task using the storage/job context supplied by the server."""
    options = _options(ctx)
    action = options.get("type")
    actions = {
        "architecture": _architecture, "directory": _directory,
        "prompt": _prompt, "draft": _draft, "enrich": _enrich,
        "review": _review, "finalize": _finalize, "batch": _batch, "reindex": _reindex,
    }
    if action not in actions:
        raise ValueError("不支持的生成任务类型")
    ctx.check_cancel()
    stage, documents = _snapshot(ctx, options)
    from .language import creative_language
    with _proxy_environment(ctx), _core_progress(ctx), creative_language(ctx.config):
        result = actions[action](ctx, options, stage, documents)
    return {"type": action, **result}
