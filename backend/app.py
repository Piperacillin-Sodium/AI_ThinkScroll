"""Same-origin JSON API and buildless HTML frontend for the local workbench."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import JobManager
from .storage import Conflict, Store
from .settings import SettingsService


def create_app(root: Path | None = None, data_dir: Path | None = None, runner=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    data_dir = data_dir or (Path(os.environ["AI_NOVEL_DATA_DIR"]) if os.environ.get("AI_NOVEL_DATA_DIR") else None)
    store = Store(root, data_dir)
    settings_service = SettingsService(root, store.lock)
    read_config = settings_service.read_config

    def safe_config():
        public = settings_service.get_public()
        return {"models": public["models"], "tasks": public["tasks"], "settings_deferred": False,
                "other_params": public["config"].get("other_params", {}),
                "language": public["config"].get("language", "zh")}

    def dispatch(ctx):
        if ctx.payload.get("type") in JobManager.SETTINGS_TYPES:
            return settings_service.run_operation(ctx)
        if ctx.payload.get("type") in {"knowledge_import", "knowledge_clear"}:
            from .knowledge import run_knowledge
            return run_knowledge(ctx)
        if runner is not None:
            return runner(ctx)
        from .generation import run_generation
        return run_generation(ctx)

    manager = JobManager(store, read_config, dispatch)

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(title="AI思卷 工作台", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.jobs, app.state.settings = store, manager, settings_service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "请从本地工作台页面发起操作。"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不支持跨站请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            # Revalidate buildless HTML and modules after local source updates.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(Conflict)
    async def conflict_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(OSError)
    async def io_error(request, exc):
        return JSONResponse({"detail": "文件操作失败，请检查目录是否可写、文件是否被其他程序占用。"}, status_code=500)

    def writable(project_id):
        if manager.active(project_id):
            raise Conflict("这部小说正在执行任务，请等待结束后再保存文件。编辑内容可以先保留在页面。")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/bootstrap")
    def bootstrap():
        # Import only the configured workspace novel, never scan/read arbitrary folders.
        if not store.projects():
            conf = read_config()
            params = conf.get("other_params", {})
            path = params.get("filepath")
            if path:
                try:
                    dest = Path(path)
                    if not dest.is_absolute():
                        dest = root / dest
                    if dest.is_dir():
                        safe = {k: params[k] for k in Store.DEFAULTS if k in params}
                        for key, low, high in (("num_chapters", 1, 10000), ("word_number", 100, 100000)):
                            try:
                                number = int(safe.get(key, Store.DEFAULTS[key]))
                            except (TypeError, ValueError):
                                number = 0
                            safe[key] = number if low <= number <= high else Store.DEFAULTS[key]
                        store.register(dest.name, dest, safe, existing=True)
                except ValueError:
                    pass
        projects = store.projects()
        return {"projects": projects, "config": safe_config(),
                "active_project_id": projects[0]["id"] if projects else None}

    @app.get("/api/projects")
    def projects():
        return {"projects": store.projects()}

    @app.post("/api/projects", status_code=201)
    def create_project(body: dict = Body(...)):
        defaults = dict(read_config().get("other_params", {}))
        for key, low, high in (("num_chapters", 1, 10000), ("word_number", 100, 100000), ("chapter_num", 1, 10000)):
            try:
                number = int(defaults.get(key, Store.DEFAULTS[key]))
            except (TypeError, ValueError):
                number = 0
            defaults[key] = number if low <= number <= high else Store.DEFAULTS[key]
        params = {**defaults, **body.get("params", {})}
        return store.register(body.get("name"), body.get("path"), params)

    @app.post("/api/projects/import")
    def import_project(body: dict = Body(...)):
        path = str(body.get("path", "")).strip()
        if not path:
            raise ValueError("请输入已有小说的目录路径。")
        return store.register(body.get("name") or Path(path).name, path, body.get("params", {}), existing=True)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return store.project(project_id)

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: dict = Body(...)):
        return store.update_project(project_id, body.get("params", {}))

    @app.get("/api/projects/{project_id}/chapters")
    def chapters(project_id: str):
        return store.chapters(store.project(project_id))

    @app.get("/api/projects/{project_id}/roles")
    def roles(project_id: str):
        return {"roles": store.roles(store.project(project_id))}

    @app.get("/api/projects/{project_id}/documents/{kind}/history")
    def history(project_id: str, kind: str, chapter: int = Query(1, ge=1, le=10000)):
        return {"versions": store.history(store.project(project_id), kind, chapter)}

    @app.get("/api/projects/{project_id}/documents/{kind}")
    def get_document(project_id: str, kind: str, chapter: int = Query(1, ge=1, le=10000)):
        return store.doc(store.project(project_id), kind, chapter)

    @app.put("/api/projects/{project_id}/documents/{kind}")
    def save_document(project_id: str, kind: str, body: dict = Body(...), chapter: int = Query(1, ge=1, le=10000)):
        if not isinstance(body.get("content"), str) or not isinstance(body.get("revision"), str):
            raise ValueError("保存时必须包含文本 content 和当前 revision。")
        with store.lock:
            writable(project_id)
            return store.save_doc(store.project(project_id), kind, body["content"], body["revision"], chapter)

    @app.post("/api/projects/{project_id}/jobs", status_code=202)
    def submit_job(project_id: str, body: dict = Body(...)):
        allowed = {"type", "mode", "chapter", "word_number", "user_guidance", "characters_involved",
                   "key_items", "scene_location", "time_constraint", "custom_prompt_text", "content",
                   "start", "end", "min_words", "auto_enrich", "role_ids", "config_name"}
        return manager.submit(store.project(project_id), {k: v for k, v in body.items() if k in allowed})

    @app.get("/api/jobs")
    def list_jobs(project_id: str | None = None):
        return {"jobs": manager.list(project_id)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return manager.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return manager.cancel(job_id)

    @app.post("/api/jobs/{job_id}/apply")
    def apply_job(job_id: str):
        return manager.apply(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, after: int = 0):
        manager.get(job_id)

        async def events():
            cursor = after
            while True:
                job = manager.get(job_id)
                for event in job["events"]:
                    if event["seq"] > cursor:
                        cursor = event["seq"]
                        yield f"id: {cursor}\nevent: log\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if job["status"] in manager.TERMINAL:
                    yield f"event: done\ndata: {json.dumps({'status': job['status']})}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/settings")
    def settings():
        return settings_service.get_public()

    @app.put("/api/settings")
    @app.patch("/api/settings")
    def save_settings(body: dict = Body(...)):
        return settings_service.save(body)

    @app.post("/api/settings/reload")
    def reload_settings():
        return settings_service.get_public()

    @app.post("/api/settings/operations", status_code=202)
    def settings_operation(body: dict = Body(...)):
        allowed = {"type", "name", "config", "webdav_config", "proxy_setting", "revision"}
        return manager.submit_settings({key: value for key, value in body.items() if key in allowed})

    @app.get("/api/settings/jobs")
    def settings_jobs():
        return {"jobs": manager.list("__settings__")}

    @app.get("/api/projects/{project_id}/knowledge")
    def knowledge_list(project_id: str):
        from .knowledge import knowledge_status
        return knowledge_status(store.project(project_id))

    @app.post("/api/projects/{project_id}/knowledge", status_code=202)
    def knowledge_import(project_id: str, body: dict = Body(...)):
        return manager.submit(store.project(project_id), {
            "type": "knowledge_import", "filename": body.get("filename"), "content": body.get("content")})

    @app.post("/api/projects/{project_id}/knowledge/clear", status_code=202)
    def knowledge_clear(project_id: str, body: dict = Body(...)):
        if body.get("confirm") is not True:
            raise ValueError("请先确认要清空当前小说的全部检索索引。")
        return manager.submit(store.project(project_id), {"type": "knowledge_clear"})

    frontend = root / "frontend"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
