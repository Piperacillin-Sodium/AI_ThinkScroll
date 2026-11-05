"""Single-worker, durable jobs; browser connections never own the worker lifecycle."""
from __future__ import annotations

import copy
import json
import logging
import queue
import re
import threading
import uuid
from pathlib import Path

from .storage import Conflict, Store, now, revision


class Cancelled(BaseException):
    """Do not let legacy generic retry handlers turn cancellation into more calls."""


class JobContext:
    def __init__(self, manager, job_id, project, payload, config, inputs):
        self.manager, self.store = manager, manager.store
        self.job_id, self.project, self.payload, self.config = job_id, project, payload, config
        self.input_documents = inputs
        self.input_roles = copy.deepcopy(self.store.roles(project))
        self.stage_dir = self.store.data_dir / "tasks" / job_id
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event = threading.Event()

    def emit(self, message, stage=None):
        self.manager.emit(self.job_id, message, stage)

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def input_doc(self, kind, chapter=1):
        for doc in self.input_documents:
            if doc["kind"] == kind and (kind not in Store.PER_CHAPTER or doc["chapter"] == int(chapter)):
                return dict(doc)
        return {"kind": kind, "chapter": int(chapter), "content": "", "revision": revision(""),
                "filename": str(self.store.document_path(self.project, kind, chapter))}


class JobManager:
    SETTINGS_TYPES = {"test_llm", "test_embedding", "test_webdav", "webdav_backup", "webdav_restore"}
    TYPES = {"architecture", "directory", "prompt", "draft", "review", "enrich", "finalize", "batch", "reindex",
             "knowledge_import", "knowledge_clear"} | SETTINGS_TYPES
    TERMINAL = {"cancelled", "succeeded", "failed", "interrupted"}

    def __init__(self, store, config_provider, runner=None):
        self.store, self.config_provider, self.runner = store, config_provider, runner
        # One lock order for metadata and file writes, including service callbacks.
        self.lock = store.lock
        self.records = {job["id"]: job for job in store.jobs()}
        self.contexts = {}
        self.queue = queue.Queue()
        for job in self.records.values():
            if job["status"] not in self.TERMINAL:
                job.update(status="interrupted", error="服务已重启。已保存文件保留，请检查后重试或继续生成。", finished_at=now())
                store.save_job(job)
        self.worker = threading.Thread(target=self._work, name="novel-generation", daemon=True)
        self.worker.start()

    @staticmethod
    def safe_config(config):
        return copy.deepcopy(config)

    def public(self, job):
        return copy.deepcopy({k: v for k, v in job.items() if not k.startswith("_")})

    def get(self, job_id):
        with self.lock:
            if job_id not in self.records:
                raise FileNotFoundError("任务不存在。")
            return self.public(self.records[job_id])

    def list(self, project_id=None):
        with self.lock:
            jobs = [j for j in self.records.values() if not project_id or j["project_id"] == project_id]
            return [self.public(j) for j in sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:60]]

    def submit_settings(self, payload):
        if payload.get("type") not in self.SETTINGS_TYPES:
            raise ValueError("不支持的设置操作。")
        project = {"id": "__settings__", "name": "全局设置", "path": str(self.store.data_dir / "settings"), "params": {}}
        return self.submit(project, payload)

    def submit(self, project, payload):
        payload = copy.deepcopy(payload)
        if payload.get("type") not in self.TYPES:
            raise ValueError("不支持的生成任务。")
        for key in ("chapter", "start", "end", "word_number", "min_words"):
            if key in payload and payload[key] not in (None, ""):
                try:
                    payload[key] = int(payload[key])
                except (ValueError, TypeError):
                    raise ValueError(f"{key} 必须为整数。") from None
                upper = 100000 if key in {"word_number", "min_words"} else 10000
                if not 1 <= payload[key] <= upper:
                    raise ValueError(f"{key} 超出有效范围。")
        if payload.get("mode", "new") not in {"new", "resume"}:
            raise ValueError("生成模式无效。")
        if len(json.dumps(payload, ensure_ascii=False)) > 6_000_000:
            raise ValueError("本次任务输入过大。")
        with self.lock:
            # Duplicate clicks cannot produce concurrent calls to paid APIs.
            for job in self.records.values():
                if job["project_id"] == project["id"] and job["status"] not in self.TERMINAL:
                    raise Conflict("当前小说还有未结束的任务，请等待完成或先请求停止。")
            if len(self.contexts) >= 20:
                raise Conflict("任务队列已满，请稍后重试。")
            config = self.safe_config(self.config_provider())
            inputs = [self.store.doc(project, kind) for kind in Store.STATIC]
            for kind, folder in Store.PER_CHAPTER.items():
                for path in sorted((Path(project["path"]) / folder).glob("chapter_*.txt")):
                    match = re.fullmatch(r"chapter_(\d+)\.txt", path.name)
                    if match and 1 <= int(match[1]) <= 10000:
                        inputs.append(self.store.doc(project, kind, int(match[1])))
            job_id = uuid.uuid4().hex
            ctx = JobContext(self, job_id, copy.deepcopy(project), payload, config, inputs)
            mirror = {**project, "path": str(ctx.stage_dir / "input")}
            for doc in inputs:
                self.store.atomic_write(self.store.document_path(mirror, doc["kind"], doc["chapter"]), doc["content"])
            job = {"id": job_id, "project_id": project["id"], "type": payload["type"],
                   "chapter": payload.get("chapter", 1), "status": "queued", "stage": "排队中",
                   "created_at": now(), "events": [], "result": None, "error": None, "applied": False}
            self.contexts[job_id], self.records[job_id] = ctx, job
            self.store.save_job(job)
            self.emit(job_id, "已记录本次输入，等待执行。")
            self.queue.put(job_id)
            return self.public(job)

    def redact(self, job_id, message):
        ctx = self.contexts.get(job_id)
        if ctx:
            def secrets(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if str(key).lower() in {"api_key", "webdav_password", "password", "token", "secret", "authorization"}:
                            if isinstance(item, str) and item:
                                yield item
                        else:
                            yield from secrets(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from secrets(item)
            for secret in sorted(set(secrets(ctx.config)) | set(secrets(ctx.payload)), key=len, reverse=True):
                message = message.replace(secret, "[密钥已隐藏]")
        message = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[凭据已隐藏]@", message)
        return re.sub(r"(?i)([?&](?:key|api_key|token)=)[^&\s]+", r"\1[已隐藏]", message)

    def redact_result(self, job_id, value):
        if isinstance(value, str):
            return self.redact(job_id, value)
        if isinstance(value, dict):
            return {key: self.redact_result(job_id, item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_result(job_id, item) for item in value]
        return value

    def emit(self, job_id, message, stage=None):
        with self.lock:
            job = self.records[job_id]
            message = self.redact(job_id, str(message))[:4000]
            seq = job["events"][-1]["seq"] + 1 if job["events"] else 1
            job["events"].append({"seq": seq, "time": now(), "message": message})
            job["events"] = job["events"][-1000:]
            if stage:
                job["stage"] = str(stage)
            self.store.save_job(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.records.get(job_id)
            if not job:
                raise FileNotFoundError("任务不存在。")
            if job["status"] not in self.TERMINAL:
                self.contexts[job_id].cancel_event.set()
                job["status"] = "cancelled" if job["status"] == "queued" else "cancelling"
                job["stage"] = "已停止" if job["status"] == "cancelled" else "等待当前请求结束后停止"
                self.emit(job_id, job["stage"])
            return self.public(job)

    def apply(self, job_id):
        with self.lock:
            job = self.records.get(job_id)
            if not job:
                raise FileNotFoundError("任务不存在。")
            if job["status"] != "succeeded":
                raise ValueError("只能采用已成功完成的候选结果。")
            project = self.store.project(job["project_id"])
            docs = (job.get("result") or {}).get("documents", [])
            if not docs:
                raise ValueError("这个任务已经直接保存，或没有可采用的文本。")
            if job["applied"]:
                return {"documents": [self.store.doc(project, d["kind"], d.get("chapter", 1)) for d in docs], "project": project}
            if any(j["project_id"] == project["id"] and j["status"] not in self.TERMINAL for j in self.records.values()):
                raise Conflict("请等待当前小说的生成任务结束后再采用结果。")
            # Regenerating an outline must not reset accumulated chapter memory.
            if job["type"] == "architecture" and self.store.finalizations(project):
                docs = [d for d in docs if d["kind"] != "characters"]
            result = self.store.apply_documents(project, docs, source=f"采用{job['type']}任务")
            job["applied"] = True
            self.emit(job_id, "候选结果已采用并写入历史版本。")
            return {"documents": result, "project": project}

    def active(self, project_id):
        with self.lock:
            return any(j["project_id"] == project_id and j["status"] not in self.TERMINAL for j in self.records.values())

    def _work(self):
        while True:
            job_id = self.queue.get()
            if job_id is None:
                self.queue.task_done()
                return
            ctx = self.contexts[job_id]
            with self.lock:
                job = self.records[job_id]
                if job["status"] == "cancelled":
                    self.contexts.pop(job_id, None)
                    self.queue.task_done()
                    continue
                job.update(status="running", started_at=now(), stage="准备执行")
                self.store.save_job(job)
            manager = self
            worker_ident = threading.get_ident()

            class ProgressHandler(logging.Handler):
                def emit(self, record):
                    if record.thread == worker_ident and record.levelno >= logging.INFO:
                        manager.emit(job_id, record.getMessage())

            handler = ProgressHandler()
            logging.getLogger().addHandler(handler)
            try:
                ctx.check_cancel()
                if self.runner is None:
                    from .generation import run_generation
                    runner = run_generation
                else:
                    runner = self.runner
                result = self.redact_result(job_id, runner(ctx))
                with self.lock:
                    job.update(status="succeeded", stage="已完成", result=result or {}, finished_at=now())
                    self.emit(job_id, "任务完成。" + ("请检查候选结果后采用。" if (result or {}).get("documents") else ""))
            except Cancelled:
                with self.lock:
                    job.update(status="cancelled", stage="已停止", finished_at=now())
                    self.emit(job_id, "已停止后续步骤；此前已提交的章节与记忆保留。")
            except Exception as exc:
                with self.lock:
                    message = self.redact(job_id, str(exc)) or type(exc).__name__
                    job.update(status="failed", stage="执行失败", error=message, finished_at=now())
                    self.emit(job_id, message)
            finally:
                logging.getLogger().removeHandler(handler)
                with self.lock:
                    self.store.save_job(job)
                    self.contexts.pop(job_id, None)
                self.queue.task_done()

    def close(self):
        self.queue.put(None)
